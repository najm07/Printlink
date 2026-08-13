"""PrintLink configuration: paths, ports, and tunables in one place."""
from pathlib import Path

APP_NAME = "PrintLink"

# --- storage ---
DATA_DIR = Path.home() / "AppData" / "Local" / "PrintLink"
DB_FILE = DATA_DIR / "printlink.db"
IDENTITY_FILE = DATA_DIR / "identity.json"
TARGET_FILE = DATA_DIR / "target.json"   # last tray-selected remote printer

# --- network ---
LISTEN_PORT = 9100                 # HTTP API port (TCP, firewall-opened)
MDNS_SERVICE_TYPE = "_printlink._tcp.local."
CONNECT_TIMEOUT_S = 4
READ_TIMEOUT_S = 180               # host replies only after printing finishes
MAX_JOB_MB = 100

# --- named pipe (port monitor <-> agent) ---
PIPE_NAME = r"\\.\pipe\\PrintLinkSender"
PIPE_CONNECT_WAIT_MS = 20000

# --- shares ---
DEFAULT_SHARE_DAYS = 7
MAX_SHARE_DAYS = 90
SWEEP_INTERVAL_S = 3600            # hourly expiry enforcement

# --- sender retry ---
RETRY_INTERVAL_S = 15
RETRY_MAX_ATTEMPTS = 20            # ~5 minutes of retrying

# --- temp spool dirs ---
OUTBOX_DIR_NAME = "printlink_outbox"   # sender side (from port monitor)
INBOX_DIR_NAME = "printlink_jobs"      # host side (received uploads)


def ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
