import json
from identity import generate_id, normalize_id, is_valid_id, load_or_create_id


def test_generate_id_format():
    pc_id = generate_id()
    assert is_valid_id(pc_id)
    assert pc_id.count(" ") == 2


def test_normalize_variants():
    assert normalize_id("482 917 305") == "482917305"
    assert normalize_id("482-917-305") == "482917305"
    assert normalize_id("482917305") == "482917305"


def test_is_valid_id_rejects_bad_input():
    assert not is_valid_id("123")
    assert not is_valid_id("abc def ghi")
    assert not is_valid_id("")


def test_persistence(tmp_path):
    first = load_or_create_id(tmp_path)
    assert load_or_create_id(tmp_path) == first
    data = json.loads((tmp_path / "identity.json").read_text())
    assert data["pc_id"] == first


def test_regenerates_on_corrupt_config(tmp_path):
    (tmp_path / "identity.json").write_text('{"pc_id": "broken"}')
    assert is_valid_id(load_or_create_id(tmp_path))


def test_regenerates_on_malformed_json(tmp_path):
    """Old code crashed on unreadable JSON; now it regenerates (B4)."""
    (tmp_path / "identity.json").write_text("{not json at all", encoding="utf-8")
    assert is_valid_id(load_or_create_id(tmp_path))


def test_atomic_write_leaves_no_temp_files(tmp_path):
    """B4: identity.json must be written via temp+replace — a crash mid-write
    used to be able to truncate the file and orphan every grant."""
    for _ in range(3):
        assert is_valid_id(load_or_create_id(tmp_path))
    assert list(tmp_path.glob("*.tmp")) == []
    data = json.loads((tmp_path / "identity.json").read_text(encoding="utf-8"))
    assert is_valid_id(data["pc_id"])
