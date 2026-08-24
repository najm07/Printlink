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


def test_alias_must_be_unique(db):
    """1.0: duplicate aliases made grant lookups by alias ambiguous."""
    db.add_shared_printer("HP-1", "Accounting")
    with pytest.raises(ValueError):
        db.add_shared_printer("HP-2", "Accounting")
    with pytest.raises(ValueError):          # case-insensitive
        db.add_shared_printer("HP-2", "ACCOUNTING")
    # rename into an existing alias is refused too
    pid = db.add_shared_printer("HP-3", "Free")
    with pytest.raises(ValueError):
        db.update_shared_printer_alias(pid, "accounting")


def test_migration_renames_duplicate_aliases(tmp_path):
    """Old dbs without the constraint: later duplicates get 'X (2)', 'X (3)'."""
    import sqlite3
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    # faithful pre-0.3 combined layout (migrate_split runs first)
    con.executescript("""
        CREATE TABLE shared_printers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_name TEXT NOT NULL,
            alias TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(local_name));
        CREATE TABLE grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            remote_id TEXT NOT NULL,
            remote_name TEXT,
            printer_id INTEGER NOT NULL REFERENCES shared_printers(id),
            token TEXT NOT NULL,
            granted_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(remote_id, printer_id));
        CREATE TABLE remote_printers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host_id TEXT NOT NULL,
            host_name TEXT,
            host_ip TEXT,
            host_port INTEGER NOT NULL DEFAULT 9100,
            printer_alias TEXT NOT NULL,
            token TEXT NOT NULL,
            granted_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE(host_id, printer_alias));
    """)
    con.execute("INSERT INTO shared_printers (local_name, alias) VALUES ('A', 'Front')")
    con.execute("INSERT INTO shared_printers (local_name, alias) VALUES ('B', 'front')")
    con.execute("INSERT INTO shared_printers (local_name, alias) VALUES ('C', 'Front')")
    con.commit()
    con.close()

    db = Database(path, tmp_path / "p.db")   # split + _ensure_unique_alias
    # later duplicates get suffixed, keeping their own casing
    assert sorted(r["alias"] for r in db.list_shared_printers(enabled_only=False)) \
        == ["Front", "Front (3)", "front (2)"]
    # the rebuilt table enforces uniqueness case-insensitively
    with pytest.raises(ValueError):
        db.add_shared_printer("D", "FRONT")
    assert db.add_shared_printer("D", "Reception") > 0


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
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00",
                    printer_alias="Accounting-HP")
    g = db.find_grant_by_remote_and_alias("111222333", "Accounting-HP")
    assert g is not None and g["token"] == "tok"
    assert db.find_grant_by_remote_and_alias("111222333", "Ghost") is None
    assert db.find_grant_by_remote_and_alias("999999999", "Accounting-HP") is None


def test_remote_label_fallback(db):
    db.upsert_remote_printer("777888999", "Office-HP", "tok", "2030-01-01 00:00:00")
    assert remote_label(db.get_remote_printer("777888999", "Office-HP")) == "Office-HP @ 777888999"
    db.set_remote_printer_name("777888999", "Office-HP", "Reception")
    assert remote_label(db.get_remote_printer("777888999", "Office-HP")) == "Office-HP @ Reception"


def test_migration_splits_combined_database(tmp_path):
    """Pre-0.3 single-file layout -> shared printers stay machine-wide,
    token tables move to the per-user private file (grants gain a
    denormalized printer_alias from the same-file join)."""
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as con:
        con.executescript(
            """CREATE TABLE shared_printers (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   local_name TEXT NOT NULL,
                   alias TEXT NOT NULL,
                   enabled INTEGER NOT NULL DEFAULT 1,
                   created_at TEXT NOT NULL DEFAULT (datetime('now')),
                   UNIQUE(local_name));
               CREATE TABLE grants (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   remote_id TEXT NOT NULL,
                   remote_name TEXT,
                   printer_id INTEGER NOT NULL REFERENCES shared_printers(id) ON DELETE CASCADE,
                   token TEXT NOT NULL,
                   granted_at TEXT NOT NULL DEFAULT (datetime('now')),
                   expires_at TEXT NOT NULL,
                   status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
                   UNIQUE(remote_id, printer_id));
               CREATE TABLE remote_printers (
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
                   UNIQUE(host_id, printer_alias));""")
        con.execute("INSERT INTO shared_printers (local_name, alias) VALUES ('HP', 'acct')")
        con.execute("""INSERT INTO grants (remote_id, remote_name, printer_id,
                       token, expires_at) VALUES ('111222333', 'A', 1, 'tok', '2030-01-01')""")
        con.execute("""INSERT INTO remote_printers (host_id, printer_alias, token,
                       expires_at) VALUES ('444555666', 'office-hp', 'tok2', '2030-01-01')""")

    db = Database(path)
    private = tmp_path / "old-private.db"

    with sqlite3.connect(db.shared_path) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"grants", "remote_printers"} & tables)
        assert "shared_printers" in tables
        assert con.execute("SELECT COUNT(*) FROM shared_printers").fetchone()[0] == 1

    with sqlite3.connect(private) as con:
        con.row_factory = sqlite3.Row
        g = con.execute("SELECT * FROM grants WHERE remote_id='111222333'").fetchone()
        assert g["token"] == "tok" and g["printer_alias"] == "acct"
        rp = con.execute(
            "SELECT * FROM remote_printers WHERE host_id='444555666'").fetchone()
        assert rp["token"] == "tok2"
        cols = {r[1] for r in con.execute("PRAGMA table_info(remote_printers)")}
        assert "name" in cols            # fresh private schema includes it

    # and the migrated data keeps working through the normal API
    assert db.find_grant("111222333", "tok")["printer_alias"] == "acct"


def test_split_routing_tables_in_right_files(tmp_path):
    shared_only = tmp_path / "s.db"
    db = Database(shared_only, tmp_path / "p.db")
    db.add_shared_printer("HP", "a")
    import sqlite3
    with sqlite3.connect(shared_only) as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert not ({"grants", "remote_printers"} & tables)
    with sqlite3.connect(tmp_path / "p.db") as con:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"grants", "remote_printers"} <= tables


def test_alias_rename_syncs_denormalized_grants(db):
    """Grants carry an alias snapshot (cross-file JOIN is impossible since
    the split); renaming must keep them findable under the new alias."""
    pid = db.add_shared_printer("HP", "Old-Alias")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00",
                    printer_alias="Old-Alias")
    db.update_shared_printer_alias(pid, "New-Alias")
    assert db.find_grant_by_remote_and_alias("111222333", "New-Alias") is not None
    assert db.find_grant_by_remote_and_alias("111222333", "Old-Alias") is None


def test_cascade_delete_removes_grants(db):
    pid = db.add_shared_printer("HP", "a")
    db.upsert_grant("111222333", pid, "tok", "2030-01-01 00:00:00",
                    printer_alias="a")
    # manual cascade lives in the method (cross-file FKs don't exist)
    assert db.delete_shared_printer(pid)
    assert db.list_grants() == []
