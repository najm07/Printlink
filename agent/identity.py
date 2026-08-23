"""PrintLink identity: AnyDesk-style random PC ID, generated once and persisted."""
import json
import os
import secrets
import string
import tempfile
from pathlib import Path

ID_GROUPS = (3, 3, 3)  # e.g. "482 917 305"
_CONFIG_NAME = "identity.json"


def _config_dir() -> Path:
    base = Path.home() / "AppData" / "Local" / "PrintLink"
    base.mkdir(parents=True, exist_ok=True)
    return base


def write_json_atomic(path: Path, data: dict) -> None:
    """Write JSON via temp file + os.replace (atomic same-volume).

    A crash mid-write must never leave a truncated file: identity.json holds
    the persistent PC ID, and losing it silently orphans every grant."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def generate_id() -> str:
    digits = "".join(secrets.choice(string.digits) for _ in range(sum(ID_GROUPS)))
    return " ".join(digits[sum(ID_GROUPS[:i]):sum(ID_GROUPS[:i + 1])]
                    for i in range(len(ID_GROUPS)))


def normalize_id(pc_id: str) -> str:
    """Strip spaces/dashes so '482 917 305' and '482-917-305' compare equal."""
    return "".join(c for c in pc_id if c in string.digits)


def is_valid_id(pc_id: str) -> bool:
    return len(normalize_id(pc_id)) == sum(ID_GROUPS)


def load_or_create_id(config_dir: Path | None = None) -> str:
    cfg_dir = config_dir or _config_dir()
    cfg_file = cfg_dir / _CONFIG_NAME
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}  # unreadable/corrupt: regenerate below
        pc_id = data.get("pc_id")
        if pc_id and is_valid_id(pc_id):
            return pc_id
    pc_id = generate_id()
    write_json_atomic(cfg_file, {"pc_id": pc_id})
    # Two accounts can hit first start concurrently on the machine-wide dir;
    # whoever's replace lands last wins. Read back so all processes converge
    # on the SAME id instead of each keeping their own.
    try:
        winner = json.loads(cfg_file.read_text(encoding="utf-8")).get("pc_id")
        if is_valid_id(winner):
            return winner
    except (OSError, ValueError):
        pass
    return pc_id
