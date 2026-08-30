"""PrintLink databases: SQLite storage, split by secretiveness (0.3+).

Two files, two trust levels (see docs/security.md):

- SHARED db (`printlink.db`, %PROGRAMDATA%\\PrintLink) — machine-wide,
  read-mostly: `shared_printers`. Every Windows account must see the same
  printer list; nothing secret lives here anymore.
- PRIVATE db (`printlink-private.db`, per-user %LOCALAPPDATA%\\PrintLink)
  — `grants` and `remote_printers`. These tables hold pairing tokens; the
  DB split means a local user can no longer read another account's tokens
  or inject grants into it.

The split breaks cross-file JOINs and FOREIGN KEYs, so:

- grants carry a denormalized `printer_alias` snapshot (kept in sync on
  alias rename), and
- unsharing a printer cascade-deletes its grants in application code
  (delete_shared_printer), not via ON DELETE CASCADE.

`Database.__init__` migrates a pre-0.3 combined single-file layout on
first open: token tables move to the private file, legacy tables are then
dropped from the shared one. The copy runs in one transaction before any
drop, so an interrupted migration leaves both sources intact.
"""
import sqlite3
from pathlib import Path
from contextlib import contextmanager

from logutil import get_logger

log = get_logger("db")


def remote_label(row) -> str:
    """Human label for a remote_printers row: '<alias> @ <name>' when the
    user named the receiver, else the classic '<alias> @ <host_id>'."""
    name = row["name"]
    if name:
        return f"{row['printer_alias']} @ {name}"
    return f"{row['printer_alias']} @ {row['host_id']}"


SHARED_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_printers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    local_name TEXT NOT NULL,          -- Windows printer name on this PC
    alias TEXT NOT NULL COLLATE NOCASE UNIQUE,  -- friendly name shown to remotes
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(local_name)
);
"""

PRIVATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remote_id TEXT NOT NULL,           -- normalized 9-digit ID of the allowed PC
    remote_name TEXT,                  -- hostname/user shown in the accept dialog
    printer_id INTEGER NOT NULL,       -- shared db's shared_printers.id (manual FK)
    printer_alias TEXT NOT NULL,       -- denormalized snapshot, synced on rename
    token TEXT NOT NULL,               -- pairing token the remote must prove
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
    tls_fp TEXT,                         -- pinned SHA256 of the host certificate
    token TEXT NOT NULL,               -- token the host issued to us
    granted_at TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','expired','revoked')),
    UNIQUE(host_id, printer_alias)
);
"""


def _table_names(con: sqlite3.Connection) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _copy_table(src: sqlite3.Connection, dst_con: sqlite3.Connection,
                insert_sql: str, select_sql: str) -> int:
    """Copy rows from the pre-0.3 combined file into a split table."""
    n = 0
    for row in src.execute(select_sql):
        dst_con.execute(insert_sql, tuple(row))
        n += 1
    return n


def migrate_split(old_path: Path, shared_con: sqlite3.Connection,
                  private_con: sqlite3.Connection) -> bool:
    """Move a pre-0.3 combined db into the split layout.

    shared_printers rows go through shared_con, token tables through
    private_con. Both sides commit together; any failure rolls both back
    so an interrupted migration leaves the original untouched."""
    if not old_path.exists():
        return False
    src = sqlite3.connect(str(old_path))
    src.row_factory = sqlite3.Row
    try:
        names = _table_names(src)
        if "grants" not in names and "remote_printers" not in names:
            return False   # already split (or foreign file): nothing to do

        shared_con.execute("BEGIN")
        private_con.execute("BEGIN")
        try:
            _copy_table(
                src, shared_con,
                """INSERT OR IGNORE INTO shared_printers
                   (id, local_name, alias, enabled, created_at)
                   VALUES (?,?,?,?,?)""",
                "SELECT id, local_name, alias, enabled, created_at "
                "FROM shared_printers")

            # Explicit column mapping: old grant rows lack printer_alias,
            # so the denormalized snapshot comes from a same-file join.
            if "grants" in names:
                _copy_table(
                    src, private_con,
                    """INSERT OR IGNORE INTO grants
                       (id, remote_id, remote_name, printer_id,
                        printer_alias, token, granted_at, expires_at, status)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    """SELECT g.id, g.remote_id, g.remote_name, g.printer_id,
                              COALESCE(p.alias, ''), g.token, g.granted_at,
                              g.expires_at, g.status
                       FROM grants g LEFT JOIN shared_printers p
                            ON p.id = g.printer_id""")

            if "remote_printers" in names:
                cols = {r[1] for r in src.execute(
                    "PRAGMA table_info(remote_printers)")}
                has_name = "name" in cols
                for row in src.execute("SELECT * FROM remote_printers"):
                    private_con.execute(
                        """INSERT OR IGNORE INTO remote_printers
                           (id, host_id, host_name, host_ip, host_port,
                            printer_alias, name, token, granted_at,
                            expires_at, status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                        (row["id"], row["host_id"], row["host_name"],
                         row["host_ip"], row["host_port"],
                         row["printer_alias"],
                         row["name"] if has_name else None,
                         row["token"], row["granted_at"],
                         row["expires_at"], row["status"]))

            shared_con.execute("COMMIT")
            private_con.execute("COMMIT")
            return True
        except Exception:
            shared_con.execute("ROLLBACK")
            private_con.execute("ROLLBACK")
            raise
    finally:
        src.close()


def _has_unique_alias_index(con: sqlite3.Connection) -> bool:
    for row in con.execute("PRAGMA index_list('shared_printers')"):
        if row["unique"]:
            cols = [r["name"] for r in con.execute(
                f"PRAGMA index_info('{row['name']}')")]
            if cols == ["alias"]:
                return True
    return False


def _ensure_unique_alias(con: sqlite3.Connection) -> None:
    """1.0 upgrade: aliases must be unique (case-insensitive) — duplicates
    made grant lookups by alias ambiguous. Old dbs get a table rebuild;
    colliding aliases are renamed 'X (2)', 'X (3)', ... oldest wins."""
    if _has_unique_alias_index(con):
        return
    seen: set[str] = set()
    for row in con.execute(
            "SELECT id, alias FROM shared_printers ORDER BY id").fetchall():
        alias, base, n = row["alias"], row["alias"], 2
        while alias.casefold() in seen:
            alias = f"{base} ({n})"
            n += 1
        seen.add(alias.casefold())
        if alias != row["alias"]:
            log.warning("duplicate shared alias renamed: '%s' -> '%s'",
                        row["alias"], alias)
            con.execute("UPDATE shared_printers SET alias = ? WHERE id = ?",
                        (alias, row["id"]))
    con.executescript("""
        CREATE TABLE shared_printers_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            local_name TEXT NOT NULL,
            alias TEXT NOT NULL COLLATE NOCASE UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(local_name)
        );
        INSERT INTO shared_printers_new SELECT * FROM shared_printers;
        DROP TABLE shared_printers;
        ALTER TABLE shared_printers_new RENAME TO shared_printers;
    """)


class Database:
    def __init__(self, shared_path, private_path=None):
        shared_path = Path(shared_path)
        self.shared_path = str(shared_path)
        if private_path is None:
            private_path = shared_path.with_name(
                shared_path.stem + "-private" + shared_path.suffix)
        self.private_path = str(private_path)

        # One-time split of a pre-0.3 combined file. Only after BOTH sides
        # committed do we drop the legacy token tables from the shared one.
        with self.connect_shared() as scon, self.connect_private() as pcon:
            pcon.executescript(PRIVATE_SCHEMA)
            migrated = migrate_split(shared_path, scon, pcon)

        with self.connect_shared() as con:
            con.executescript(SHARED_SCHEMA)
            _ensure_unique_alias(con)
            if migrated:
                # Tokens now live in the private file — remove the copies.
                for t in _table_names(con) & {"grants", "remote_printers"}:
                    con.execute(f"DROP TABLE IF EXISTS {t}")

        with self.connect_private() as con:
            cols = {r[1] for r in con.execute("PRAGMA table_info(remote_printers)")}
            for col in ("name", "tls_fp"):
                if col not in cols:
                    con.execute(f"ALTER TABLE remote_printers ADD COLUMN {col} TEXT")

    @contextmanager
    def connect_shared(self):
        con = sqlite3.connect(self.shared_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")  # shared across accounts
        try:
            yield con
            con.commit()
        finally:
            con.close()

    @contextmanager
    def connect_private(self):
        Path(self.private_path).parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.private_path, timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 10000")
        try:
            yield con
            con.commit()
        finally:
            con.close()

    # ---- host side: printers we share (SHARED db) ----
    def add_shared_printer(self, local_name: str, alias: str) -> int:
        try:
            with self.connect_shared() as con:
                cur = con.execute(
                    "INSERT INTO shared_printers (local_name, alias) VALUES (?, ?)",
                    (local_name, alias))
                return cur.lastrowid
        except sqlite3.IntegrityError as e:
            raise ValueError("That alias is already in use — pick another.") from e

    def list_shared_printers(self, enabled_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM shared_printers"
        if enabled_only:
            q += " WHERE enabled = 1"
        with self.connect_shared() as con:
            return con.execute(q).fetchall()

    def get_enabled_printer(self, printer_id: int) -> sqlite3.Row | None:
        with self.connect_shared() as con:
            return con.execute(
                "SELECT * FROM shared_printers WHERE id = ? AND enabled = 1",
                (printer_id,)).fetchone()

    def update_shared_printer_alias(self, printer_id: int, alias: str) -> None:
        try:
            with self.connect_shared() as con:
                con.execute("UPDATE shared_printers SET alias = ? WHERE id = ?",
                            (alias, printer_id))
        except sqlite3.IntegrityError as e:
            raise ValueError("That alias is already in use — pick another.") from e
        # keep the denormalized snapshot in grants in sync (private db)
        with self.connect_private() as con:
            con.execute("UPDATE grants SET printer_alias = ? WHERE printer_id = ?",
                        (alias, printer_id))

    def delete_shared_printer(self, printer_id: int) -> bool:
        """Unshare a printer. Cross-file FKs don't exist, so the grant
        cascade is manual (application-level ON DELETE CASCADE)."""
        with self.connect_shared() as con:
            cur = con.execute("DELETE FROM shared_printers WHERE id = ?",
                              (printer_id,))
            deleted = cur.rowcount > 0
        if deleted:
            with self.connect_private() as con:
                con.execute("DELETE FROM grants WHERE printer_id = ?",
                            (printer_id,))
        return deleted

    # ---- host side: grants (PRIVATE db) ----
    def upsert_grant(self, remote_id, printer_id, token, expires_at,
                     remote_name=None, printer_alias=None) -> None:
        with self.connect_private() as con:
            con.execute(
                """INSERT INTO grants (remote_id, remote_name, printer_id,
                                       printer_alias, token, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(remote_id, printer_id) DO UPDATE SET
                       token=excluded.token, expires_at=excluded.expires_at,
                       remote_name=excluded.remote_name,
                       printer_alias=COALESCE(excluded.printer_alias, grants.printer_alias),
                       granted_at=datetime('now'), status='active'""",
                (remote_id, remote_name, printer_id, printer_alias or "",
                 token, expires_at))

    def find_grant(self, remote_id: str, token: str) -> sqlite3.Row | None:
        with self.connect_private() as con:
            return con.execute(
                "SELECT * FROM grants WHERE remote_id = ? AND token = ?",
                (remote_id, token)).fetchone()

    def list_grants_for_remote(self, remote_id: str,
                               status: str | None = "active") -> list[sqlite3.Row]:
        """All of one remote's grants — the HMAC path probes these by hint."""
        q = "SELECT * FROM grants WHERE remote_id = ?"
        args: tuple = (remote_id,)
        if status:
            q += " AND status = ?"
            args = (remote_id, status)
        with self.connect_private() as con:
            return con.execute(q, args).fetchall()

    def find_grants_by_remote_and_alias(self, remote_id: str,
                                        printer_alias: str) -> list[sqlite3.Row]:
        with self.connect_private() as con:
            return con.execute(
                "SELECT * FROM grants WHERE remote_id = ? AND printer_alias = ?",
                (remote_id, printer_alias)).fetchall()

    def find_grant_by_remote_and_alias(self, remote_id: str,
                                       printer_alias: str) -> sqlite3.Row | None:
        rows = self.find_grants_by_remote_and_alias(remote_id, printer_alias)
        return rows[0] if rows else None

    def set_grant_status(self, grant_id: int, status: str) -> None:
        with self.connect_private() as con:
            con.execute("UPDATE grants SET status = ? WHERE id = ?",
                        (status, grant_id))

    def list_grants(self, status: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT *, printer_alias AS printer_alias FROM grants"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        with self.connect_private() as con:
            return con.execute(q, args).fetchall()

    # ---- client side: remote printers (PRIVATE db) ----
    def upsert_remote_printer(self, host_id, printer_alias, token, expires_at,
                              host_ip=None, host_name=None, host_port=9100,
                              name=None, tls_fp=None) -> None:
        with self.connect_private() as con:
            con.execute(
                """INSERT INTO remote_printers
                       (host_id, host_name, host_ip, host_port, printer_alias,
                        name, tls_fp, token, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(host_id, printer_alias) DO UPDATE SET
                       token=excluded.token, expires_at=excluded.expires_at,
                       host_ip=excluded.host_ip, host_name=excluded.host_name,
                       host_port=excluded.host_port,
                       tls_fp=COALESCE(excluded.tls_fp, remote_printers.tls_fp),
                       granted_at=datetime('now'), status='active'""",
                (host_id, host_name, host_ip, host_port, printer_alias, name,
                 tls_fp, token, expires_at))

    def set_remote_printer_name(self, host_id: str, printer_alias: str,
                                name: str | None) -> None:
        with self.connect_private() as con:
            con.execute("UPDATE remote_printers SET name = ? WHERE host_id = ? AND printer_alias = ?",
                        (name, host_id, printer_alias))

    def delete_remote_printer(self, host_id: str, printer_alias: str) -> bool:
        with self.connect_private() as con:
            cur = con.execute("DELETE FROM remote_printers WHERE host_id = ? AND printer_alias = ?",
                              (host_id, printer_alias))
            return cur.rowcount > 0

    def get_remote_printer(self, host_id: str, printer_alias: str) -> sqlite3.Row | None:
        with self.connect_private() as con:
            return con.execute(
                "SELECT * FROM remote_printers WHERE host_id = ? AND printer_alias = ?",
                (host_id, printer_alias)).fetchone()

    def list_remote_printers(self, status: str | None = "active") -> list[sqlite3.Row]:
        q = "SELECT * FROM remote_printers"
        args: tuple = ()
        if status:
            q += " WHERE status = ?"
            args = (status,)
        with self.connect_private() as con:
            return con.execute(q, args).fetchall()

    def update_remote_host_ip(self, host_id: str, ip: str, port: int = 9100) -> None:
        from identity import normalize_id
        host_id = normalize_id(host_id)
        with self.connect_private() as con:
            # host_id may be stored with or without spaces (pre-1.0 rows);
            # match either form so the update never silently does nothing.
            con.execute("UPDATE remote_printers SET host_ip = ?, host_port = ? "
                        "WHERE REPLACE(host_id, ' ', '') = REPLACE(?, ' ', '')",
                        (ip, port, host_id))

    def update_remote_tls_fp(self, host_id: str, tls_fp: str) -> None:
        from identity import normalize_id
        host_id = normalize_id(host_id)
        with self.connect_private() as con:
            con.execute("UPDATE remote_printers SET tls_fp = ? "
                        "WHERE REPLACE(host_id, ' ', '') = REPLACE(?, ' ', '')",
                        (tls_fp, host_id))
