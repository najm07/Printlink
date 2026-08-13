"""PrintLink identity: AnyDesk-style random PC ID, generated once and persisted."""
import secrets
import string
import json
from pathlib import Path

ID_GROUPS = (3, 3, 3)  # e.g. "482 917 305"
_CONFIG_NAME = "identity.json"


def _config_dir() -> Path:
    base = Path.home() / "AppData" / "Local" / "PrintLink"
    base.mkdir(parents=True, exist_ok=True)
    return base


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
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
        pc_id = data.get("pc_id")
        if pc_id and is_valid_id(pc_id):
            return pc_id
    pc_id = generate_id()
    cfg_file.write_text(json.dumps({"pc_id": pc_id}, indent=2), encoding="utf-8")
    return pc_id
