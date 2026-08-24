"""End-to-end API tests: real Flask app + test client, pywin32 stubbed."""
import io
import sys
import time
import types
from datetime import datetime, timedelta, timezone
import pytest

# stub Windows-only printing before server imports it
fake = types.ModuleType("printer_local")
fake.DEFAULT_OPTIONS = {"copies": 1, "pages": "", "paper": "auto",
                        "color": "color", "duplex": "off",
                        "orientation": "auto", "fit": "fit"}
fake.list_printers = lambda: ["HP LaserJet Pro"]
fake.printer_status = lambda n: {"offline": False, "paused": False,
                                 "error": False, "jobs_queued": 0,
                                 "port": "USB001", "name": n}
PRINTED = []
fake.print_via_shell = lambda path, printer: PRINTED.append((open(path, "rb").read(), printer))
fake.print_text = lambda data, printer, job="PrintLink Job", copies=1: PRINTED.append((data, printer))
fake.print_emf = lambda data, printer, job="PrintLink Job": PRINTED.append((data, printer))
fake.print_word = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer))
fake.print_image = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer))
fake.print_pdf = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer, opts))
fake.sniff_format = lambda d: ("pdf" if d[:5] == b"%PDF-" else
                               "docx" if b"word/document.xml" in d else
                               "png" if d[:4] == b"\x89PNG" else
                               "xps" if d[:4] == b"PK\x03\x04" else
                               "text" if b"\x00" not in d[:1024] else "binary")
fake.extract_emf = lambda d: None
sys.modules["printer_local"] = fake

from db import Database
from config import MAX_SHARE_DAYS, RATE_SHARE_MAX
import server as server_mod
from server import create_app
from crypto import encrypt_payload
from auth import sign_nonce, token_hint


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_shared_printer("HP LaserJet Pro", "Accounting-HP")
    app = create_app(db, "482 917 305", on_share_request=lambda *a: True)
    return app.test_client(), db


def request_share(client, sender="111 222 333", alias="Accounting-HP"):
    r = client.post("/request-share", json={"sender_id": sender, "sender_name": "Test-PC",
                                            "printer_alias": alias, "days": 7})
    return r


def do_print(client, token, sender="111 222 333", payload=b"%PDF-1.4 test",
             options=None):
    """Proof-path print (1.0 has no legacy X-Token auth)."""
    nonce = _challenge(client, sender)
    body = encrypt_payload(payload, token)
    fields = {"file": (io.BytesIO(body), "doc.pdf")}
    if options is not None:
        import json
        fields["options"] = json.dumps(options)
    return client.post("/print",
                       headers={"X-Sender-ID": sender,
                                "X-Token-Hint": token_hint(token),
                                "X-Nonce": nonce,
                                "X-Signature": sign_nonce(token, nonce)},
                       data=fields, content_type="multipart/form-data")


def test_ping(env):
    client, _ = env
    j = client.get("/ping").get_json()
    assert j["id"] == "482 917 305"
    assert "version" in j  # lets users verify which build a PC runs


def test_share_accept_and_print(env):
    client, _ = env
    r = request_share(client)
    assert r.status_code == 200
    token = r.get_json()["token"]
    r = do_print(client, token)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"


def test_unknown_printer_404(env):
    client, _ = env
    assert request_share(client, alias="Ghost").status_code == 404


def test_decline_403(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_shared_printer("HP", "a")
    app = create_app(db, "1", on_share_request=lambda *a: False)
    r = app.test_client().post("/request-share",
                               json={"sender_id": "111 222 333", "printer_alias": "a"})
    assert r.status_code == 403


def test_revoke_grant(env):
    client, db = env
    token = request_share(client).get_json()["token"]
    # legacy body-token is refused outright in 1.0
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333",
                          "printer_alias": "Accounting-HP", "token": token})
    assert r.status_code == 404
    assert "removed in PrintLink 1.0" in r.get_json()["reason"]
    # wrong signature over its own fresh challenge -> no matching grant
    wrong_nonce = _challenge(client)
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333",
                          "printer_alias": "Accounting-HP",
                          "nonce": wrong_nonce,
                          "signature": sign_nonce("wrong" * 8, wrong_nonce)})
    assert r.status_code == 404
    # correct HMAC proof over its own fresh challenge revokes
    nonce = _challenge(client)
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333",
                          "printer_alias": "Accounting-HP",
                          "nonce": nonce,
                          "signature": sign_nonce(token, nonce)})
    assert r.status_code == 200 and r.get_json()["status"] == "revoked"
    assert db.list_grants()[0]["status"] == "revoked"


def test_bad_token_403(env):
    client, _ = env
    assert do_print(client, "wrong").status_code == 403


def test_no_file_400(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = client.post("/print", headers=_proof_headers(client, token))
    assert r.status_code == 400


def test_printers_listing(env):
    client, _ = env
    rows = client.get("/printers").get_json()
    assert rows[0]["alias"] == "Accounting-HP" and not rows[0]["status"]["offline"]


def test_print_word_docx(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    docx = (b"PK\x03\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00[Content_Types].xml"
            b"\x00\x00\x00\x00\x00\x00\x00\x00"
            b"word/document.xml")
    body = encrypt_payload(docx, token)
    r = client.post("/print", headers=_proof_headers(client, token),
                    data={"file": (io.BytesIO(body), "doc.docx")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"
    assert b"word/document.xml" in PRINTED[-1][0]


def test_print_png(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    body = encrypt_payload(png, token)
    r = client.post("/print", headers=_proof_headers(client, token),
                    data={"file": (io.BytesIO(body), "pic.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"
    assert PRINTED[-1][0].startswith(b"\x89PNG")


def test_print_pdf_with_options(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token,
                 options={"copies": 2, "pages": "1-3,5", "paper": "A4",
                          "color": "mono", "duplex": "long", "fit": "fit"})
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][2] == {"copies": 2, "pages": "1-3,5", "paper": "A4",
                              "color": "mono", "duplex": "long", "fit": "fit"}


def test_print_bad_options_400(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token, options="not-json{")
    assert r.status_code == 400


def test_print_text_copies(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token, payload=b"hello text\n",
                 options={"copies": 3})
    assert r.status_code == 200
    assert PRINTED[-1] == (b"hello text\n", "HP LaserJet Pro")


# ---------- S3: 'days' is validated and clamped server-side ----------------

EXP_FMT = "%Y-%m-%d %H:%M:%S"


def _grant_expiry(db, remote_id):
    g = next(g for g in db.list_grants() if g["remote_id"] == remote_id)
    return datetime.strptime(g["expires_at"], EXP_FMT).replace(tzinfo=timezone.utc)


def test_share_days_clamped_to_max(env):
    """A crafted 10^9-day request must not stick (audit S3)."""
    client, db = env
    r = client.post("/request-share", json={
        "sender_id": "222 333 444", "sender_name": "Greedy",
        "printer_alias": "Accounting-HP", "days": 10 ** 9})
    assert r.status_code == 200
    span = _grant_expiry(db, "222333444") - datetime.now(timezone.utc)
    assert timedelta(days=MAX_SHARE_DAYS - 1) < span \
        <= timedelta(days=MAX_SHARE_DAYS, seconds=10)


def test_share_days_clamped_to_min(env):
    client, db = env
    r = client.post("/request-share", json={
        "sender_id": "333 444 555", "sender_name": "Negative",
        "printer_alias": "Accounting-HP", "days": -5})
    assert r.status_code == 200
    span = _grant_expiry(db, "333444555") - datetime.now(timezone.utc)
    assert timedelta(0) < span <= timedelta(days=1, seconds=10)


def test_share_days_non_numeric_400(env):
    client, db = env
    r = client.post("/request-share", json={
        "sender_id": "444 555 666", "sender_name": "Junk",
        "printer_alias": "Accounting-HP", "days": "soon"})
    assert r.status_code == 400
    assert not [g for g in db.list_grants() if g["remote_id"] == "444555666"]


# ---------- S4: the unauthenticated /request-share endpoint is limited -----

def test_share_request_rate_limited(env):
    client, _db = env
    codes = []
    for i in range(RATE_SHARE_MAX + 2):
        codes.append(client.post(
            "/request-share",
            json={"sender_id": f"{100 + i:09d}", "sender_name": "Spam",
                  "printer_alias": "Accounting-HP"}).status_code)
    assert codes[:RATE_SHARE_MAX] == [200] * RATE_SHARE_MAX
    assert codes[RATE_SHARE_MAX:] == [429] * 2


# ---------- low-severity fixes: 401 on bad ciphertext, generic 500 ---------

def test_print_tampered_ciphertext_401(env):
    client, _db = env
    token = request_share(client).get_json()["token"]
    blob = b"\x00" * 48          # right length, wrong everything else
    r = client.post("/print",
                    headers=_proof_headers(client, token),
                    data={"file": (io.BytesIO(blob), "x.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 401
    assert r.get_json()["error"] == "decrypt failed"


def test_print_failure_returns_generic_body(env, monkeypatch):
    """500 must not echo exception text (can contain local paths/hostnames)."""
    client, _db = env
    token = request_share(client).get_json()["token"]

    def boom(path, printer, opts=None):
        raise RuntimeError("C:\\Users\\admin\\secrets\\crashed")

    monkeypatch.setattr(server_mod, "print_pdf", boom)
    body = encrypt_payload(b"%PDF-1.4 x", token)
    r = client.post("/print",
                    headers=_proof_headers(client, token),
                    data={"file": (io.BytesIO(body), "doc.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 500
    assert r.get_json() == {"error": "print failed"}


# ---------- Phase 3: HMAC challenge-response auth (token never on wire) ----

def _challenge(client, sender="111 222 333"):
    r = client.get("/auth-challenge", query_string={"sender_id": sender})
    assert r.status_code == 200
    return r.get_json()["nonce"]


def _proof_headers(client, token, sender="111 222 333"):
    """Auth headers for ONE request: fetches a fresh single-use nonce."""
    nonce = _challenge(client, sender)
    return {"X-Sender-ID": sender,
            "X-Token-Hint": token_hint(token),
            "X-Nonce": nonce,
            "X-Signature": sign_nonce(token, nonce)}


def _do_print_proof(client, token, sender="111 222 333",
                    payload=b"%PDF-1.4 test", nonce=None):
    nonce = nonce or _challenge(client, sender)
    body = encrypt_payload(payload, token)
    return client.post("/print",
                       headers={"X-Sender-ID": sender,
                                "X-Token-Hint": token_hint(token),
                                "X-Nonce": nonce,
                                "X-Signature": sign_nonce(token, nonce)},
                       data={"file": (io.BytesIO(body), "doc.pdf")},
                       content_type="multipart/form-data")


def test_challenge_bad_sender_400(env):
    client, _db = env
    r = client.get("/auth-challenge", query_string={"sender_id": "abc"})
    assert r.status_code == 400


def test_hmac_print_flow(env):
    """Full 0.3 flow: no X-Token header anywhere, job accepted."""
    client, db = env
    token = request_share(client).get_json()["token"]
    r = _do_print_proof(client, token)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][0] == b"%PDF-1.4 test"


def test_hmac_wrong_proof_403(env):
    client, _db = env
    token = request_share(client).get_json()["token"]
    nonce = _challenge(client)
    r = client.post("/print",
                    headers={"X-Sender-ID": "111 222 333",
                             "X-Token-Hint": token_hint(token),
                             "X-Nonce": nonce,
                             "X-Signature": sign_nonce("ff" * 32, nonce)},
                    data={"file": (io.BytesIO(b"x" * 64), "d.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 403
    assert r.get_json()["error"] == "unknown ID or proof"


def test_nonce_is_single_use(env):
    client, _db = env
    token = request_share(client).get_json()["token"]
    nonce = _challenge(client)
    assert _do_print_proof(client, token, nonce=nonce).status_code == 200
    replay = client.post("/print",
                         headers={"X-Sender-ID": "111 222 333",
                                  "X-Token-Hint": token_hint(token),
                                  "X-Nonce": nonce,
                                  "X-Signature": sign_nonce(token, nonce)},
                         data={"file": (io.BytesIO(b"x" * 64), "d.pdf")},
                         content_type="multipart/form-data")
    assert replay.status_code == 403
    assert replay.get_json()["error"] == "invalid or expired challenge"


def test_expired_nonce_rejected(env, monkeypatch):
    monkeypatch.setattr(server_mod, "AUTH_NONCE_TTL_S", 0)
    client, _db = env
    token = request_share(client).get_json()["token"]
    nonce = _challenge(client)
    # Windows' time.monotonic() ticks at ~15.6 ms under Python <=3.12; a
    # zero-TTL nonce only looks stale once the clock has actually advanced.
    time.sleep(0.05)
    r = _do_print_proof(client, token, nonce=nonce)
    assert r.status_code == 403
    assert r.get_json()["error"] == "invalid or expired challenge"


def test_legacy_token_rejected_after_removal(env):
    """1.0 removed the pre-0.3 X-Token path: senders get a clear upgrade
    error instead of a silent downgrade."""
    client, _db = env
    token = request_share(client).get_json()["token"]
    body = encrypt_payload(b"%PDF-1.4 x", token)
    r = client.post("/print",
                    headers={"X-Sender-ID": "111 222 333", "X-Token": token},
                    data={"file": (io.BytesIO(body), "doc.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 403
    assert "removed in PrintLink 1.0" in r.get_json()["error"]


def test_missing_credentials_403(env):
    client, _db = env
    request_share(client)
    r = client.post("/print",
                    headers={"X-Sender-ID": "111 222 333"},
                    data={"file": (io.BytesIO(b"x"), "d.pdf")},
                    content_type="multipart/form-data")
    assert r.status_code == 403


def test_revoke_grant_via_proof(env):
    client, db = env
    token = request_share(client).get_json()["token"]
    nonce = _challenge(client)
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333",
                          "printer_alias": "Accounting-HP",
                          "nonce": nonce,
                          "signature": sign_nonce(token, nonce)})
    assert r.status_code == 200 and r.get_json()["status"] == "revoked"
    assert db.list_grants()[0]["status"] == "revoked"
    # and the revoked grant can no longer print via proof either
    r2 = _do_print_proof(client, token)
    assert r2.status_code == 403
