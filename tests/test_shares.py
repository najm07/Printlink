from datetime import datetime, timedelta, timezone
import pytest
from db import Database
from shares import (create_grant, authorize_print_proof,
                    revoke_grant, sweep_expired_grants, store_accepted_share,
                    get_usable_printer, extend_grant,
                    revoke_remote_share_proof)
from auth import sign_nonce, token_hint


def _auth(db, remote_id: str, token: str, nonce: str = "n"):
    """Proof-path stand-in for the removed legacy authorize_print."""
    return authorize_print_proof(db, remote_id, token_hint(token),
                                 nonce, sign_nonce(token, nonce))

EXP = "%Y-%m-%d %H:%M:%S"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def setup(tmp_path):
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP LaserJet Pro", "Accounting-HP")
    return db, pid


def test_full_grant_lifecycle(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid, days=7)
    assert len(g["token"]) == 64
    auth = _auth(db, "111-222-333", g["token"], nonce="v1")  # ID variants work
    assert auth["ok"] and auth["printer"]["alias"] == "Accounting-HP"


def test_wrong_token_and_unknown_id(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    assert not _auth(db, "111 222 333", "bad")["ok"]
    assert not _auth(db, "999 999 999", g["token"])["ok"]


def test_revoke(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    revoke_grant(db, db.list_grants()[0]["id"])
    assert _auth(db, "111 222 333", g["token"])["error"] == "share was revoked"


def test_expiry_enforced_and_swept(setup):
    db, pid = setup
    g = create_grant(db, "444 555 666", "Sara-PC", pid)
    yesterday = (_utcnow() - timedelta(days=1)).strftime(EXP)
    with db.connect_private() as con:
        con.execute("UPDATE grants SET expires_at = ? WHERE remote_id = '444555666'", (yesterday,))
    assert sweep_expired_grants(db) == 1
    assert _auth(db, "444 555 666", g["token"])["error"] == "share expired"
    assert sweep_expired_grants(db) == 0  # already marked


def test_unshared_printer_denies(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    with db.connect_shared() as con:
        con.execute("UPDATE shared_printers SET enabled = 0 WHERE id = ?", (pid,))
    assert _auth(db, "111 222 333", g["token"])["error"] == "printer no longer shared"


def test_client_side_checks(tmp_path):
    """0.3+ contract: local time-expiry is advisory (host decides on the
    real request); only missing/revoked entries block pre-flight."""
    db = Database(tmp_path / "t.db")
    exp = (_utcnow() + timedelta(days=7)).strftime(EXP)
    store_accepted_share(db, "777 888 999", "Host-PC", "192.168.1.50", "Office-HP", "tok", exp)
    assert get_usable_printer(db, "777 888 999", "Office-HP")["ok"]
    assert not get_usable_printer(db, "777 888 999", "Nope")["ok"]
    past = (_utcnow() - timedelta(days=1)).strftime(EXP)
    store_accepted_share(db, "777 888 999", "Host-PC", "192.168.1.50", "Office-HP", "tok", past)
    res = get_usable_printer(db, "777 888 999", "Office-HP")
    assert res["ok"] and res.get("stale") is True
    # revoked still hard-blocks
    with db.connect_private() as con:
        con.execute("UPDATE remote_printers SET status='revoked'")
    assert get_usable_printer(db, "777 888 999", "Office-HP")["error"] == "share was revoked"


def test_store_accepted_share_with_name(tmp_path):
    db = Database(tmp_path / "t.db")
    exp = (_utcnow() + timedelta(days=7)).strftime(EXP)
    store_accepted_share(db, "777 888 999", "Host-PC", "192.168.1.50",
                         "Office-HP", "tok", exp, name="Reception Canon")
    rp = db.get_remote_printer("777888999", "Office-HP")
    assert rp["name"] == "Reception Canon"


def test_extend_grant_reactivates(tmp_path):
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP", "a")
    past = (_utcnow() - timedelta(days=2)).strftime(EXP)
    db.upsert_grant("111222333", pid, "tok", past)
    g = db.list_grants()[0]
    assert _auth(db, "111222333", "tok")["error"].startswith("share expired")
    new_expiry = extend_grant(db, g["id"], 30)
    assert new_expiry > past
    assert _auth(db, "111222333", "tok")["ok"]
    assert db.list_grants()[0]["expires_at"] == new_expiry


def test_revoke_remote_share(tmp_path):
    """1.0: only the HMAC proof path exists; a body token is refused."""
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP", "Accounting-HP")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00",
                    printer_alias="Accounting-HP")
    bad_sig = revoke_remote_share_proof(db, "111222333", "Accounting-HP",
                                        "n1", sign_nonce("other", "n1"))
    assert bad_sig["error"] == "no matching grant or bad signature"
    ghost = revoke_remote_share_proof(db, "111222333", "Ghost",
                                      "n2", sign_nonce("tok", "n2"))
    assert ghost["error"] == "no matching grant or bad signature"
    ok = revoke_remote_share_proof(db, "111222333", "Accounting-HP",
                                   "n3", sign_nonce("tok", "n3"))
    assert ok == {"ok": True}
    assert db.list_grants()[0]["status"] == "revoked"


def test_revoke_remote_share_proof(tmp_path):
    from auth import sign_nonce
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP", "Accounting-HP")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00",
                    printer_alias="Accounting-HP")
    bad = revoke_remote_share_proof(db, "111222333", "Accounting-HP",
                                    "nonce", sign_nonce("other", "nonce"))
    assert bad["error"] == "no matching grant or bad signature"
    ok = revoke_remote_share_proof(db, "111222333", "Accounting-HP",
                                   "nonce", sign_nonce("tok", "nonce"))
    assert ok == {"ok": True}
    assert db.list_grants()[0]["status"] == "revoked"


def test_authorize_print_proof(tmp_path):
    """0.3 gate: hint routes to the grant, HMAC proof over a fresh nonce
    authenticates; wrong proof/hint finds nothing."""
    from auth import token_hint
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP", "Accounting-HP")
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    res = authorize_print_proof(db, "111222333",
                                token_hint(g["token"]), "n1",
                                sign_nonce(g["token"], "n1"))
    assert res["ok"] and res["printer"]["alias"] == "Accounting-HP"
    bad = authorize_print_proof(db, "111222333",
                                token_hint(g["token"]), "n2",
                                sign_nonce("wrong-token", "n2"))
    assert not bad["ok"]
    assert not authorize_print_proof(db, "999999999",
                                     token_hint(g["token"]), "n3",
                                     sign_nonce(g["token"], "n3"))["ok"]
