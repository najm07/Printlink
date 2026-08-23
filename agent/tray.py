"""PrintLink tray app: pystray icon + Tkinter dialogs for share management.

Runs the pystray icon on the main thread; Flask server and mDNS run in
daemon threads started by main.py. Tkinter dialogs are created on demand
in short-lived threads (Tk must not conflict with the pystray loop).
"""
import threading
import tkinter as tk
from tkinter import simpledialog, messagebox, ttk
from pathlib import Path
import pystray
from PIL import Image, ImageDraw

from db import Database, remote_label
from printer_local import list_printers
from shares import DEFAULT_SHARE_DAYS
from config import VERSION, SHARE_DIALOG_TIMEOUT_S
from logutil import get_logger

log = get_logger("tray")


def _default_icon() -> Image.Image:
    img = Image.new("RGB", (64, 64), "#1e6ff5")
    d = ImageDraw.Draw(img)
    d.rectangle((16, 24, 48, 44), fill="white")          # printer body
    d.rectangle((22, 12, 42, 26), fill="#cfe1ff")        # paper in
    d.rectangle((22, 38, 42, 52), fill="#cfe1ff")        # paper out
    return img


def _dialog(fn, timeout_s: float | None = None):
    """Run a Tkinter dialog in its own thread; return its result via Event.

    With timeout_s, the window is force-closed after that long and the
    result is None — an unanswered dialog must never pin the calling
    thread (the Flask worker) forever."""
    result, done = {}, threading.Event()

    def wrapper():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)

        def force_close():
            try:
                root.destroy()   # unblocks any modal messagebox inside fn
            except tk.TclError:
                pass             # already closed by the user

        if timeout_s:
            root.after(int(timeout_s * 1000), force_close)
        try:
            result["value"] = fn(root)
        except tk.TclError:
            pass                 # force-closed mid-dialog -> treat as None
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass
            done.set()
    threading.Thread(target=wrapper, daemon=True).start()
    done.wait()
    return result.get("value")


def _notify(title: str, msg: str):
    def _w():
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        messagebox.showinfo(title, msg)
        root.destroy()
    threading.Thread(target=_w, daemon=True).start()


class PrintLinkTray:
    def __init__(self, db: Database, my_id: str, send_request_fn, on_quit_fn,
                 selected_target: dict | None = None, send_file_fn=None,
                 revoke_fn=None):
        """send_request_fn(host_id, printer_alias, days, name) -> (ok, message)
        on_quit_fn() -> cleanup hook from main.py
        selected_target: {"value": (host_id, printer_alias)} shared with the
        pipe reader so port-monitor jobs go to the user's chosen printer.
        send_file_fn(filepath, host_id, printer_alias, options=) -> (ok, message):
        direct file send (no Windows spooler involved).
        revoke_fn(host_id, printer_alias) -> (ok, message): best-effort host-side
        grant revocation used when the user removes a remote printer."""
        self.db, self.my_id = db, my_id
        self.send_request_fn = send_request_fn
        self.on_quit_fn = on_quit_fn
        self.send_file_fn = send_file_fn
        self.revoke_fn = revoke_fn
        self.selected_target = selected_target if selected_target is not None else {}
        self.icon = pystray.Icon("PrintLink", _default_icon(), "PrintLink",
                                 self._menu())

    # ---- share-request gate, called by server.py from the Flask thread ----
    def on_share_request(self, sender_id: str, sender_name: str,
                         printer_alias: str, days: int) -> bool:
        accepted = _dialog(
            lambda root: messagebox.askyesno(
                "PrintLink — print share request",
                f"{sender_name} (ID {sender_id}) wants to print on\n"
                f"'{printer_alias}' for {days} day(s).\n\nAccept?",
            ),
            timeout_s=SHARE_DIALOG_TIMEOUT_S)
        log.info("share request from %s (%s) for '%s': %s",
                 sender_name, sender_id, printer_alias,
                 "ACCEPTED" if accepted else
                 ("TIMED OUT" if accepted is None else "DECLINED"))
        return bool(accepted)

    # ---- menu actions ----
    def _target_label(self) -> str:
        t = self.selected_target.get("value")
        if not t:
            return "None (jobs discarded)"
        row = None
        try:
            row = self.db.get_remote_printer(t[0], t[1])
        except Exception as e:
            log.warning("target label lookup failed: %r", e)
        return remote_label(row) if row is not None else f"{t[1]} @ {t[0]}"

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem(f"PrintLink v{VERSION}", lambda *_: None,
                             enabled=False),
            pystray.MenuItem(f"My ID: {self.my_id}", lambda *_: None, enabled=False),
            pystray.MenuItem("Copy my ID", self._copy_id),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Send document...", self._send_document),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Share a printer...", self._share_printer),
            pystray.MenuItem("My shared printers...", self._manage_shared),
            pystray.MenuItem("Manage grants...", self._manage_grants),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Add remote printer by ID...", self._add_remote),
            pystray.MenuItem(f"Remote target: {self._target_label()}",
                             lambda *_: None, enabled=False),
            pystray.MenuItem("Select remote printer...", self._select_target),
            pystray.MenuItem("Manage remote printers...", self._manage_remotes),
            pystray.MenuItem("My remote printers", self._list_remotes),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

    def _send_document(self, *_):
        from tkinter import filedialog
        if self.send_file_fn is None:
            _notify("PrintLink", "Direct send is not available.")
            return
        path = _dialog(lambda root: filedialog.askopenfilename(
            title="Print with PrintLink",
            filetypes=[("Documents", "*.pdf *.png *.jpg *.jpeg *.txt *.emf *.xps"),
                       ("All files", "*.*")]))
        if not path:
            return
        target = self.selected_target.get("value")
        if target is None:
            _notify("PrintLink", "No remote printer selected yet — pick one.")
            self._select_target()
            target = self.selected_target.get("value")
            if target is None:
                return
        host_id, alias = target
        rows = self.db.list_remote_printers(status="active")
        printers = [(r["host_id"], r["printer_alias"], r["name"]) for r in rows]
        if not printers:
            _notify("PrintLink", "No active remote printers. Add one first via "
                                  "'Add remote printer by ID...'.")
            return
        if (host_id, alias) not in printers:
            host_id, alias = printers[0][:2]
        from preview import ask_print_options
        res = _dialog(lambda root: ask_print_options(path, printers,
                                                     selected=(host_id, alias),
                                                     parent=root))
        if res is None:
            log.info("send document cancelled by user (preview dialog)")
            return
        opts, (host_id, alias) = res
        from cli import save_selected_target
        save_selected_target(host_id, alias)
        self.selected_target["value"] = (host_id, alias)
        ok, msg = self.send_file_fn(path, host_id, alias, options=opts)
        log.info("send document %s -> %s @ %s: ok=%s msg=%r options=%r",
                 path, alias, host_id, ok, msg, opts)
        _notify("PrintLink", msg if ok else f"Failed: {msg}")

    def on_delivered(self, filepath: str, host_id: str, printer_alias: str,
                     reason=None):
        label = self._lookup_label(host_id, printer_alias)
        _notify("PrintLink", f"'{Path(filepath).name}' printed on {label}.")

    def on_failed(self, filepath: str, host_id: str, printer_alias: str,
                  reason=None):
        label = self._lookup_label(host_id, printer_alias)
        _notify("PrintLink",
                f"Failed to print '{Path(filepath).name}' ({label}): "
                f"{reason or 'unknown error'}")

    def on_printer_error(self, filepath: str, host_id: str, printer_alias: str,
                         reason: str):
        label = self._lookup_label(host_id, printer_alias)
        _notify("PrintLink",
                f"{label}: {reason}. Retrying in the background...")

    def _lookup_label(self, host_id: str, printer_alias: str) -> str:
        try:
            row = self.db.get_remote_printer(host_id, printer_alias)
        except Exception as e:
            log.warning("label lookup failed for %s: %r", host_id, e)
            row = None
        return remote_label(row) if row is not None else f"{printer_alias} @ {host_id}"

    def _select_target(self, *_):
        rows = self.db.list_remote_printers(status="active")
        if not rows:
            _notify("PrintLink", "No active remote printers. Add one first via "
                                 "'Add remote printer by ID...'.")
            return

        def ui(root):
            win = tk.Toplevel(root)
            win.title("Select remote printer")
            win.attributes("-topmost", True)
            lb = tk.Listbox(win, width=70)
            for r in rows:
                lb.insert("end", f"{remote_label(r)} ({r['host_ip'] or 'unknown IP'})")
            lb.pack(padx=8, pady=8)
            out = {}
            def ok():
                sel = lb.curselection()
                if sel:
                    r = rows[sel[0]]
                    out["v"] = (r["host_id"], r["printer_alias"])
                win.destroy()
            tk.Button(win, text="Select", command=ok).pack(pady=6)
            win.wait_window()
            return out.get("v")

        picked = _dialog(ui)
        if picked:
            self.selected_target["value"] = picked
            from cli import save_selected_target
            save_selected_target(*picked)  # so one-shot --send can use it too
            log.info("remote target selected: %s @ %s", picked[1], picked[0])
            self.icon.menu = self._menu()  # refresh the disabled label row
            _notify("PrintLink", f"Remote printer set to {self._lookup_label(*picked)}.")

    def _copy_id(self, *_):
        _dialog(lambda root: (root.clipboard_clear(),
                              root.clipboard_append(self.my_id), None)[-1])
        _notify("PrintLink", f"ID {self.my_id} copied to clipboard.")

    def _share_printer(self, *_):
        def ui(root):
            shared = {p["local_name"] for p in self.db.list_shared_printers(enabled_only=False)}
            available = [p for p in list_printers() if p not in shared]
            if not available:
                messagebox.showinfo("PrintLink", "All local printers are already shared.")
                return None
            win = tk.Toplevel(root)
            win.title("Share a printer")
            win.attributes("-topmost", True)
            tk.Label(win, text="Printer:").grid(row=0, column=0, padx=6, pady=6)
            cb = ttk.Combobox(win, values=available, state="readonly", width=38)
            cb.grid(row=0, column=1, padx=6)
            cb.current(0)
            tk.Label(win, text="Share as (alias):").grid(row=1, column=0, padx=6)
            alias = tk.Entry(win, width=40)
            alias.insert(0, cb.get())
            alias.grid(row=1, column=1, padx=6)
            out = {}
            def ok():
                out["v"] = (cb.get(), alias.get().strip())
                win.destroy()
            tk.Button(win, text="Share", command=ok).grid(row=2, column=1, sticky="e", padx=6, pady=8)
            win.wait_window()
            return out.get("v")
        picked = _dialog(ui)
        if picked and picked[1]:
            self.db.add_shared_printer(picked[0], picked[1])
            log.info("shared printer '%s' as alias '%s'", picked[0], picked[1])
            _notify("PrintLink", f"'{picked[0]}' is now shared as '{picked[1]}'.")

    def _manage_grants(self, *_):
        def ui(root):
            grants = self.db.list_grants()
            if not grants:
                messagebox.showinfo("PrintLink", "No grants yet.")
                return None
            win = tk.Toplevel(root)
            win.title("Manage grants")
            win.attributes("-topmost", True)
            lb = tk.Listbox(win, width=74)
            for g in grants:
                lb.insert("end", f"[{g['status']}] {g['remote_id']} ({g['remote_name']}) → "
                                 f"{g['printer_alias']} until {g['expires_at']}")
            lb.pack(padx=8, pady=8)
            out = {}

            def done(action, value=None):
                out["a"], out["v"] = action, value
                win.destroy()

            def revoke():
                sel = lb.curselection()
                if sel:
                    g = grants[sel[0]]
                    if messagebox.askyesno("PrintLink",
                                           f"Revoke {g['remote_id']}'s access to "
                                           f"'{g['printer_alias']}'?"):
                        done("revoke", g["id"])

            def extend():
                sel = lb.curselection()
                if sel:
                    g = grants[sel[0]]
                    days = simpledialog.askinteger(
                        "PrintLink", f"Extend access for {g['remote_id']} "
                                     f"(days):", initialvalue=DEFAULT_SHARE_DAYS,
                        minvalue=1, maxvalue=365)
                    if days:
                        done("extend", (g["id"], days))

            btns = tk.Frame(win)
            tk.Button(btns, text="Revoke selected", command=revoke).pack(side="left", padx=4)
            tk.Button(btns, text="Extend selected...", command=extend).pack(side="left", padx=4)
            tk.Button(btns, text="Close", command=lambda: done("close")).pack(side="left", padx=4)
            btns.pack(pady=6)
            win.wait_window()
            return out.get("a"), out.get("v")

        action, value = _dialog(ui)
        if not action or action == "close":
            return
        if action == "revoke":
            from shares import revoke_grant
            revoke_grant(self.db, value)
            _notify("PrintLink", "Grant revoked.")
            return
        if action == "extend":
            grant_id, days = value
            from shares import extend_grant
            new_expiry = extend_grant(self.db, grant_id, days)
            _notify("PrintLink", f"Access extended until {new_expiry}.")

    def _manage_shared(self, *_):
        def ui(root):
            shared = self.db.list_shared_printers(enabled_only=False)
            if not shared:
                messagebox.showinfo("PrintLink", "No shared printers yet. Use "
                                                 "'Share a printer...'.")
                return None
            win = tk.Toplevel(root)
            win.title("My shared printers")
            win.attributes("-topmost", True)
            lb = tk.Listbox(win, width=74)
            for p in shared:
                lb.insert("end", f"{p['alias']} → {p['local_name']}"
                                 f" ({'enabled' if p['enabled'] else 'disabled'})")
            lb.pack(padx=8, pady=8)
            out = {}

            def done(action, value=None):
                out["a"], out["v"] = action, value
                win.destroy()

            def rename():
                sel = lb.curselection()
                if sel:
                    p = shared[sel[0]]
                    done("rename", p["id"])

            def unshare():
                sel = lb.curselection()
                if sel:
                    p = shared[sel[0]]
                    if messagebox.askyesno(
                            "PrintLink",
                            f"Unshare '{p['alias']}'?\n"
                            "Everyone's access to this printer will be revoked."):
                        done("unshare", p["id"])

            btns = tk.Frame(win)
            tk.Button(btns, text="Rename alias...", command=rename).pack(side="left", padx=4)
            tk.Button(btns, text="Unshare", command=unshare).pack(side="left", padx=4)
            tk.Button(btns, text="Close", command=lambda: done("close")).pack(side="left", padx=4)
            btns.pack(pady=6)
            win.wait_window()
            return out.get("a"), out.get("v")

        action, value = _dialog(ui)
        if not action or action == "close":
            return
        if action == "rename":
            alias = _dialog(lambda root: simpledialog.askstring(
                "PrintLink", "New alias (remote users will see this):"))
            if alias and alias.strip():
                self.db.update_shared_printer_alias(value, alias.strip())
                _notify("PrintLink", f"Alias updated to '{alias.strip()}'.")
            return
        if action == "unshare":
            self.db.delete_shared_printer(value)
            _notify("PrintLink", "Printer unshared (all grants revoked).")

    def _add_remote(self, *_):
        host_id = _dialog(lambda root: simpledialog.askstring(
            "PrintLink", "Enter the host PC's ID (e.g. 482 917 305):"))
        if not host_id:
            return
        alias = _dialog(lambda root: simpledialog.askstring(
            "PrintLink", "Printer alias on that host (e.g. Accounting-HP):"))
        if not alias:
            return
        name = _dialog(lambda root: simpledialog.askstring(
            "PrintLink", "Name of this receiver (shown as 'CANON @ <name>', "
                         "e.g. Lina's PC):", initialvalue=alias))
        if name is None:
            return
        name = name.strip() or None
        days = _dialog(lambda root: simpledialog.askinteger(
            "PrintLink", "Days of access:", initialvalue=DEFAULT_SHARE_DAYS,
            minvalue=1, maxvalue=90)) or DEFAULT_SHARE_DAYS
        ok, msg = self.send_request_fn(host_id, alias, days, name)
        log.info("add-remote %s@%s for %d days (name=%r) -> ok=%s msg=%r",
                 alias, host_id, days, name, ok, msg)
        _notify("PrintLink", msg)

    def _manage_remotes(self, *_):
        rows = self.db.list_remote_printers(status="active")
        if not rows:
            _notify("PrintLink", "No active remote printers. Add one first via "
                                 "'Add remote printer by ID...'.")
            return

        def ui(root):
            win = tk.Toplevel(root)
            win.title("Manage remote printers")
            win.attributes("-topmost", True)
            lb = tk.Listbox(win, width=74)
            for r in rows:
                lb.insert("end", f"{remote_label(r)} ({r['host_ip'] or 'unknown IP'})")
            lb.pack(padx=8, pady=8)
            out = {}

            def done(action, value=None):
                out["a"], out["v"] = action, value
                win.destroy()

            def rename():
                sel = lb.curselection()
                if sel:
                    r = rows[sel[0]]
                    done("rename", (r["host_id"], r["printer_alias"], r["name"]))

            def remove():
                sel = lb.curselection()
                if sel:
                    r = rows[sel[0]]
                    label = remote_label(r)
                    if messagebox.askyesno(
                            "PrintLink",
                            f"Remove '{label}'?\n"
                            "The host will also be told to revoke your access."):
                        done("remove", (r["host_id"], r["printer_alias"], label))

            btns = tk.Frame(win)
            tk.Button(btns, text="Rename...", command=rename).pack(side="left", padx=4)
            tk.Button(btns, text="Remove", command=remove).pack(side="left", padx=4)
            tk.Button(btns, text="Close", command=lambda: done("close")).pack(side="left", padx=4)
            btns.pack(pady=6)
            win.wait_window()
            return out.get("a"), out.get("v")

        action, value = _dialog(ui)
        if not action or action == "close":
            return
        if action == "rename":
            host_id, alias, old_name = value
            new_name = _dialog(lambda root: simpledialog.askstring(
                "PrintLink", "New name for this printer:", initialvalue=old_name or ""))
            if new_name is None:
                return
            new_name = new_name.strip() or None
            self.db.set_remote_printer_name(host_id, alias, new_name)
            self.icon.menu = self._menu()
            _notify("PrintLink",
                    f"Renamed to {new_name or f'{alias} @ {host_id}'}.")
            return
        if action == "remove":
            host_id, alias, label = value
            self.db.delete_remote_printer(host_id, alias)
            if self.selected_target.get("value") == (host_id, alias):
                self.selected_target["value"] = None
                from cli import clear_selected_target
                clear_selected_target()
            self.icon.menu = self._menu()
            revoke_msg = ""
            if self.revoke_fn is not None:
                r_ok, r_msg = self.revoke_fn(host_id, alias)
                revoke_msg = f"\nHost: {r_msg}" if not r_ok else ""
                log.info("remove %s '%s' -> host revoke ok=%s msg=%r",
                         host_id, alias, r_ok, r_msg)
            _notify("PrintLink", f"'{label}' removed.{revoke_msg}")

    def _list_remotes(self, *_):
        rows = self.db.list_remote_printers(status=None)
        text = "\n".join(f"[{r['status']}] {remote_label(r)} "
                         f"({r['host_ip']}) until {r['expires_at']}" for r in rows) or "None."
        _notify("PrintLink — my remote printers", text)

    def _quit(self, *_):
        self.on_quit_fn()
        self.icon.stop()

    def run(self):
        self.icon.run()
