"""PrintLink configuration: paths, ports, and tunables in one place."""
import os
import shutil
from pathlib import Path

APP_NAME = "PrintLink"

# Keep in sync with MyAppVersion in installer/printlink.iss
VERSION = "0.2.7"

# --- storage ---
# Data lives machine-wide under %PROGRAMDATA% so EVERY user account on the PC
# shares one identity, one grant/printers DB, and one persisted target —
# otherwise printers added by the admin never show up for other accounts.
# Falls back to per-user %LOCALAPPDATA% when the shared dir is not writable.
_PER_USER_DIR = Path.home() / "AppData" / "Local" / "PrintLink"
SHARED_DIR = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "PrintLink"


def _pick_data_dir() -> Path:
    try:
        SHARED_DIR.mkdir(parents=True, exist_ok=True)
        probe = SHARED_DIR / ".writetest"
        probe.write_text("x")
        probe.unlink()
        return SHARED_DIR
    except OSError:
        return _PER_USER_DIR


DATA_DIR = _pick_data_dir()
DB_FILE = DATA_DIR / "printlink.db"
IDENTITY_FILE = DATA_DIR / "identity.json"
TARGET_FILE = DATA_DIR / "target.json"   # last tray-selected remote printer

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

# --- sender retry ---
RETRY_INTERVAL_S = 15
RETRY_MAX_ATTEMPTS = 20            # ~5 minutes of retrying

# --- temp spool dirs ---
INBOX_DIR_NAME = "printlink_jobs"      # host side (received uploads)


def migrate_from_per_user():
    """First run on the shared dir: pull identity/db/target from the current
    user's old per-user data dir so nothing is lost. Never overwrites files
    that already exist in the shared dir."""
    if DATA_DIR == _PER_USER_DIR or not _PER_USER_DIR.exists():
        return
    for name in ("printlink.db", "identity.json", "target.json"):
        src = _PER_USER_DIR / name
        dst = DATA_DIR / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
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
    migrate_from_per_user()
    _try_fix_acl()

