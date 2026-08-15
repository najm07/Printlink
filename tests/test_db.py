import pytest
from db import Database, remote_label


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


def test_remote_printer_name_and_rename(db):
    db.upsert_remote_printer("777888999", "Office-HP", "tok", "2030-01-01 00:00:00",
                             name="Reception Canon")
    rp = db.get_remote_printer("777888999", "Office-HP")
    assert rp["name"] == "Reception Canon"
    db.set_remote_printer_name("777888999", "Office-HP", "Upstairs Canon")
    assert db.get_remote_printer("777888999", "Office-HP")["name"] == "Upstairs Canon"
    db.set_remote_printer_name("777888999", "Office-HP", None)
    assert db.get_remote_printer("777888999", "Office-HP")["name"] is None


def test_delete_remote_printer(db):
    db.upsert_remote_printer("777888999", "Office-HP", "tok", "2030-01-01 00:00:00")
    assert db.delete_remote_printer("777888999", "Office-HP")
    assert not db.delete_remote_printer("777888999", "Office-HP")  # already gone
    assert db.list_remote_printers(status=None) == []


def test_shared_printer_rename_and_unshare(db):
    pid = db.add_shared_printer("HP LaserJet Pro", "Accounting-HP")
    db.update_shared_printer_alias(pid, "Upstairs-HP")
    assert db.list_shared_printers()[0]["alias"] == "Upstairs-HP"
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00")
    assert db.delete_shared_printer(pid)
    assert db.list_shared_printers(enabled_only=False) == []
    assert db.list_grants() == []  # cascade


def test_find_grant_by_remote_and_alias(db):
    pid = db.add_shared_printer("HP", "Accounting-HP")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00")
    g = db.find_grant_by_remote_and_alias("111222333", "Accounting-HP")
    assert g is not None and g["token"] == "tok"
    assert db.find_grant_by_remote_and_alias("111222333", "Ghost") is None
    assert db.find_grant_by_remote_and_alias("999999999", "Accounting-HP") is None


def test_remote_label_fallback(db):
    db.upsert_remote_printer("777888999", "Office-HP", "tok", "2030-01-01 00:00:00")
    assert remote_label(db.get_remote_printer("777888999", "Office-HP")) == "Office-HP @ 777888999"
    db.set_remote_printer_name("777888999", "Office-HP", "Reception")
    assert remote_label(db.get_remote_printer("777888999", "Office-HP")) == "Office-HP @ Reception"


def test_migration_adds_name_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.executescript(
            """CREATE TABLE remote_printers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   host_id TEXT NOT NULL,
                   host_name TEXT,
                   host_ip TEXT,
                   host_port INTEGER NOT NULL DEFAULT 9100,
                   printer_alias TEXT NOT NULL,
                   token TEXT NOT NULL,
                   granted_at TEXT NOT NULL DEFAULT (datetime('now')),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
                   UNIQUE(host_id, printer_alias)
               );
               CREATE TABLE shared_printers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   local_name TEXT NOT NULL,
                   alias TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   UNIQUE(local_name)
               );
               CREATE TABLE grants (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   remote_id TEXT NOT NULL,
                   remote_name TEXT,
                   printer_id INTEGER NOT NULL REFERENCES shared_printers(id) ON DELETE CASCADE,
                   token TEXT NOT NULL,
                   granted_at TEXT NOT NULL DEFAULT (datetime('now')),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
                   UNIQUE(remote_id, printer_id)
               );""")
    migrated = Database(path)
    with migrated.connect() as con:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(remote_printers)")}
    assert "name" in cols


def test_cascade_delete_removes_grants(db):
    pid = db.add_shared_printer("HP", "a")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00")
    with db.connect() as con:
        con.execute("DELETE FROM shared_printers WHERE id = ?", (pid,))
    assert db.list_grants() == []
