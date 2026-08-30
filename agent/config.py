"""PrintLink configuration: paths, ports, and tunables in one place."""
import os
import shutil
from pathlib import Path

APP_NAME = "PrintLink"

# Keep in sync with MyAppVersion in installer/printlink.iss
VERSION = "1.0.2"

# --- storage (split since 0.3, see docs/security.md) ---
# SHARED: %PROGRAMDATA%\PrintLink — machine-wide, nothing secret. Holds the
#   shared-printer list, this PC's identity, and logs; every Windows account
#   must see the same printers.
# PRIVATE: per-user %LOCALAPPDATA%\PrintLink — holds printlink-private.db
#   (grants + remote printers) and target.json. Tokens become readable only
#   by the account that owns them.
# Falls back to per-user for BOTH when %PROGRAMDATA% is not writable.
_PER_USER_DIR = Path.home() / "AppData" / "Local" / "PrintLink"
SHARED_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "PrintLink"
PRIVATE_DIR = _PER_USER_DIR


def _dir_writable(d: Path) -> bool:
    try:
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".writetest"
        probe.write_text("x")
        probe.unlink()
        return True
    except OSError:
        return False


def _pick_shared_dir() -> Path:
    return SHARED_DIR if _dir_writable(SHARED_DIR) else _PER_USER_DIR


DATA_DIR = _pick_shared_dir()          # legacy name: where machine-wide data lives
DB_FILE = DATA_DIR / "printlink.db"                # shared db (printers only)
PRIVATE_DB_FILE = PRIVATE_DIR / "printlink-private.db"  # tokens (per-user)
IDENTITY_FILE = DATA_DIR / "identity.json"
TARGET_FILE = PRIVATE_DIR / "target.json"          # last tray-selected remote
TLS_CERT_FILE = DATA_DIR / "printlink-tls.pem"     # host identity (key+cert)

# --- network ---
LISTEN_PORT = 9100                 # HTTP API port (TCP, firewall-opened)
MDNS_SERVICE_TYPE = "_printlink._tcp.local."
CONNECT_TIMEOUT_S = 4
READ_TIMEOUT_S = 180               # host replies only after printing finishes
MAX_JOB_MB = 100

# --- shares ---
DEFAULT_SHARE_DAYS = 7
MAX_SHARE_DAYS = 90                # server-side clamp on /request-share 'days'
SWEEP_INTERVAL_S = 3600            # hourly expiry enforcement

# --- share-request flood control (unauthenticated endpoint) ---
RATE_SHARE_WINDOW_S = 900          # sliding window per client IP
RATE_SHARE_MAX = 5                 # max /request-share calls per window
SHARE_DIALOG_TIMEOUT_S = 60        # unanswered accept dialog auto-declines

# --- wire auth (0.3+): HMAC proof instead of transmitting the token ---
AUTH_NONCE_TTL_S = 120             # how long a host-issued nonce stays valid
AUTH_MAX_NONCES = 256              # bounded memory against challenge floods
ROUTE_VERIFY_TTL_S = 60            # trust a /ping-verified route this long
# 1.0 removed the pre-0.3 X-Token fallback entirely; peers without HMAC
# support get a clear "update PrintLink" error instead of a silent downgrade.

# --- sender retry ---
RETRY_INTERVAL_S = 15
RETRY_MAX_ATTEMPTS = 20            # ~5 minutes of retrying

# --- temp spool dirs ---
INBOX_DIR_NAME = "printlink_jobs"      # host side (received uploads)


def migrate_from_per_user():
    """First run on the shared dir: pull identity/db/target from the current
    user's old per-user data dir so nothing is lost. Never overwrites files
    that already exist in the destination."""
    if DATA_DIR == _PER_USER_DIR or not _PER_USER_DIR.exists():
        return
    for src_dir, dst_dir, names in (
            (_PER_USER_DIR, DATA_DIR, ("printlink.db", "identity.json")),
            (_PER_USER_DIR, PRIVATE_DIR, ("target.json",))):
        for name in names:
            src = src_dir / name
            dst = dst_dir / name
            if src.exists() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass


def migrate_legacy_target():
    """0.2.3-0.2.7 kept target.json machine-wide next to the db; since 0.3
    it is a per-user preference — carry it over once."""
    legacy = SHARED_DIR / "target.json"
    if legacy.exists() and not TARGET_FILE.exists():
        try:
            shutil.copy2(legacy, TARGET_FILE)
        except OSError:
            pass


def _try_fix_acl() -> None:
    """Best-effort: make the shared dir writable by all local users. The
    installer does this too; this covers manual/pl_dist deployments.
    Silently ignored when the current user lacks the rights."""
    if DATA_DIR != SHARED_DIR:
        return
    try:
        import subprocess
        subprocess.run(
            ["icacls", str(SHARED_DIR), "/grant", "Users:(OI)(CI)M",
             "/T", "/Q"],
            capture_output=True, timeout=15)
    except Exception:
        pass


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PRIVATE_DIR.mkdir(parents=True, exist_ok=True)
    migrate_from_per_user()
    migrate_legacy_target()
    _try_fix_acl()

