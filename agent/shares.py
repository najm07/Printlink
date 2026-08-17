"""PrintLink share logic: request/accept/revoke/weekly-expiry.

Host side = the PC with the physical printer.
Client side = the PC asking to print remotely.

All datetimes are UTC 'YYYY-MM-DD HH:MM:SS' strings (matches SQLite datetime()).
"""
from datetime import datetime, timedelta, timezone

from db import Database
from identity import normalize_id
from config import DEFAULT_SHARE_DAYS
from crypto import new_pairing_token


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------- host side ----------

def create_grant(db: Database, remote_id: str, remote_name: str, printer_id: int,
                 days: int = DEFAULT_SHARE_DAYS) -> dict:
    """Called when the host user clicks 'Accept' on a share request."""
    token = new_pairing_token()  # 64-char pairing token
    expires = _utcnow() + timedelta(days=days)
    db.upsert_grant(normalize_id(remote_id), printer_id, token, _fmt(expires), remote_name)
    return {"token": token, "expires_at": _fmt(expires)}


def authorize_print(db: Database, remote_id: str, token: str) -> dict:
    """Gate every incoming /print job. Returns {'ok': True, 'printer': row} or {'ok': False, 'error': ...}."""
    grant = db.find_grant(normalize_id(remote_id), token)
    if grant is None:
        return {"ok": False, "error": "unknown ID or token"}
    if grant["status"] == "revoked":
        return {"ok": False, "error": "share was revoked"}
    if grant["status"] == "expired" or _utcnow() > datetime.strptime(grant["expires_at"], "%Y-%m-%d %H:%M:%S"):
        db.set_grant_status(grant["id"], "expired")
        return {"ok": False, "error": "share expired"}
    with db.connect() as con:
        printer = con.execute("SELECT * FROM shared_printers WHERE id = ? AND enabled = 1",
                              (grant["printer_id"],)).fetchone()
    if printer is None:
        return {"ok": False, "error": "printer no longer shared"}
    return {"ok": True, "printer": printer, "grant": grant}


def revoke_grant(db: Database, grant_id: int) -> None:
    """Manual early revocation from the tray menu."""
    db.set_grant_status(grant_id, "revoked")


def extend_grant(db: Database, grant_id: int, days: int = DEFAULT_SHARE_DAYS) -> str:
    """Re-activate a grant and push its expiry out by `days`. Returns new expiry."""
    expires = _utcnow() + timedelta(days=days)
    with db.connect() as con:
        con.execute("UPDATE grants SET expires_at = ?, status = 'active' WHERE id = ?",
                    (_fmt(expires), grant_id))
    return _fmt(expires)


def revoke_remote_share(db: Database, remote_id: str, printer_alias: str,
                        token: str) -> dict:
    """Called by POST /revoke-grant: the remote PC asks us to drop its grant.
    Token is verified so nobody can revoke someone else's access."""
    grant = db.find_grant_by_remote_and_alias(normalize_id(remote_id), printer_alias)
    if grant is None:
        return {"ok": False, "error": "no grant for this ID and printer"}
    if grant["token"] != token:
        return {"ok": False, "error": "token mismatch"}
    db.set_grant_status(grant["id"], "revoked")
    return {"ok": True}


def sweep_expired_grants(db: Database) -> int:
    """Hourly background job: mark overdue grants expired. Returns count updated."""
    expired = [g for g in db.list_grants("active")
               if _utcnow() > datetime.strptime(g["expires_at"], "%Y-%m-%d %H:%M:%S")]
    for g in expired:
        db.set_grant_status(g["id"], "expired")
    return len(expired)


# ---------- client side ----------

def store_accepted_share(db: Database, host_id: str, host_name: str, host_ip: str,
                         printer_alias: str, token: str, expires_at: str,
                         name: str | None = None) -> None:
    """Called when the host accepts our request and replies with a token."""
    db.upsert_remote_printer(normalize_id(host_id), printer_alias, token,
                             expires_at, host_ip=host_ip, host_name=host_name,
                             name=name)


def get_usable_printer(db: Database, host_id: str, printer_alias: str) -> dict:
    """Check before sending a job. Mirrors authorize_print on the client side."""
    rp = db.get_remote_printer(normalize_id(host_id), printer_alias)
    if rp is None:
        return {"ok": False, "error": "printer not added"}
    if rp["status"] != "active":
        return {"ok": False, "error": f"share is {rp['status']}"}
    if _utcnow() > datetime.strptime(rp["expires_at"], "%Y-%m-%d %H:%M:%S"):
        return {"ok": False, "error": "share expired — request it again"}
    return {"ok": True, "remote": rp}
