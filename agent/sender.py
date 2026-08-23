"""PrintLink sender: client-side networking + local retry queue.

Responsibilities:
- resolve a host ID to an IP (mDNS cache via discovery.py, fallback to stored IP)
- POST /request-share when the user adds a remote printer
- POST /print jobs with token auth; queue + retry while the host is unreachable
"""
import json
import os
import threading
import queue
import time
from pathlib import Path
import requests

from db import Database
from identity import normalize_id, is_valid_id
from shares import store_accepted_share, get_usable_printer
from crypto import encrypt_payload
from config import (CONNECT_TIMEOUT_S, READ_TIMEOUT_S, RETRY_INTERVAL_S,
                    RETRY_MAX_ATTEMPTS)
from logutil import get_logger

log = get_logger("sender")


def _safe_json(resp) -> dict:
    """JSON body as a dict, or {} when the peer answered non-JSON (proxy,
    captive portal, HTML error page). json() raises ValueError, which is
    NOT a RequestException — without this guard such replies crashed the
    caller instead of surfacing as a normal failure."""
    try:
        j = resp.json()
    except (ValueError, AttributeError):
        return {}
    return j if isinstance(j, dict) else {}


class Sender:
    def __init__(self, db: Database, my_id: str, my_name: str, resolver,
                 on_delivered=None, on_failed=None, on_printer_error=None):
        """resolver(host_id) -> (ip, port) or None  (from discovery.py)
        on_delivered(filepath, host_id, alias)      — job printed
        on_failed(filepath, host_id, alias, reason) — job given up on
        on_printer_error(filepath, host_id, alias, reason) — first failed
        attempt of a job (printer offline, paper out, host error, ...);
        called once per job, before retries continue."""
        self.db, self.my_id, self.my_name = db, my_id, my_name
        self.resolver = resolver
        self.on_delivered = on_delivered
        self.on_failed = on_failed
        self.on_printer_error = on_printer_error
        self._jobs: queue.Queue = queue.Queue()
        self._pending = 0
        self._ever_failed = False
        self.last_error: str | None = None
        self._lock = threading.Lock()
        self._drained = threading.Event()
        self._drained.set()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._retry_loop, daemon=True,
                                        name="printlink-sender")
        self._worker.start()
        log.info("sender started (id=%s name=%s)", my_id, my_name)

    def wait_idle(self, timeout: float) -> bool:
        """Block until every queued job is done; True only if none failed."""
        self._drained.wait(timeout)
        with self._lock:
            return self._pending == 0 and not self._ever_failed

    def _job_done(self, filepath: str, host_id: str, printer_alias: str, ok: bool,
                  reason: str | None = None):
        with self._lock:
            self._pending = max(0, self._pending - 1)
            if not ok:
                self._ever_failed = True
            if self._pending == 0:
                self._drained.set()
        cb = self.on_delivered if ok else self.on_failed
        if cb:
            try:
                cb(filepath, host_id, printer_alias, reason)
            except Exception:
                log.exception("delivery callback failed for %s", filepath)

    # ---------- address resolution ----------
    def _base_url(self, host_id: str) -> str | None:
        host_id = normalize_id(host_id)
        found = self.resolver(host_id)
        if found:
            ip, port = found
            self.db.update_remote_host_ip(host_id, ip, port)
            log.info("resolved %s -> %s:%s (mDNS)", host_id, ip, port)
            return f"http://{ip}:{port}"
        rp = next((r for r in self.db.list_remote_printers(status=None)
                   if r["host_id"] == host_id), None)
        if rp and rp["host_ip"]:
            log.info("resolved %s -> %s:%s (stored IP)", host_id,
                     rp["host_ip"], rp["host_port"])
            return f"http://{rp['host_ip']}:{rp['host_port']}"
        log.warning("could not resolve %s: mDNS miss and no stored IP", host_id)
        return None

    # ---------- share request ----------
    def request_share(self, host_id: str, printer_alias: str, days: int,
                      name: str | None = None) -> tuple[bool, str]:
        if not is_valid_id(host_id):
            log.warning("request_share: invalid ID %r", host_id)
            return False, "Invalid ID format."
        base = self._base_url(host_id)
        if base is None:
            return False, "Host not found on the LAN (is it online and running PrintLink?)"
        try:
            log.info("request_share: pinging %s", base)
            ping = requests.get(f"{base}/ping", timeout=CONNECT_TIMEOUT_S)
            ping_j = _safe_json(ping)
            if ping.status_code != 200 or "id" not in ping_j:
                log.warning("request_share: bad /ping from %s: HTTP %d %r",
                            base, ping.status_code, ping.text[:120])
                return False, "Host did not answer its /ping properly."
            if normalize_id(ping_j.get("id", "")) != normalize_id(host_id):
                log.warning("request_share: ID mismatch at %s (got %r)",
                            base, ping_j.get("id"))
                return False, f"ID mismatch: that IP answers as {ping_j.get('id')}."
            log.info("request_share: POST %s/request-share alias=%r days=%d",
                     base, printer_alias, days)
            r = requests.post(f"{base}/request-share", json={
                "sender_id": self.my_id, "sender_name": self.my_name,
                "printer_alias": printer_alias, "days": days}, timeout=60)
            log.info("request_share: HTTP %d -> %s", r.status_code,
                     _safe_json(r).get("status", r.text[:200]))
        except requests.RequestException as e:
            log.warning("request_share: connection failed to %s: %r", base, e)
            return False, f"Connection failed: {e}"
        j = _safe_json(r)
        if not j:
            log.warning("request_share: non-JSON reply from %s: HTTP %d %r",
                        host_id, r.status_code, r.text[:120])
            return False, f"Host answered unexpectedly (HTTP {r.status_code})."
        if r.status_code == 200 and j.get("status") == "accepted":
            ip = base.split("//")[1].split(":")[0]
            store_accepted_share(self.db, host_id, "", ip, printer_alias,
                                 j["token"], j["expires_at"], name=name)
            log.info("request_share: ACCEPTED for %s (%s) token saved", host_id, printer_alias)
            return True, f"Access granted until {j['expires_at']}."
        log.warning("request_share: refused by %s: %s", host_id,
                    j.get("reason", r.status_code))
        return False, f"Refused: {j.get('reason', r.status_code)}"

    def revoke_share(self, host_id: str, printer_alias: str) -> tuple[bool, str]:
        """Best-effort: ask the host to drop our grant (used when the user
        removes a remote printer). Never blocks the UI for long."""
        rp = next((r for r in self.db.list_remote_printers(status=None)
                   if r["host_id"] == host_id
                   and r["printer_alias"] == printer_alias), None)
        if rp is None:
            return False, "printer not in list"
        base = self._base_url(host_id)
        if base is None:
            return False, "host unreachable — local entry removed anyway"
        try:
            r = requests.post(f"{base}/revoke-grant", json={
                "sender_id": self.my_id, "printer_alias": printer_alias,
                "token": rp["token"]}, timeout=CONNECT_TIMEOUT_S)
            log.info("revoke_share %s '%s': HTTP %d", host_id, printer_alias,
                     r.status_code)
            if r.status_code == 200:
                return True, "host grant revoked"
            return False, f"host said {r.status_code}"
        except requests.RequestException as e:
            log.warning("revoke_share failed for %s: %r", host_id, e)
            return False, f"host unreachable: {e}"

    # ---------- printing ----------
    def print_file(self, filepath: str | Path, host_id: str, printer_alias: str,
                   delete_after: bool = False,
                   options: dict | None = None) -> tuple[bool, str]:
        """Queue a file for delivery to a remote printer.

        delete_after: only the legacy pipe reader sets this for its own temp
        job files. User documents handed in directly are NEVER deleted.
        options: print preferences (copies/pages/paper/color/duplex/fit) sent
        to the receiver, which applies what it can per format.
        """
        check = get_usable_printer(self.db, host_id, printer_alias)
        if not check["ok"]:
            log.warning("print_file %s -> %s@%s rejected: %s",
                        filepath, printer_alias, host_id, check["error"])
            return False, check["error"]
        self._jobs.put((str(filepath), normalize_id(host_id), printer_alias, 0,
                        bool(delete_after), dict(options or {})))
        with self._lock:
            was_idle = self._pending == 0
            self._pending += 1
            if was_idle:
                # A new drain cycle starts clean; never reset the latch while
                # sibling jobs are still outstanding, or their failures would
                # vanish from wait_idle's result.
                self._ever_failed = False
            self._drained.clear()
        log.info("print_file %s queued for %s@%s (options=%r)",
                 filepath, printer_alias, host_id, options or {})
        return True, "Job queued."

    def _send_once(self, filepath: str, host_id: str,
                   printer_alias: str, options: dict | None = None
                   ) -> tuple[bool, bool]:
        """Try once. Returns (delivered, retryable).

        Retryable: connection errors and HTTP 503 (printer offline, may
        recover). Permanent (give up immediately): HTTP 400/403/500 etc. —
        the receiver answered and the job cannot succeed as-is.
        """
        base = self._base_url(host_id)
        rp = self.db.get_remote_printer(host_id, printer_alias)
        if base is None or rp is None:
            log.warning("send %s: no route to %s (base=%s rp=%s)",
                        filepath, host_id, base, "missing" if rp is None else "ok")
            return False, True
        try:
            with open(filepath, "rb") as f:
                raw = f.read()
            payload = encrypt_payload(raw, rp["token"])
            log.info("send %s -> %s/print (%d -> %d encrypted bytes, alias=%s)",
                     filepath, base, len(raw), len(payload), printer_alias)
            data = None
            if options:
                data = {"options": json.dumps(options)}
            r = requests.post(f"{base}/print",
                              headers={"X-Sender-ID": self.my_id, "X-Token": rp["token"]},
                              files={"file": (Path(filepath).name, payload)},
                              data=data,
                              timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S))
            log.info("send %s -> HTTP %d body=%s", Path(filepath).name,
                     r.status_code, r.text[:200])
            if r.status_code == 200:
                self.last_error = None
                return True, False
            self.last_error = r.text[:300]
            return False, r.status_code == 503
        except requests.RequestException as e:
            log.warning("send %s failed: %r", filepath, e)
            self.last_error = str(e)
            return False, True
        except OSError as e:
            log.warning("send %s failed (IO): %r", filepath, e)
            self.last_error = str(e)
            return False, True

    @staticmethod
    def _cleanup(filepath: str) -> None:
        try:
            os.unlink(filepath)
        except OSError:
            pass

    def _retry_loop(self):
        max_attempts = RETRY_MAX_ATTEMPTS
        pending = []
        while not self._stop.is_set():
            try:
                pending.append(self._jobs.get(timeout=1))
            except queue.Empty:
                pass
            still = []
            for filepath, host_id, alias, attempts, delete_after, options in pending:
                log.info("retry_loop: attempt %d for %s@%s (%s)",
                         attempts + 1, alias, host_id, Path(filepath).name)
                ok, retryable = self._send_once(filepath, host_id, alias, options)
                if ok:
                    if delete_after:
                        self._cleanup(filepath)
                    log.info("retry_loop: delivered %s to %s@%s",
                             Path(filepath).name, alias, host_id)
                    self._job_done(filepath, host_id, alias, True)
                    continue  # delivered
                reason = self.last_error or "unknown error"
                if attempts == 0 and self.on_printer_error:
                    # first failed attempt: tell the user what's wrong now,
                    # before the silent retry period starts
                    try:
                        self.on_printer_error(filepath, host_id, alias, reason)
                    except Exception:
                        log.exception("printer-error callback failed for %s",
                                      filepath)
                if not retryable:
                    log.error("retry_loop: giving up on %s (receiver rejected, "
                              "not retryable): %s", Path(filepath).name, reason)
                    if delete_after:
                        self._cleanup(filepath)
                    self._job_done(filepath, host_id, alias, False, reason)
                    continue
                if attempts + 1 < max_attempts:
                    still.append((filepath, host_id, alias, attempts + 1,
                                  delete_after, options))
                else:
                    log.error("retry_loop: giving up on %s after %d attempts",
                              Path(filepath).name, max_attempts)
                    if delete_after:
                        self._cleanup(filepath)
                    self._job_done(filepath, host_id, alias, False, reason)
            pending = still
            if pending:
                log.info("retry_loop: %d job(s) pending, retrying in %ds",
                         len(pending), RETRY_INTERVAL_S)
                time.sleep(RETRY_INTERVAL_S)

    def stop(self):
        self._stop.set()
