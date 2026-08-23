"""PrintLink entry point: wires identity, DB, discovery, server, sender, tray.

Modes:
  python main.py                          - tray app (receiver + sender)
  python main.py --send <file> [--target <host_id>]   - one-shot direct send
  python main.py --install-verbs          - Explorer 'Print with PrintLink'
  python main.py --uninstall-verbs

Run:  python main.py            (dev)
Install: packaged exe registered in HKCU\\...\\Run for auto-start.
"""
import argparse
import socket
import sys
import threading

from identity import load_or_create_id
from db import Database
from discovery import Discovery
from server import create_app, run_in_thread
from sender import Sender
from shares import sweep_expired_grants
from cli import (install_shell_verbs, uninstall_shell_verbs,
                 resolve_send_target, check_send_file,
                 save_selected_target, load_selected_target,
                 pick_target_dialog)
from preview import ask_print_options
from config import (DATA_DIR, DB_FILE, PRIVATE_DB_FILE, LISTEN_PORT,
                    SWEEP_INTERVAL_S, ensure_dirs)
from logutil import setup_logging, get_logger

log = get_logger("main")


def _expiry_sweeper(db: Database, stop: threading.Event):
    """Hourly background job: mark OUR host-side grants expired.

    Deliberately does NOT touch client-side remote_printers rows: the host
    is authoritative about expiry (it re-checks every job and may have
    extended a grant we can't see), so flipping local statuses to 'expired'
    only created stale lockouts. Honesty in the UI comes from displaying
    the expires_at date, not from mutating state."""
    while not stop.wait(SWEEP_INTERVAL_S):
        try:
            n = sweep_expired_grants(db)
            if n:
                print(f"[PrintLink] swept {n} expired grant(s)")
        except Exception as e:
            print(f"[PrintLink] sweeper error: {e}")


def _build_sender(db: Database, my_id: str, my_name: str) -> Sender:
    discovery = Discovery(my_id, LISTEN_PORT)
    return Sender(db, my_id, my_name, discovery.resolve), discovery


def _cmd_send(args) -> int:
    """One-shot direct send: no tray, no HTTP server, no spooler."""
    setup_logging()
    ensure_dirs()
    my_id = load_or_create_id(DATA_DIR)
    db = Database(DB_FILE, PRIVATE_DB_FILE)
    ok, err = check_send_file(args.send)
    if not ok:
        log.error("send: %s", err)
        print(f"[PrintLink] {err}", file=sys.stderr)
        return 2
    rows = db.list_remote_printers(status="active")
    printers = [(r["host_id"], r["printer_alias"], r["name"]) for r in rows]
    target = resolve_send_target(db, args.target, {})
    if target is None:
        target = load_selected_target(db)
    pairs = [(p[0], p[1]) for p in printers]
    if target is not None and target not in pairs:
        log.info("target %s @ %s no longer active; defaulting to first printer",
                 target[0], target[1])
        target = None
    if target is None and printers:
        target = printers[0][:2]
    if target is None:
        target = pick_target_dialog(db)
        if target is not None:
            save_selected_target(*target)
    if target is None:
        log.error("send: no target resolved")
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            messagebox.showwarning(
                "PrintLink", "No remote printer to print to.\n\n"
                "Open the PrintLink tray on this PC, add the printer "
                "by ID, then try again.")
            root.destroy()
        except Exception:
            pass
        print("[PrintLink] no target: select a remote printer in the tray "
              "or pass --target <host_id>", file=sys.stderr)
        return 2
    res = ask_print_options(args.send, printers or None, selected=target)
    if res is None:
        log.info("send cancelled by user (preview dialog)")
        return 0
    opts, target = res
    host_id, alias = target
    save_selected_target(host_id, alias)
    log.info("send target (from dialog): %s @ %s", alias, host_id)
    discovery = Discovery(my_id, LISTEN_PORT, advertise=False)
    sender = Sender(db, my_id, socket.gethostname(), discovery.resolve)
    sender.on_delivered = lambda fp, hid, al, reason=None: log.info(
        "send delivered: %s -> %s @ %s", fp, al, hid)
    sender.on_failed = lambda fp, hid, al, reason=None: log.error(
        "send FAILED: %s -> %s @ %s: %s", fp, al, hid, reason or "unknown")
    sender.on_printer_error = lambda fp, hid, al, reason: log.warning(
        "send printer error: %s -> %s @ %s: %s", fp, al, hid, reason)
    try:
        ok, msg = sender.print_file(args.send, host_id, alias, options=opts)
        if not ok:
            log.error("send: rejected: %s", msg)
            print(f"[PrintLink] {msg}", file=sys.stderr)
            return 2
        log.info("send queued: %s -> %s @ %s", args.send, alias, host_id)
        delivered = sender.wait_idle(timeout=450)
        log.info("send finished: delivered=%s", delivered)
        if delivered:
            print(f"[PrintLink] delivered to {alias} @ {host_id}")
        else:
            reason = sender.last_error or "unknown error"
            log.error("send FAILED to %s @ %s: %s", alias, host_id, reason)
            try:
                import tkinter as tk
                from tkinter import messagebox
                root = tk.Tk()
                root.withdraw()
                root.attributes("-topmost", True)
                messagebox.showerror(
                    "PrintLink", f"Failed to print on {alias} @ {host_id}:\n\n"
                                 f"{reason}")
                root.destroy()
            except Exception:
                pass
            print(f"[PrintLink] failed to {alias} @ {host_id} — {reason}",
                  file=sys.stderr)
        return 0 if delivered else 1
    finally:
        discovery.close()


def _cmd_tray() -> None:
    setup_logging()
    ensure_dirs()
    stop = threading.Event()

    # 1. identity + storage
    my_id = load_or_create_id(DATA_DIR)
    my_name = f"{socket.gethostname()}"
    db = Database(DB_FILE, PRIVATE_DB_FILE)
    log.info("PrintLink agent starting — PC ID: %s (%s), log in %s",
             my_id, my_name, DATA_DIR / "printlink.log")

    # 2. LAN discovery (advertise our ID, resolve others)
    discovery = Discovery(my_id, LISTEN_PORT)

    # 3. HTTP server (receiver) — tray callback wired after tray exists
    tray_holder: dict = {}

    def share_gate(sender_id, sender_name, alias, days) -> bool:
        tray = tray_holder.get("tray")
        return tray.on_share_request(sender_id, sender_name, alias, days) if tray else False

    app = create_app(db, my_id, on_share_request=share_gate)
    run_in_thread(app, LISTEN_PORT)

    # 4. sender (client side, retry queue)
    sender = Sender(db, my_id, my_name, discovery.resolve)

    # 4b. persisted send-target (shared with one-shot --send processes)
    selected_target = {"value": None}
    persisted = load_selected_target(db)
    if persisted:
        selected_target["value"] = persisted
        log.info("tray target seeded from persisted selection: %s @ %s",
                 persisted[1], persisted[0])

    # 5. background expiry enforcement
    sweeper = threading.Thread(target=_expiry_sweeper, args=(db, stop),
                               daemon=True, name="printlink-sweeper")
    sweeper.start()

    # 6. tray on the main thread (pystray requirement)
    def on_quit():
        stop.set()
        sender.stop()
        discovery.close()

    def on_delivered(filepath, host_id, alias, reason=None):
        log.info("delivered %s to %s @ %s", filepath, alias, host_id)
        try:
            tray.on_delivered(filepath, host_id, alias)
        except Exception:
            log.exception("delivery notification failed")

    def on_failed(filepath, host_id, alias, reason=None):
        log.error("giving up on %s -> %s @ %s: %s", filepath, alias, host_id,
                  reason or "unknown")
        try:
            tray.on_failed(filepath, host_id, alias, reason)
        except Exception:
            log.exception("failure notification failed")

    def on_printer_error(filepath, host_id, alias, reason):
        log.warning("printer error %s -> %s @ %s: %s", filepath, alias,
                    host_id, reason)
        try:
            tray.on_printer_error(filepath, host_id, alias, reason)
        except Exception:
            log.exception("printer-error notification failed")

    sender.on_delivered = on_delivered
    sender.on_failed = on_failed
    sender.on_printer_error = on_printer_error

    from tray import PrintLinkTray  # lazy: pystray needs a display/desktop

    tray = PrintLinkTray(db, my_id, sender.request_share, on_quit,
                         selected_target=selected_target,
                         send_file_fn=sender.print_file,
                         revoke_fn=sender.revoke_share)
    tray_holder["tray"] = tray

    try:
        tray.run()  # blocks until Quit
    finally:
        on_quit()


def main():
    parser = argparse.ArgumentParser(prog="PrintLinkAgent",
                                     description="PrintLink tray app / direct sender")
    parser.add_argument("--send", metavar="FILE", help="send a document PDF/image directly")
    parser.add_argument("--target", metavar="HOST_ID",
                        help="remote PC ID for --send (default: tray selection)")
    parser.add_argument("--install-verbs", action="store_true",
                        help="add Explorer 'Print with PrintLink' context menu")
    parser.add_argument("--uninstall-verbs", action="store_true",
                        help="remove the Explorer context menu")
    args = parser.parse_args()

    if args.install_verbs or args.uninstall_verbs:
        setup_logging()  # verb mode is short-lived; still want the log line
    if args.install_verbs:
        install_shell_verbs()
        return
    if args.uninstall_verbs:
        uninstall_shell_verbs()
        return
    if args.send:
        sys.exit(_cmd_send(args))
    _cmd_tray()


if __name__ == "__main__":
    main()