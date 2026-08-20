"""PrintLink logging: one rotated file logger shared by all agent modules.

Every stage of the print pipeline writes here:
  - sender: preview dialog -> HTTP POST -> retry queue
  - receiver: HTTP receive -> decrypt -> spool via shell handler

Log file: %PROGRAMDATA%\\PrintLink\\printlink.log (shared, rotated, 3
backups). If the shared dir is not writable by this user (e.g. the ACL
was never fixed), fall back to %LOCALAPPDATA%\\PrintLink\\printlink.log —
logging must never crash the agent.
"""
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import DATA_DIR


def _log_file() -> Path:
    shared = DATA_DIR / "printlink.log"
    try:
        shared.parent.mkdir(parents=True, exist_ok=True)
        with open(shared, "a", encoding="utf-8"):
            pass
        return shared
    except OSError:
        per_user = Path.home() / "AppData" / "Local" / "PrintLink"
        per_user.mkdir(parents=True, exist_ok=True)
        return per_user / "printlink.log"


def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    """Configure (once) and return the 'printlink' root logger."""
    root = logging.getLogger("printlink")
    if root.handlers:
        return root
    log_file = _log_file()
    handler = RotatingFileHandler(log_file, maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    root.info("logging to %s", log_file)
    return root


def get_logger(name: str) -> logging.Logger:
    """Child logger for a module, e.g. get_logger('sender') -> printlink.sender."""
    return logging.getLogger("printlink." + name)
