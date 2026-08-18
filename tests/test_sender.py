"""Sender retry loop: printer-error notifications and failure reporting."""
import requests
import pytest

import sender as sender_mod
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


@pytest.fixture
def fast_sender(monkeypatch):
    monkeypatch.setattr(sender_mod, "RETRY_INTERVAL_S", 0.05)
    monkeypatch.setattr(sender_mod, "RETRY_MAX_ATTEMPTS", 3)

    def make(responses):
        calls = {"n": 0}

        def fake_post(url, headers=None, files=None, data=None, timeout=None):
            r = responses[min(calls["n"], len(responses) - 1)]
            calls["n"] += 1
            return r

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