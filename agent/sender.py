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
from auth import sign_nonce, token_hint
from tlsutil import probe_fingerprint, PinnedAdapterHosts
from config import (CONNECT_TIMEOUT_S, READ_TIMEOUT_S, RETRY_INTERVAL_S,
                    RETRY_MAX_ATTEMPTS, ROUTE_VERIFY_TTL_S)
from logutil import get_logger

log = get_logger("sender")


class RequestsHttp:
    """Thin transport so tests can inject fakes; production calls with a
    per-host pinned Session use it, everything else falls through to the
    requests module (kept as an indirection point for test patching)."""

    @staticmethod
    def new_session(fingerprint_hex: str | None):
        import urllib3
        s = requests.Session()
        if fingerprint_hex:
            # fingerprint IS the trust anchor: disable CA verification and
            # rely solely on the pinned fingerprint (checked by the custom
            # poolmanager). Without verify=False the self-signed cert still
            # fails CA verification before the fingerprint is even checked.
            s.mount("https://", PinnedAdapterHosts.adapter(fingerprint_hex))
            s.verify = False
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        else:
            s.verify = False     # first contact only — pinned immediately after
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return s

    def get(self, url, session=None, **kw):
        if session is not None:
            return session.get(url, **kw)
        return requests.get(url, **kw)   # module indirection: test-patchable

    def post(self, url, session=None, **kw):
        if session is not None:
            return session.post(url, **kw)
        return requests.post(url, **kw)


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
                 on_delivered=None, on_failed=None, on_printer_error=None,
                 http=None, probe_tls=True):
        """resolver(host_id) -> (ip, port) or None  (from discovery.py)
        on_delivered(filepath, host_id, alias)      — job printed
        on_failed(filepath, host_id, alias, reason) — job given up on
        on_printer_error(filepath, host_id, alias, reason) — first failed
        attempt of a job (printer offline, paper out, host error, ...);
        called once per job, before retries continue.
        http: transport override for tests. probe_tls=False skips TOFU
        fingerprint capture (unit tests with fake transport)."""
        self.db, self.my_id, self.my_name = db, my_id, my_name
        self.resolver = resolver
        self.on_delivered = on_delivered
        self.on_failed = on_failed
        self.on_printer_error = on_printer_error
        self.http = http or RequestsHttp()
        self.probe_tls = probe_tls
        self._jobs: queue.Queue = queue.Queue()
        self._pending = 0
        self._ever_failed = False
        self.last_error: str | None = None
        self._lock = threading.Lock()
        # job log for the tray's "Print jobs..." view (newest last)
        self._job_log: dict[int, dict] = {}
        self._job_seq = 0
        self._cancelled: set[int] = set()
        # host_id -> (base_url, valid_until): a route we /ping-verified as
        # really belonging to that ID (audit S2 — stale-IP impersonation).
        self._routes: dict[str, tuple[str, float]] = {}
        # host_id -> pinned TLS session + fingerprint (1.0 transport)
        self._tls_sessions: dict[str, requests.Session] = {}
        self._fps: dict[str, str] = {}
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

    # ---------- transport (HTTPS with pinned host certificate) ----------
    @staticmethod
    def _row_tls_fp(row) -> str | None:
        try:
            return row["tls_fp"] if row else None
        except (KeyError, IndexError):
            return None

    def _session_for(self, host_id: str):
        """Pinned session for a host whose fingerprint we know; None means
        'no fp yet' → caller must capture one via probe before sending."""
        host_id = normalize_id(host_id)
        return self._tls_sessions.get(host_id)

    def _set_host_fp(self, host_id: str, fp: str, persist=True) -> None:
        host_id = normalize_id(host_id)
        self._fps[host_id] = fp
        if host_id not in self._tls_sessions:
            try:
                self._tls_sessions[host_id] = \
                    RequestsHttp.new_session(fp)
            except Exception as e:
                log.warning("could not build pinned session: %r", e)
                return
        if persist:
            try:
                self.db.update_remote_tls_fp(host_id, fp)
            except Exception as e:
                log.debug("tls_fp persist failed: %r", e)

    def _ensure_host_fp(self, host_id: str, base: str) -> bool:
        """True when we have (or just captured) the host's cert fingerprint.
        Missing fp on an existing row = 0.4→1.0 upgrade: one TOFU capture."""
        host_id = normalize_id(host_id)
        if self._fps.get(host_id) or self._tls_sessions.get(host_id):
            return True
        rp = next((r for r in self.db.list_remote_printers(status=None)
                    if normalize_id(r["host_id"]) == host_id), None)
        stored = self._row_tls_fp(rp)
        if stored:
            self._set_host_fp(host_id, stored, persist=False)
            return True
        if not self.probe_tls:
            return False
        ip = base.split("//")[1].split(":")[0]
        port = int(base.rsplit(":", 1)[1])
        fp = probe_fingerprint(ip, port)
        if not fp:
            return False
        log.info("captured host certificate fingerprint %s… (%s)",
                 fp[:12], host_id)
        self._set_host_fp(host_id, fp, persist=bool(rp))
        return True

    # ---------- address resolution + identity verification ----------
    def _base_url(self, host_id: str) -> str | None:
        host_id = normalize_id(host_id)
        found = self.resolver(host_id)
        if found:
            ip, port = found
            self.db.update_remote_host_ip(host_id, ip, port)
            log.info("resolved %s -> %s:%s (mDNS)", host_id, ip, port)
            return f"https://{ip}:{port}"
        rp = next((r for r in self.db.list_remote_printers(status=None)
                    if normalize_id(r["host_id"]) == host_id), None)
        if rp and rp["host_ip"]:
            log.info("resolved %s -> %s:%s (stored IP)", host_id,
                     rp["host_ip"], rp["host_port"])
            return f"https://{rp['host_ip']}:{rp['host_port']}"
        log.warning("could not resolve %s: mDNS miss and no stored IP", host_id)
        return None

    def _identify(self, base: str, host_id: str | None = None) -> str | None:
        """Peer's self-reported ID from /ping; None when unreachable or
        the answer isn't a well-formed 200."""
        host_id = normalize_id(host_id or "")
        try:
            r = self.http.get(f"{base}/ping", timeout=CONNECT_TIMEOUT_S,
                              session=self._session_for(host_id))
        except requests.RequestException as e:
            log.warning("ping %s failed: %r", base, e)
            return None
        if r.status_code != 200:
            return None
        peer = normalize_id(_safe_json(r).get("id", ""))
        return peer or None

    def _verified_base_url(self, host_id: str) -> tuple[str | None, str | None]:
        """Resolve an ID to an address, capture/pin the host certificate,
        and verify that machine's identity before anything sensitive is
        sent to it. Returns (base, error).

        Positive verifications are cached briefly so print retries don't
        re-ping on every attempt; mismatches drop the cached route."""
        host_id = normalize_id(host_id)
        route = self._routes.get(host_id)
        if route and route[1] > time.monotonic():
            return route[0], None
        base = self._base_url(host_id)
        if base is None:
            return None, ("Host not found on the LAN "
                          "(is it online and running PrintLink?)")
        # 1.0: pin the host certificate before the identity check itself —
        # a MITM cannot answer /ping with someone else's ID over TLS unless
        # they also present a cert we pinned at pairing time.
        self._ensure_host_fp(host_id, base)
        peer = self._identify(base, host_id)
        if peer is None:
            return None, f"Host at {base} did not answer its /ping properly."
        if peer != host_id:
            self._routes.pop(host_id, None)
            return None, f"ID mismatch: that IP answers as {peer}."
        self._routes[host_id] = (base, time.monotonic() + ROUTE_VERIFY_TTL_S)
        return base, None

    def _challenge(self, base: str, host_id: str | None = None) -> str | None:
        """Fetch a single-use HMAC challenge. None => the peer cannot do
        HMAC auth (pre-1.0 agent or endpoint failure)."""
        try:
            r = self.http.get(f"{base}/auth-challenge", session=self._session_for(host_id or ""),
                              params={"sender_id": self.my_id},
                              timeout=CONNECT_TIMEOUT_S)
        except requests.RequestException as e:
            log.warning("auth-challenge %s failed: %r", base, e)
            return None
        nonce = _safe_json(r).get("nonce")
        if not nonce:
            log.info("no challenge from %s (pre-0.3 host? HTTP %d)",
                     base, r.status_code)
        return nonce

    # ---------- share request ----------
    def request_share(self, host_id: str, printer_alias: str, days: int,
                      name: str | None = None) -> tuple[bool, str]:
        if not is_valid_id(host_id):
            log.warning("request_share: invalid ID %r", host_id)
            return False, "Invalid ID format."
        base, err = self._verified_base_url(host_id)
        if base is None:
            log.warning("request_share: %s", err)
            return False, err
        # TOFU capture for first-time pairing (no db row yet): pin whatever
        # cert this server presents, then sanity-check it against the
        # fingerprint the host signs into its acceptance response.
        self._ensure_host_fp(host_id, base)
        try:
            log.info("request_share: POST %s/request-share alias=%r days=%d",
                     base, printer_alias, days)
            r = self.http.post(f"{base}/request-share", session=self._session_for(host_id),
                               json={"sender_id": self.my_id,
                                     "sender_name": self.my_name,
                                     "printer_alias": printer_alias,
                                     "days": days}, timeout=60)
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
        announced = j.get("tls_fp")
        captured = self._fps.get(normalize_id(host_id))
        if announced and captured and announced != captured:
            log.error("request_share: certificate changed between probe and "
                      "accept (%s… != %s…) — refusing", captured[:12],
                      announced[:12])
            return False, "Host certificate changed during pairing — abort."
        if r.status_code == 200 and j.get("status") == "accepted":
            ip = base.split("//")[1].split(":")[0]
            store_accepted_share(self.db, host_id, "", ip, printer_alias,
                                 j["token"], j["expires_at"], name=name,
                                 tls_fp=announced or captured)
            log.info("request_share: ACCEPTED for %s (%s) token saved",
                     host_id, printer_alias)
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
        base, err = self._verified_base_url(host_id)
        if base is None:
            return False, f"{err} — local entry removed anyway"
        payload: dict = {"sender_id": self.my_id,
                         "printer_alias": printer_alias}
        nonce = self._challenge(base, host_id)
        if not nonce:
            return False, ("host runs an old PrintLink without secure "
                           "revocation — update it; entry removed locally")
        # prove ownership without sending the token
        payload["nonce"] = nonce
        payload["signature"] = sign_nonce(rp["token"], nonce)
        try:
            r = self.http.post(f"{base}/revoke-grant", session=self._session_for(host_id),
                               json=payload, timeout=CONNECT_TIMEOUT_S)
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
        jid = self._job_register(str(filepath), normalize_id(host_id),
                                 printer_alias, bool(delete_after),
                                 dict(options or {}))
        self._jobs.put((str(filepath), normalize_id(host_id), printer_alias, 0,
                        bool(delete_after), dict(options or {}), jid))
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

    # ---------- job log (tray "Print jobs..." view) ----------
    def _job_register(self, filepath: str, host_id: str, alias: str,
                      delete_after: bool, options: dict) -> int:
        with self._lock:
            self._job_seq += 1
            jid = self._job_seq
            self._job_log[jid] = {
                "id": jid, "file": filepath, "host": host_id, "alias": alias,
                "delete_after": delete_after, "options": options,
                "status": "queued", "attempts": 0, "error": None,
                "added": time.strftime("%H:%M:%S"),
            }
            # keep the log bounded: drop oldest finished entries first
            over = len(self._job_log) - 60
            if over > 0:
                done = [k for k in sorted(self._job_log)
                        if self._job_log[k]["status"]
                        in ("delivered", "failed", "cancelled")][:over]
                for k in done:
                    self._job_log.pop(k)
            return jid

    def _job_set(self, jid: int, status: str | None = None,
                 error: str | None = None, attempts: int | None = None) -> None:
        with self._lock:
            j = self._job_log.get(jid)
            if not j:
                return
            if status:
                j["status"] = status
            if attempts is not None:
                j["attempts"] = attempts
            if error is not None:        # "" clears the stored error
                j["error"] = str(error)[:200] or None

    def list_jobs(self) -> list[dict]:
        """Snapshot for the tray view, newest first."""
        with self._lock:
            return [dict(v) for v in
                    sorted(self._job_log.values(),
                           key=lambda j: j["id"], reverse=True)]

    def cancel_job(self, job_id: int) -> tuple[bool, str]:
        """Best-effort cancel: jobs already on the wire finish; queued ones
        are skipped by the retry loop. Never deletes the user's document."""
        with self._lock:
            j = self._job_log.get(job_id)
            if not j:
                return False, "No such job."
            if j["status"] in ("delivered", "failed", "cancelled"):
                return False, f"Job already {j['status']}."
            self._cancelled.add(job_id)
        return True, "Job will be cancelled."

    def retry_job(self, job_id: int) -> tuple[bool, str]:
        """Re-queue a failed/cancelled job whose file still exists."""
        with self._lock:
            j = self._job_log.get(job_id)
            if not j:
                return False, "No such job."
            if j["status"] not in ("failed", "cancelled"):
                return False, f"Only failed jobs can be retried (this one is {j['status']})."
        if not os.path.isfile(j["file"]):
            return False, "The document no longer exists on disk."
        self._cancelled.discard(job_id)
        self._jobs.put((j["file"], j["host"], j["alias"], 0,
                        j["delete_after"], dict(j["options"]), job_id))
        self._job_set(job_id, status="queued", error="")
        with self._lock:
            was_idle = self._pending == 0
            self._pending += 1
            if was_idle:
                self._ever_failed = False   # user-driven retry: fresh cycle
            self._drained.clear()
        return True, "Job re-queued."

    def _send_once(self, filepath: str, host_id: str,
                   printer_alias: str, options: dict | None = None
                   ) -> tuple[bool, bool]:
        """Try once. Returns (delivered, retryable).

        Retryable: connection errors and HTTP 503 (printer offline, may
        recover). Permanent (give up immediately): HTTP 400/403/500 etc. —
        the receiver answered and the job cannot succeed as-is.
        """
        base, err = self._verified_base_url(host_id)
        rp = self.db.get_remote_printer(host_id, printer_alias)
        if base is None or rp is None:
            log.warning("send %s: no verified route to %s (base=%s rp=%s)",
                        filepath, host_id,
                        "ok" if base else err, "missing" if rp is None else "ok")
            return False, True
        try:
            with open(filepath, "rb") as f:
                raw = f.read()
            payload = encrypt_payload(raw, rp["token"])
            # make sure the pinned session exists before anything sensitive
            self._ensure_host_fp(host_id, base)
            nonce = self._challenge(base, host_id)
            if not nonce:
                # 1.0 removed the plaintext X-Token fallback: a host without
                # HMAC support cannot be talked to securely.
                self.last_error = ("Host did not offer an auth challenge — "
                                   "it runs an old PrintLink. Update "
                                   "PrintLink on BOTH PCs.")
                log.warning("send %s: no challenge from %s; giving up",
                            filepath, base)
                return False, False
            headers = {"X-Sender-ID": self.my_id,
                       "X-Token-Hint": token_hint(rp["token"]),
                       "X-Nonce": nonce,
                       "X-Signature": sign_nonce(rp["token"], nonce)}
            log.info("send %s -> %s/print (%d -> %d encrypted bytes, alias=%s, auth=hmac)",
                     filepath, base, len(raw), len(payload), printer_alias)
            data = None
            if options:
                data = {"options": json.dumps(options)}
            r = self.http.post(f"{base}/print", session=self._session_for(host_id),
                               headers=headers,
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
            for filepath, host_id, alias, attempts, delete_after, options, jid \
                    in pending:
                if jid in self._cancelled:
                    self._cancelled.discard(jid)
                    self._job_set(jid, status="cancelled")
                    self._job_done(filepath, host_id, alias, False,
                                   "cancelled by user")
                    continue
                log.info("retry_loop: attempt %d for %s@%s (%s)",
                         attempts + 1, alias, host_id, Path(filepath).name)
                self._job_set(jid, status="sending", attempts=attempts + 1)
                ok, retryable = self._send_once(filepath, host_id, alias, options)
                reason = self.last_error or "unknown error"
                if ok:
                    if delete_after:
                        self._cleanup(filepath)
                    log.info("retry_loop: delivered %s to %s@%s",
                             Path(filepath).name, alias, host_id)
                    self._job_set(jid, status="delivered", error="")
                    self._job_done(filepath, host_id, alias, True)
                    continue  # delivered
                if attempts == 0 and self.on_printer_error:
                    # first failed attempt: tell the user what's wrong now,
                    # before the silent retry period starts
                    try:
                        self.on_printer_error(filepath, host_id, alias, reason)
                    except Exception:
                        log.exception("printer-error callback failed for %s",
                                      filepath)
                give_up = not retryable or attempts + 1 >= max_attempts
                if give_up:
                    why = ("receiver rejected" if not retryable
                           else f"after {max_attempts} attempts")
                    log.error("retry_loop: giving up on %s (%s): %s",
                              Path(filepath).name, why, reason)
                    if delete_after:
                        self._cleanup(filepath)
                    self._job_set(jid, status="failed", error=reason)
                    self._job_done(filepath, host_id, alias, False, reason)
                    continue
                still.append((filepath, host_id, alias, attempts + 1,
                              delete_after, options, jid))
                self._job_set(jid, status="queued", error=reason)
            pending = still
            if pending:
                log.info("retry_loop: %d job(s) pending, retrying in %ds",
                         len(pending), RETRY_INTERVAL_S)
                time.sleep(RETRY_INTERVAL_S)

    def stop(self):
        self._stop.set()
