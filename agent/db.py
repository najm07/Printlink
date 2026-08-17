"""PrintLink local database: SQLite storage for printers, shares, and pairing tokens.

Roles:
- This PC as HOST: rows in `shared_printers` (printers we expose) and `grants`
  (remote PCs allowed to print, with token + expiry).
- This PC as CLIENT: rows in `remote_printers` (printers we were granted access to).
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager


def remote_label(row) -> str:
    """Human label for a remote_printers row: '<alias> @ <name>' when the
    user named the receiver, else the classic '<alias> @ <host_id>'."""
    name = row["name"]
    if name:
        return f"{row['printer_alias']} @ {name}"
    return f"{row['printer_alias']} @ {row['host_id']}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_name TEXT NOT NULL,          -- Windows printer name on this PC
    alias TEXT NOT NULL,               -- friendly name shown to remotes
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(local_name)
);

CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_id TEXT NOT NULL,           -- normalized 9-digit ID of the allowed PC
    remote_name TEXT,                  -- hostname/user shown in the accept dialog
    printer_id INTEGER NOT NULL REFERENCES shared_printers(id) ON DELETE CASCADE,
    token TEXT NOT NULL,               -- pairing token the remote must present
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
    UNIQUE(remote_id, printer_id)
);

CREATE TABLE IF NOT EXISTS remote_printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    host_id TEXT NOT NULL,             -- normalized 9-digit ID of the host PC
    host_name TEXT,
    host_ip TEXT,                      -- last known IP (refreshed via mDNS)
    host_port INTEGER NOT NULL DEFAULT 9100,
    printer_alias TEXT NOT NULL,
    name TEXT,                           -- user-defined display name (client-side)
    token TEXT NOT NULL,               -- token the host issued to us
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
    UNIQUE(host_id, printer_alias)
);
"""


class Database:
    def __init__(self, db_path: Path | str):
        self.db_path = str(db_path)
        with self.connect() as con:
            con.executescript(SCHEMA)
            cols = {r[1] for r in con.execute("PRAGMA table_info(remote_printers)")}
            if "name" not in cols:
                con.execute("ALTER TABLE remote_printers ADD COLUMN name TEXT")
                con.commit()

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.db_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")  # shared DB across user accounts
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- host side: printers we share ----
    def add_shared_printer(self, local_name: str, alias: str) -> int:
        with self.connect() as con:
            cur = con.execute(
                "INSERT INTO shared_printers (local_name, alias) VALUES (?, ?)",
                (local_name, alias))
            return cur.lastrowid

    def list_shared_printers(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM shared_printers"
        if enabled_only:
            q += " WHERE enabled = 1"
        with self.connect() as con:
            return con.execute(q).fetchall()

    def update_shared_printer_alias(self, printer_id: int, alias: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE shared_printers SET alias = ? WHERE id = ?",
                        (alias, printer_id))

    def delete_shared_printer(self, printer_id: int) -> bool:
        """Unshare a printer; its grants cascade-delete (ON DELETE CASCADE)."""
        with self.connect() as con:
            cur = con.execute("DELETE FROM shared_printers WHERE id = ?", (printer_id,))
            return cur.rowcount > 0

    # ---- host side: grants to remote PCs ----
    def upsert_grant(self, remote_id, printer_id, token, expires_at, remote_name=None) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO grants (remote_id, remote_name, printer_id, token, expires_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(remote_id, printer_id) DO UPDATE SET
                       token=excluded.token, expires_at=excluded.expires_at,
                       remote_name=excluded.remote_name,
                       granted_at=datetime('now'), status='active'""",
                (remote_id, remote_name, printer_id, token, expires_at))

    def find_grant(self, remote_id: str, token: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM grants WHERE remote_id = ? AND token = ?",
                (remote_id, token)).fetchone()

    def find_grant_by_remote_and_alias(self, remote_id: str, printer_alias: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                """SELECT g.*, p.alias AS printer_alias
                   FROM grants g JOIN shared_printers p ON p.id = g.printer_id
                   WHERE g.remote_id = ? AND p.alias = ?""",
                (remote_id, printer_alias)).fetchone()

    def set_grant_status(self, grant_id: int, status: str) -> None:
        with self.connect() as con:
            con.execute("UPDATE grants SET status = ? WHERE id = ?", (status, grant_id))

    def list_grants(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT g.*, p.alias AS printer_alias FROM grants g JOIN shared_printers p ON p.id = g.printer_id"
        args = ()
        if status:
            q += " WHERE g.status = ?"
            args = (status,)
        with self.connect() as con:
            return con.execute(q, args).fetchall()

    # ---- client side: remote printers we can use ----
    def upsert_remote_printer(self, host_id, printer_alias, token, expires_at,
                              host_ip=None, host_name=None, host_port=9100,
                              name=None) -> None:
        with self.connect() as con:
            con.execute(
                """INSERT INTO remote_printers
                       (host_id, host_name, host_ip, host_port, printer_alias, name, token, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host_id, printer_alias) DO UPDATE SET
                       token=excluded.token, expires_at=excluded.expires_at,
                       host_ip=excluded.host_ip, host_name=excluded.host_name,
                       host_port=excluded.host_port,
                       granted_at=datetime('now'), status='active'""",
                (host_id, host_name, host_ip, host_port, printer_alias, name,
                 token, expires_at))

    def set_remote_printer_name(self, host_id: str, printer_alias: str,
                                name: str | None) -> None:
        with self.connect() as con:
            con.execute("UPDATE remote_printers SET name = ? WHERE host_id = ? AND printer_alias = ?",
                        (name, host_id, printer_alias))

    def delete_remote_printer(self, host_id: str, printer_alias: str) -> bool:
        with self.connect() as con:
            cur = con.execute("DELETE FROM remote_printers WHERE host_id = ? AND printer_alias = ?",
                              (host_id, printer_alias))
            return cur.rowcount > 0

    def get_remote_printer(self, host_id: str, printer_alias: str) -> sqlite3.Row | None:
        with self.connect() as con:
            return con.execute(
                "SELECT * FROM remote_printers WHERE host_id = ? AND printer_alias = ?",
                (host_id, printer_alias)).fetchone()

    def list_remote_printers(self, status: str | None = "active") -> list[sqlite3.Row]:
        q = "SELECT * FROM remote_printers"
        args = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        with self.connect() as con:
            return con.execute(q, args).fetchall()

    def update_remote_host_ip(self, host_id: str, ip: str, port: int = 9100) -> None:
        with self.connect() as con:
            con.execute("UPDATE remote_printers SET host_ip = ?, host_port = ? WHERE host_id = ?",
                        (ip, port, host_id))
