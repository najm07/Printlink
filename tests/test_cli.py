"""Tests for cli.py: send-target resolution and shell-verb command lines."""
from pathlib import Path

pytest = __import__("pytest")

pytest.importorskip("tkinter")

from cli import (resolve_send_target, check_send_file,
                 save_selected_target, load_selected_target)


def test_main_wiring_imports_check_send_file():
    """Regression: _cmd_send crashed with NameError 'check_send_file is not
    defined' because main.py never imported it (only exercised at runtime
    via the right-click verb)."""
    import main
    assert main.check_send_file is check_send_file


class FakeDB:
    """Duck-typed stand-in for Database.list_remote_printers(status=...)."""

    def __init__(self, rows):
        self.rows = rows

    def list_remote_printers(self, status=None):
        if status is None:
            return self.rows
        return [r for r in self.rows if r["status"] == status]


ROWS = [
    {"host_id": "656055745", "printer_alias": "IT", "status": "active"},
    {"host_id": "482917305", "printer_alias": "Accounting-HP", "status": "active"},
    {"host_id": "482917305", "printer_alias": "Accounting-HP-2", "status": "active"},
    {"host_id": "999111222", "printer_alias": "Old", "status": "expired"},
]


def test_target_from_explicit_host_id():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, "656 055 745", {}) == ("656055745", "IT")


def test_target_normalizes_id():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, "656055745", {}) == ("656055745", "IT")


def test_target_ambiguous_returns_none():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, "482 917 305", {}) is None


def test_target_unknown_host_returns_none():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, "000 000 000", {}) is None


def test_expired_remote_not_usable():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, "999 111 222", {}) is None


def test_falls_back_to_tray_selection():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, None, {"value": ("656055745", "IT")}) \
        == ("656055745", "IT")


def test_no_target_returns_none():
    db = FakeDB(ROWS)
    assert resolve_send_target(db, None, {}) is None


def test_check_send_file(tmp_path: Path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 test")
    assert check_send_file(str(p)) == (True, "")
    assert check_send_file(str(tmp_path / "missing.pdf"))[0] is False
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")
    assert check_send_file(str(empty))[0] is False


def test_save_load_selected_target(tmp_path: Path):
    p = tmp_path / "target.json"
    save_selected_target("656 055 745", "IT", path=p)
    assert load_selected_target(FakeDB(ROWS), path=p) == ("656055745", "IT")


def test_load_target_missing_file(tmp_path: Path):
    assert load_selected_target(FakeDB(ROWS), path=tmp_path / "nope.json") is None


def test_load_target_expired_remote(tmp_path: Path):
    p = tmp_path / "target.json"
    save_selected_target("999111222", "Old", path=p)
    assert load_selected_target(FakeDB(ROWS), path=p) is None


def test_load_target_bad_json(tmp_path: Path):
    p = tmp_path / "target.json"
    p.write_text("{not json", encoding="utf-8")
    assert load_selected_target(FakeDB(ROWS), path=p) is None


def test_save_selected_target_atomic_no_temp_leftovers(tmp_path: Path):
    """B4: target.json is written temp+replace, never truncated in place."""
    import json
    p = tmp_path / "target.json"
    save_selected_target("656 055 745", "IT", path=p)
    assert json.loads(p.read_text(encoding="utf-8"))["host_id"] == "656055745"
    assert list(tmp_path.glob("*.tmp")) == []