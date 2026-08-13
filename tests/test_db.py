import pytest
from db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


def test_add_and_list_shared(db):
    pid = db.add_shared_printer("HP LaserJet Pro", "Accounting-HP")
    rows = db.list_shared_printers()
    assert len(rows) == 1 and rows[0]["id"] == pid
    assert rows[0]["local_name"] == "HP LaserJet Pro"


def test_unique_local_name(db):
    db.add_shared_printer("HP", "a")
    with pytest.raises(Exception):
        db.add_shared_printer("HP", "b")


def test_grant_upsert_rotates_token(db):
    pid = db.add_shared_printer("HP", "a")
    db.upsert_grant("111222333", pid, "tok1", "2030-01-01 00:00:00")
    db.upsert_grant("111222333", pid, "tok2", "2031-01-01 00:00:00")
    grants = db.list_grants()
    assert len(grants) == 1 and grants[0]["token"] == "tok2"
    assert grants[0]["expires_at"] == "2031-01-01 00:00:00"


def test_find_grant(db):
    pid = db.add_shared_printer("HP", "a")
    db.upsert_grant("111222333", pid, "tok1", "2030-01-01 00:00:00")
    assert db.find_grant("111222333", "tok1") is not None
    assert db.find_grant("111222333", "nope") is None
    assert db.find_grant("999999999", "tok1") is None


def test_remote_printer_upsert_and_get(db):
    db.upsert_remote_printer("777888999", "Office-HP", "tok", "2030-01-01 00:00:00",
                             host_ip="192.168.1.50")
    db.upsert_remote_printer("777888999", "Office-HP", "tok2", "2031-01-01 00:00:00",
                             host_ip="192.168.1.60")
    rp = db.get_remote_printer("777888999", "Office-HP")
    assert rp["token"] == "tok2" and rp["host_ip"] == "192.168.1.60"
    assert len(db.list_remote_printers(status=None)) == 1


def test_cascade_delete_removes_grants(db):
    pid = db.add_shared_printer("HP", "a")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00")
    with db.connect() as con:
        con.execute("DELETE FROM shared_printers WHERE id = ?", (pid,))
    assert db.list_grants() == []
