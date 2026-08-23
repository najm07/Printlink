"""Cross-platform tests for printer_local helpers (no Windows/Word needed).

Covers the B1/B2 audit fixes: the targeted WINWORD timeout kill must be able
to distinguish OUR Word instances from the user's, and the GDI+ PowerShell
helper must use a unique per-call script so concurrent jobs cannot race.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "agent")  # real printer_local, not the test_api stub
sys.modules.pop("printer_local", None)

import printer_local as pl


def test_new_pids_only_reports_our_instances():
    assert pl.new_pids({100, 200}, {100, 200, 300}) == [300]
    assert pl.new_pids(set(), set()) == []
    assert pl.new_pids({7}, {7}) == []          # pre-existing: never ours
    assert pl.new_pids(set(), {42, 9}) == [9, 42]  # sorted for tidy logs


class _FakeCompleted:
    def __init__(self, stdout: bytes):
        self.stdout = stdout
        self.returncode = 0


def _patch_tasklist(monkeypatch, lines):
    def fake_run(cmd, capture_output=None, timeout=None):
        joined = " ".join(cmd)
        assert "tasklist" in joined and "WINWORD.EXE" in joined
        return _FakeCompleted("\r\n".join(lines).encode("utf-8"))

    monkeypatch.setattr("subprocess.run", fake_run)


def test_winword_pids_parses_tasklist_csv(monkeypatch):
    _patch_tasklist(monkeypatch, [
        '"WINWORD.EXE","1234","Console","1","95,020 K"',
        '"winword.exe","5678","Console","1","88,004 K"',
        '"notepad.exe","99","Console","1","12 K"',
        'INFO: No tasks are running which match the specified criteria.',
        "",
    ])
    assert pl._winword_pids() == {1234, 5678}


def test_winword_pids_survives_tasklist_failure(monkeypatch):
    import subprocess

    def boom(*a, **kw):
        raise OSError("no tasklist on this machine")

    monkeypatch.setattr(subprocess, "run", boom)
    # Empty set disables the targeted kill rather than misfiring it.
    assert pl._winword_pids() == set()


class _FakeOK:
    returncode = 0
    stdout = b""
    stderr = b""


def test_gdi_print_uses_unique_temp_script_per_call(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, capture_output=None, timeout=None):
        script = Path(cmd[cmd.index("-File") + 1])
        seen.append(script)
        assert script.exists()      # present while "powershell" runs
        assert "-NonInteractive" in cmd
        return _FakeOK()

    monkeypatch.setattr("subprocess.run", fake_run)
    pl._run_gdi_print(str(tmp_path / "img.png"), "HP")
    pl._run_gdi_print(str(tmp_path / "img2.png"), "HP")

    assert len(seen) == 2
    assert len({s.name for s in seen}) == 2     # unique per call — no shared path
    assert not any(s.exists() for s in seen)    # cleaned up afterwards


@pytest.mark.skipif(not sys.platform.startswith("win"),
                    reason="tasklist is Windows-only")
def test_winword_pids_real_call_returns_set():
    assert isinstance(pl._winword_pids(), set)
