"""PrintLink CLI helpers: Explorer shell verbs + send-target resolution.

These are pure-ish functions so the one-shot --send mode and the tray app
share the same target logic, and tests can exercise them with stub objects.
"""
import json
import sys
from pathlib import Path

from identity import normalize_id
from config import MAX_JOB_MB, TARGET_FILE
from logutil import get_logger

log = get_logger("cli")

VERB_KEY = r"Software\Classes\*\shell\PrintLink"


def exe_command() -> str:
    """Command line that launches this app (frozen exe or python main.py)."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{Path(__file__).resolve().parent / "main.py"}"'


def install_shell_verbs() -> None:
    """Add 'Print with PrintLink' to the Explorer context menu for any file."""
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, VERB_KEY) as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, "Print with PrintLink")
        winreg.SetValueEx(k, "Icon", 0, winreg.REG_SZ, f"{exe_command()},0")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, VERB_KEY + r"\command") as k:
        winreg.SetValueEx(k, None, 0, winreg.REG_SZ, f'{exe_command()} --send "%1"')
    log.info("shell verb 'Print with PrintLink' installed for *")


def uninstall_shell_verbs() -> None:
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, VERB_KEY + r"\command")
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, VERB_KEY)
        log.info("shell verb removed")
    except FileNotFoundError:
        pass


def resolve_send_target(db, host_id: str | None, selected: dict) -> tuple[str, str] | None:
    """Pick (host_id, printer_alias) for a direct send.

    Priority: explicit --target host_id (must match exactly one active remote
    printer), then the tray's currently selected target, else None.
    """
    if host_id:
        host_id = normalize_id(host_id)
        rows = [r for r in db.list_remote_printers(status="active")
                if r["host_id"] == host_id]
        if len(rows) == 1:
            log.info("target from --target: %s @ %s", rows[0]["printer_alias"], host_id)
            return host_id, rows[0]["printer_alias"]
        if len(rows) > 1:
            log.error("--target %s is ambiguous: active aliases: %s", host_id,
                      ", ".join(r["printer_alias"] for r in rows))
        else:
            log.error("--target %s: no active remote printer found", host_id)
        return None
    sel = selected.get("value")
    if sel:
        log.info("target from tray selection: %s @ %s", sel[1], sel[0])
        return sel[0], sel[1]
    return None  # caller falls back to persisted target, then dialog


def check_send_file(path: str) -> tuple[bool, str]:
    p = Path(path)
    if not p.is_file():
        return False, f"File not found: {path}"
    size = p.stat().st_size
    limit = MAX_JOB_MB * 1024 * 1024
    if size > limit:
        return False, f"File too large ({size} bytes, limit {limit})"
    if size == 0:
        return False, f"File is empty: {path}"
    return True, ""


def save_selected_target(host_id: str, printer_alias: str, path=None) -> None:
    """Persist the tray's chosen remote printer so one-shot --send processes
    (Explorer context menu) can use it without --target."""
    p = Path(path) if path else TARGET_FILE
    try:
        p.write_text(json.dumps({"host_id": normalize_id(host_id),
                                 "printer_alias": printer_alias}),
                     encoding="utf-8")
        log.info("persisted target: %s @ %s", printer_alias, host_id)
    except OSError:
        log.warning("could not persist target to %s", p)


def clear_selected_target(path=None) -> None:
    """Drop the persisted target (e.g. the chosen printer was removed)."""
    p = Path(path) if path else TARGET_FILE
    try:
        p.unlink()
        log.info("cleared persisted target at %s", p)
    except FileNotFoundError:
        pass
    except OSError:
        log.warning("could not remove %s", p)


def load_selected_target(db, path=None) -> tuple[str, str] | None:
    """(host_id, alias) from disk if it still matches an ACTIVE remote printer."""
    import time
    p = Path(path) if path else TARGET_FILE
    data = None
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        log.info("no persisted target file at %s", p)
    except (OSError, ValueError) as e:
        log.warning("persisted target unreadable at %s: %r", p, e)
    if data is None:
        return None
    host_id = normalize_id(data.get("host_id", ""))
    printer_alias = data.get("printer_alias", "")
    if not host_id or not printer_alias:
        log.warning("persisted target file has no host_id/alias: %r", data)
        return None

    # the running tray may be mid-write on the sqlite db; retry briefly
    row = None
    for _ in range(3):
        try:
            row = next((r for r in db.list_remote_printers(status="active")
                        if r["host_id"] == host_id
                        and r["printer_alias"] == printer_alias), None)
            break
        except Exception as e:  # sqlite busy / transient
            log.warning("persisted target db lookup failed (%r), retrying", e)
            time.sleep(0.2)
    if row is None:
        log.info("persisted target %s @ %s not among active remotes "
                 "(file=%s)", printer_alias, host_id, p)
        return None
    log.info("loaded persisted target: %s @ %s", printer_alias, host_id)
    return host_id, printer_alias


def pick_target_dialog(db) -> tuple[str, str] | None:
    """Ask the user which remote printer to use (used by --send when neither
    --target nor a persisted tray selection is available)."""
    rows = db.list_remote_printers(status="active")
    if not rows:
        return None
    try:
        import tkinter as tk
    except ImportError:
        return None
    result: dict = {}
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        win = tk.Toplevel(root)
        win.title("Print with PrintLink")
        lb = tk.Listbox(win, width=72)
        for r in rows:
            label = f"{r['printer_alias']} @ {r['name']}" if r["name"] \
                else f"{r['printer_alias']} @ {r['host_id']}"
            lb.insert("end", f"{label} ({r['host_ip'] or 'unknown IP'})")
        lb.pack(padx=8, pady=8)

        def ok(_=None):
            sel = lb.curselection()
            if sel:
                r = rows[sel[0]]
                result["v"] = (r["host_id"], r["printer_alias"])
            win.destroy()

        tk.Button(win, text="Send", command=ok).pack(pady=6)
        win.bind("<Return>", ok)
        win.bind("<Double-Button-1>", ok)
        win.wait_window()
        root.destroy()
    except Exception as e:
        log.warning("target dialog failed: %r", e)
        return None
    return result.get("v")
