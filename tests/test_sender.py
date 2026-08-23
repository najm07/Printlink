"""Sender retry loop: printer-error notifications and failure reporting.

Also covers the audit fixes: graceful handling of non-JSON host responses
(B3) and the failure-latch invariant that keeps wait_idle() honest while
sibling jobs are still in flight (B6).
"""
import json
import threading
import requests
import pytest

import sender as sender_mod
from auth import sign_nonce, token_hint
from sender import Sender


class FakeDB:
    def __init__(self, token="a" * 64, expires="2099-01-01 00:00:00"):
        self.row = {"host_id": "111 222 333", "printer_alias": "CANON",
                    "name": None, "status": "active", "expires_at": expires,
                    "token": token, "host_ip": "127.0.0.1", "host_port": 9100}

    def get_remote_printer(self, host_id, alias):
        return self.row

    def update_remote_host_ip(self, host_id, ip, port):
        pass

    def list_remote_printers(self, status=None):
        if status is None:
            return [self.row]
        return [self.row] if self.row["status"] == status else []


class FakeResp:
    def __init__(self, code, text=""):
        self.status_code = code
        self.text = text

    def json(self):
        # requests raises JSONDecodeError (a ValueError) on bad bodies —
        # mirror that so _safe_json's guard is exercised for real.
        return json.loads(self.text)


@pytest.fixture
def fast_sender(monkeypatch):
    monkeypatch.setattr(sender_mod, "RETRY_INTERVAL_S", 0.05)
    monkeypatch.setattr(sender_mod, "RETRY_MAX_ATTEMPTS", 3)

    def make(responses):
        calls = {"n": 0}

        def fake_get(url, params=None, timeout=None):
            if url.endswith("/auth-challenge"):
                return FakeResp(200, '{"nonce": "nonce-1"}')
            return FakeResp(200, '{"id": "111 222 333", "ok": true}')

        def fake_post(url, headers=None, files=None, data=None, json=None,
                      timeout=None):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        monkeypatch.setattr(requests, "post", fake_post)
        db = FakeDB()
        s = Sender(db, "111 222 333", "test",
                   lambda hid: ("127.0.0.1", 9100))
        return s, db

    return make


def _send(s, tmp_path, name="doc.pdf"):
    f = tmp_path / name
    f.write_bytes(b"%PDF-1.7 test")
    ok, msg = s.print_file(f, "111222333", "CANON")
    assert ok, msg
    return f


def test_printer_error_notified_once_then_delivered(fast_sender, tmp_path):
    s, db = fast_sender([FakeResp(503, '{"error": "printer is offline"}'),
                         FakeResp(200, '{"status": "printed"}')])
    events = []
    s.on_printer_error = lambda fp, hid, al, reason: events.append(("error", reason))
    s.on_delivered = lambda *a: events.append(("delivered",))
    s.on_failed = lambda *a: events.append(("failed",))
    _send(s, tmp_path)
    assert s.wait_idle(timeout=10)
    assert events == [("error", '{"error": "printer is offline"}'), ("delivered",)]


def test_failure_after_retries_reports_reason_once(fast_sender, tmp_path):
    s, db = fast_sender([FakeResp(503, "printer is offline")])
    events = []
    s.on_printer_error = lambda fp, hid, al, reason: events.append(("error", reason))
    s.on_delivered = lambda *a: events.append(("delivered",))
    s.on_failed = lambda fp, hid, al, reason: events.append(("failed", reason))
    _send(s, tmp_path)
    assert s.wait_idle(timeout=10) is False
    assert sum(1 for e in events if e[0] == "error") == 1
    assert events[-1] == ("failed", "printer is offline")


def test_permanent_rejection_notifies_error_then_fails(fast_sender, tmp_path):
    s, db = fast_sender([FakeResp(500, "paper out")])
    events = []
    s.on_printer_error = lambda fp, hid, al, reason: events.append(("error", reason))
    s.on_failed = lambda fp, hid, al, reason: events.append(("failed", reason))
    _send(s, tmp_path)
    assert s.wait_idle(timeout=10) is False
    assert events == [("error", "paper out"), ("failed", "paper out")]


# ---------- B3: non-JSON host responses must degrade to normal failures ----

def test_request_share_tolerates_non_json_ping(fast_sender, monkeypatch):
    s, _db = fast_sender([])
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: FakeResp(200, "<html>502 Bad Gateway</html>"))
    ok, msg = s.request_share("111 222 333", "CANON", 7)
    assert ok is False
    assert "/ping" in msg


def test_request_share_tolerates_non_json_reply(fast_sender, monkeypatch):
    s, _db = fast_sender([FakeResp(502, "<html>proxy error page</html>")])
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: FakeResp(200, '{"id": "111 222 333", "ok": true}'))
    ok, msg = s.request_share("111 222 333", "CANON", 7)
    assert ok is False
    assert "unexpectedly" in msg and "502" in msg


def test_request_share_rejects_bad_ping_status(fast_sender, monkeypatch):
    s, _db = fast_sender([])
    monkeypatch.setattr(
        requests, "get",
        lambda *a, **k: FakeResp(404, '{"error": "nope"}'))
    ok, msg = s.request_share("111 222 333", "CANON", 7)
    assert ok is False
    assert "/ping" in msg


# ---------- B6: failure latch survives sibling enqueue mid-cycle -----------

def _pause_worker(s):
    """Keep the retry worker from completing jobs so the lock-state asserts
    below are deterministic; release the Event to let the worker drain."""
    release = threading.Event()

    def stuck(*a, **k):
        release.wait(5)
        return False, False     # permanent failure once released

    s._send_once = stuck
    return release


def test_failure_latch_not_reset_mid_cycle(fast_sender, tmp_path):
    """Old code reset _ever_failed on EVERY print_file(); with a sibling job
    still outstanding that erased its failure from wait_idle()'s verdict."""
    s, _db = fast_sender([FakeResp(500, "boom")])
    release = _pause_worker(s)
    f = tmp_path / "b.pdf"
    f.write_bytes(b"%PDF-1.4 x")
    with s._lock:
        s._pending = 1          # sibling job in flight mid-cycle...
        s._ever_failed = True   # ...which has already failed once
    assert s.print_file(f, "111222333", "CANON")[0]
    with s._lock:
        assert (s._pending, s._ever_failed) == (2, True)
    release.set()


def test_failure_latch_resets_on_new_drain_cycle(fast_sender, tmp_path):
    """A fresh cycle (queue fully drained beforehand) starts clean again."""
    s, _db = fast_sender([FakeResp(500, "boom")])
    release = _pause_worker(s)
    f = tmp_path / "c.pdf"
    f.write_bytes(b"%PDF-1.4 x")
    with s._lock:
        s._pending = 0          # everything reported before this point
        s._ever_failed = True
    assert s.print_file(f, "111222333", "CANON")[0]
    with s._lock:
        assert s._ever_failed is False
    release.set()

# ---------- Phase 3: HMAC headers, legacy fallback, verified routes ----

def _make_sender(monkeypatch, token="ab" * 32, ping_id="111222333",
                 challenge="nonce-1", post_hook=None):
    """Sender with fully stubbed network + capture lists."""
    monkeypatch.setattr(sender_mod, "RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(sender_mod, "RETRY_MAX_ATTEMPTS", 2)
    cap = {"gets": [], "posts": []}

    def fake_get(url, params=None, timeout=None):
        cap["gets"].append(url)
        if url.endswith("/auth-challenge"):
            if challenge is None:
                return FakeResp(404, "not found")
            return FakeResp(200, json.dumps({"nonce": challenge}))
        return FakeResp(200, json.dumps({"id": ping_id}))

    def fake_post(url, headers=None, files=None, data=None, json=None,
                  timeout=None):
        cap["posts"].append({"url": url, "headers": dict(headers or {})})
        if post_hook:
            return post_hook(len(cap["posts"]))
        return FakeResp(200, '{"status": "accepted"}')

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)
    db = FakeDB(token=token)
    s = Sender(db, "111 222 333", "t", lambda hid: ("127.0.0.1", 9100))
    return s, cap


def _doc(tmp_path, name="d.pdf"):
    f = tmp_path / name
    f.write_bytes(b"%PDF-1.4 x")
    return str(f)


def test_print_sends_hmac_headers_not_token(monkeypatch, tmp_path):
    s, cap = _make_sender(monkeypatch, challenge="nonce-9")
    assert s.print_file(_doc(tmp_path), "111222333", "CANON")[0]
    assert s.wait_idle(timeout=5)
    h = cap["posts"][0]["headers"]
    assert "X-Token" not in h                      # the token never leaves
    assert h["X-Token-Hint"] == token_hint("ab" * 32)
    assert h["X-Nonce"] == "nonce-9"
    assert h["X-Signature"] == sign_nonce("ab" * 32, "nonce-9")


def test_print_falls_back_to_token_without_challenge(monkeypatch, tmp_path):
    s, cap = _make_sender(monkeypatch, challenge=None)   # pre-0.3 host
    assert s.print_file(_doc(tmp_path), "111222333", "CANON")[0]
    assert s.wait_idle(timeout=5)
    h = cap["posts"][0]["headers"]
    assert h.get("X-Token") == "ab" * 32
    assert "X-Signature" not in h


def test_identity_mismatch_blocks_send_and_never_caches(monkeypatch, tmp_path):
    """Wrong machine at the resolved IP: nothing sensitive is sent, and the
    bad route is re-checked (not cached) on every attempt."""
    s, cap = _make_sender(monkeypatch, ping_id="999888777")
    assert s.print_file(_doc(tmp_path), "111222333", "CANON")[0]
    assert s.wait_idle(timeout=5) is False
    assert cap["posts"] == []
    pings = [u for u in cap["gets"] if u.endswith("/ping")]
    assert len(pings) >= 2


def test_verified_route_cached_within_ttl(monkeypatch, tmp_path):
    s, cap = _make_sender(monkeypatch, challenge="n")
    for i in range(2):
        assert s.print_file(_doc(tmp_path, f"d{i}.pdf"), "111222333", "CANON")[0]
        assert s.wait_idle(timeout=5)
    pings = [u for u in cap["gets"] if u.endswith("/ping")]
    challenges = [u for u in cap["gets"] if u.endswith("/auth-challenge")]
    assert len(pings) == 1                         # cached route: no re-ping
    assert len(challenges) == 2                    # fresh nonce per job
