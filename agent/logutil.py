"""PrintLink logging: one rotated file logger shared by all agent modules.

Every stage of the print pipeline writes here:
  - sender: port monitor pipe -> outbox -> HTTP POST -> retry queue
  - receiver: HTTP receive -> decrypt -> spool via shell handler

Log file: %LOCALAPPDATA%\\PrintLink\\printlink.log  (rotated, 3 backups)
"""
import logging
from logging.handlers import RotatingFileHandler

from config import DATA_DIR

_LOG_FILE = DATA_DIR / "printlink.log"


def setup_logging(level: int = logging.DEBUG) -> logging.Logger:
    """Configure (once) and return the 'printlink' root logger."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("printlink")
    if root.handlers:
        return root
    handler = RotatingFileHandler(_LOG_FILE, maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    return root


def get_logger(name: str) -> logging.Logger:
    """Child logger for a module, e.g. get_logger('sender') -> printlink.sender."""
    return logging.getLogger("printlink." + name)
