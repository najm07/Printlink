from datetime import datetime, timedelta, timezone
import pytest
from db import Database
from shares import (create_grant, authorize_print, revoke_grant,
                    sweep_expired_grants, store_accepted_share,
                    get_usable_printer, extend_grant, revoke_remote_share)

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
    auth = authorize_print(db, "111-222-333", g["token"])  # ID variants work
    assert auth["ok"] and auth["printer"]["alias"] == "Accounting-HP"


def test_wrong_token_and_unknown_id(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    assert not authorize_print(db, "111 222 333", "bad")["ok"]
    assert not authorize_print(db, "999 999 999", g["token"])["ok"]


def test_revoke(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    revoke_grant(db, db.list_grants()[0]["id"])
    assert authorize_print(db, "111 222 333", g["token"])["error"] == "share was revoked"


def test_expiry_enforced_and_swept(setup):
    db, pid = setup
    g = create_grant(db, "444 555 666", "Sara-PC", pid)
    yesterday = (_utcnow() - timedelta(days=1)).strftime(EXP)
    with db.connect() as con:
        con.execute("UPDATE grants SET expires_at = ? WHERE remote_id = '444555666'", (yesterday,))
    assert sweep_expired_grants(db) == 1
    assert authorize_print(db, "444 555 666", g["token"])["error"] == "share expired"
    assert sweep_expired_grants(db) == 0  # already marked


def test_unshared_printer_denies(setup):
    db, pid = setup
    g = create_grant(db, "111 222 333", "Ahmed-PC", pid)
    with db.connect() as con:
        con.execute("UPDATE shared_printers SET enabled = 0 WHERE id = ?", (pid,))
    assert authorize_print(db, "111 222 333", g["token"])["error"] == "printer no longer shared"


def test_client_side_checks(tmp_path):
    db = Database(tmp_path / "t.db")
    exp = (_utcnow() + timedelta(days=7)).strftime(EXP)
    store_accepted_share(db, "777 888 999", "Host-PC", "192.168.1.50", "Office-HP", "tok", exp)
    assert get_usable_printer(db, "777 888 999", "Office-HP")["ok"]
    assert not get_usable_printer(db, "777 888 999", "Nope")["ok"]
    past = (_utcnow() - timedelta(days=1)).strftime(EXP)
    store_accepted_share(db, "777 888 999", "Host-PC", "192.168.1.50", "Office-HP", "tok", past)
    assert get_usable_printer(db, "777 888 999", "Office-HP")["error"].startswith("share expired")


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
    assert authorize_print(db, "111222333", "tok")["error"].startswith("share expired")
    new_expiry = extend_grant(db, g["id"], 30)
    assert new_expiry > past
    assert authorize_print(db, "111222333", "tok")["ok"]
    assert db.list_grants()[0]["expires_at"] == new_expiry


def test_revoke_remote_share(tmp_path):
    db = Database(tmp_path / "t.db")
    pid = db.add_shared_printer("HP", "Accounting-HP")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00")
    assert revoke_remote_share(db, "111222333", "Accounting-HP", "wrong")["error"] == "token mismatch"
    assert revoke_remote_share(db, "111222333", "Ghost", "tok")["error"].startswith("no grant")
    assert revoke_remote_share(db, "111222333", "Accounting-HP", "tok")["ok"]
    assert db.list_grants()[0]["status"] == "revoked"
