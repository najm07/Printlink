"""PrintLink receiver: Flask HTTP API hosted on every PC.

Endpoints:
  POST /request-share   client asks to use one of our printers (shows tray dialog)
  POST /print           authenticated print job (PDF/image upload)
  GET  /printers        list printers this host shares (for mDNS-less browsing)
  GET  /ping            liveness + host ID

Run in a background thread from the tray app.
"""
import json
import io
import os
import ssl
import tempfile
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, unquote
from flask import Flask, request, jsonify
from cryptography.exceptions import InvalidTag

from db import Database
from shares import (create_grant, authorize_print_proof,
                    revoke_remote_share_proof, DEFAULT_SHARE_DAYS)
from printer_local import (printer_status, print_via_shell,
                           print_text, print_emf, print_word, print_image,
                           print_pdf, sniff_format, extract_emf,
                           DEFAULT_OPTIONS)
from identity import is_valid_id, normalize_id
from crypto import decrypt_payload
from auth import new_nonce
from tlsutil import generate_host_cert, load_cert_fingerprint
from config import (LISTEN_PORT, MAX_JOB_MB, INBOX_DIR_NAME, VERSION,
                    MAX_SHARE_DAYS, RATE_SHARE_WINDOW_S, RATE_SHARE_MAX,
                    AUTH_NONCE_TTL_S, AUTH_MAX_NONCES, TLS_CERT_FILE)
from logutil import get_logger, clean

log = get_logger("server")


def _parse_options(raw: str | None) -> dict | None:
    """Validate the optional 'options' form field; None when absent/empty."""
    if not raw or not raw.strip():
        return {}
    try:
        opts = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(opts, dict):
        return None
    return {k: opts.get(k) for k in DEFAULT_OPTIONS
            if opts.get(k) not in (None, "")}


def create_app(db: Database, my_id: str, on_share_request=None) -> Flask:
    """on_share_request(sender_id, sender_name, printer_alias, days) -> bool
    Callback wired by the tray app to show the accept/refuse dialog."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_JOB_MB * 1024 * 1024
    queue_dir = Path(tempfile.gettempdir()) / INBOX_DIR_NAME
    queue_dir.mkdir(exist_ok=True)
    # persistent host identity: every grant is bound to this certificate
    try:
        generate_host_cert(TLS_CERT_FILE, normalize_id(my_id))
        tls_fp = load_cert_fingerprint(TLS_CERT_FILE)
    except OSError as e:
        log.error("TLS identity unavailable (%r) — HTTPS cannot start", e)
        raise
    log.info("HTTP server created on port %d (id=%s, max job %d MB)",
             LISTEN_PORT, my_id, MAX_JOB_MB)

    # Sliding-window rate limit for /request-share: the endpoint is
    # unauthenticated and each accepted call pops a modal dialog on the
    # host user's screen, so a looped requester is a spam/DoS vector.
    share_hits: dict[str, deque] = {}

    def _rate_limited(ip: str) -> bool:
        now = time.monotonic()
        hits = share_hits.setdefault(ip, deque())
        while hits and now - hits[0] > RATE_SHARE_WINDOW_S:
            hits.popleft()
        if len(hits) >= RATE_SHARE_MAX:
            return True
        hits.append(now)
        if len(share_hits) > 256:  # bounded memory against IP rotation
            stale = [k for k, v in share_hits.items()
                     if not v or now - v[-1] > RATE_SHARE_WINDOW_S]
            for k in stale:
                share_hits.pop(k, None)
        return False

    # Single-use auth challenges: nonce -> expiry (monotonic). Lost on
    # restart by design — senders fetch a fresh challenge per attempt.
    nonces: dict[str, float] = {}

    def _issue_nonce(sender_id: str) -> str | None:
        if not is_valid_id(sender_id):
            return None
        now = time.monotonic()
        expired = [k for k, exp in nonces.items() if exp < now]
        for k in expired:
            nonces.pop(k, None)
        while len(nonces) >= AUTH_MAX_NONCES:
            nonces.pop(min(nonces, key=lambda k: nonces[k]))
        nonce = new_nonce()
        nonces[nonce] = now + AUTH_NONCE_TTL_S
        return nonce

    def _take_nonce(nonce: str) -> bool:
        """Pop a stored nonce; True when it existed and was still fresh."""
        exp = nonces.pop(nonce, None)
        return exp is not None and exp >= time.monotonic()

    def _authenticate(sender_id: str) -> dict:
        """HMAC proof only — the pre-1.0 X-Token path was removed."""
        hint = request.headers.get("X-Token-Hint", "")
        nonce = request.headers.get("X-Nonce", "")
        sig = request.headers.get("X-Signature", "")
        if hint and nonce and sig:
            if not _take_nonce(nonce):
                return {"ok": False, "error": "invalid or expired challenge"}
            return authorize_print_proof(db, sender_id, hint, nonce, sig)
        if request.headers.get("X-Token"):
            return {"ok": False,
                    "error": "token auth was removed in PrintLink 1.0 — "
                             "update PrintLink on the sending PC"}
        return {"ok": False, "error": "missing credentials"}

    @app.get("/ping")
    def ping():
        log.info("GET /ping from %s", request.remote_addr)
        return jsonify({"id": my_id, "ok": True, "version": VERSION,
                        "tls": True})

    @app.get("/auth-challenge")
    def auth_challenge():
        sender_id = normalize_id(request.args.get("sender_id", ""))
        nonce = _issue_nonce(sender_id)
        if nonce is None:
            return jsonify({"error": "bad sender_id"}), 400
        log.info("GET /auth-challenge from %s (sender %s)",
                 request.remote_addr, sender_id)
        return jsonify({"nonce": nonce})

    @app.get("/printers")
    def printers():
        rows = db.list_shared_printers()
        log.info("GET /printers from %s (%d shared)", request.remote_addr, len(rows))
        return jsonify([
            {"alias": p["alias"], "status": printer_status(p["local_name"])}
            for p in rows
        ])

    @app.post("/request-share")
    def request_share():
        if _rate_limited(request.remote_addr or "?"):
            log.warning("POST /request-share from %s: rate-limited",
                        request.remote_addr)
            return jsonify({"status": "refused",
                            "reason": "too many requests"}), 429
        data = request.get_json(force=True)
        sender_id = normalize_id(data.get("sender_id", ""))
        sender_name = clean(data.get("sender_name") or "unknown")
        alias = clean(data.get("printer_alias"))
        try:
            days = int(data.get("days", DEFAULT_SHARE_DAYS))
        except (TypeError, ValueError):
            log.warning("request-share: bad 'days' value %r from %s",
                        data.get("days"), request.remote_addr)
            return jsonify({"status": "refused", "reason": "invalid days"}), 400
        # Server-side clamp: the client dialog caps at 90, but the wire is
        # unauthenticated — a crafted 999999-day request must not stick.
        days = max(1, min(days, MAX_SHARE_DAYS))
        log.info("POST /request-share from %s: %s (%s) wants '%s' for %d days",
                 request.remote_addr, sender_name, sender_id, alias, days)

        shared = next((p for p in db.list_shared_printers()
                       if p["alias"] == data.get("printer_alias")), None)
        if shared is None:
            log.warning("request-share refused: '%s' is not a shared printer",
                        alias)
            return jsonify({"status": "refused", "reason": "printer not shared"}), 404

        # Ask the local user via the tray callback
        accepted = on_share_request(sender_id, sender_name,
                                    data.get("printer_alias", ""), days) \
            if on_share_request else False
        if not accepted:
            log.info("request-share: local user DECLINED %s", sender_id)
            return jsonify({"status": "refused", "reason": "user declined"}), 403

        grant = create_grant(db, sender_id, sender_name, shared["id"], days=days,
                             printer_alias=shared["alias"])
        log.info("request-share ACCEPTED: grant for %s on '%s' expires %s",
                 sender_id, alias, grant["expires_at"])
        return jsonify({"status": "accepted", "token": grant["token"],
                        "expires_at": grant["expires_at"],
                        "tls_fp": tls_fp})

    @app.post("/revoke-grant")
    def revoke_grant():
        data = request.get_json(force=True)
        sender_id = normalize_id(data.get("sender_id", ""))
        alias_raw = data.get("printer_alias", "")
        alias = clean(alias_raw)
        log.info("POST /revoke-grant from %s for '%s'", request.remote_addr, alias)
        nonce, sig = data.get("nonce"), data.get("signature")
        if nonce and sig:
            if not _take_nonce(nonce):
                res = {"ok": False, "error": "invalid or expired challenge"}
            else:
                res = revoke_remote_share_proof(db, sender_id, alias_raw,
                                                nonce, sig)
        elif data.get("token"):
            res = {"ok": False,
                   "error": "token auth was removed in PrintLink 1.0 — "
                            "update PrintLink on the sending PC"}
        else:
            res = {"ok": False, "error": "missing credentials"}
        if not res["ok"]:
            log.warning("revoke-grant FAILED for %s '%s': %s",
                        sender_id, alias, res["error"])
            return jsonify({"status": "refused", "reason": res["error"]}), 404
        log.info("revoke-grant OK: %s no longer has '%s'", sender_id, alias)
        return jsonify({"status": "revoked"})

    @app.post("/print")
    def receive_print():
        sender_id = normalize_id(request.headers.get("X-Sender-ID", ""))
        log.info("POST /print from %s (sender_id=%s)", request.remote_addr,
                 sender_id)
        auth = _authenticate(sender_id)
        if not auth["ok"]:
            log.warning("print auth FAILED for %s: %s", sender_id, auth["error"])
            return jsonify({"error": auth["error"]}), 403
        log.info("print auth OK: %s on '%s'", sender_id, auth["printer"]["alias"])

        upload = request.files.get("file")
        if upload is None:
            log.warning("print: no file part in upload from %s", sender_id)
            return jsonify({"error": "no file"}), 400
        raw = upload.read()
        log.info("print: upload.filename=%r bytes=%d (encrypted)",
                 clean(upload.filename or "", 80), len(raw))

        try:
            payload = decrypt_payload(raw, auth["grant"]["token"])
            log.info("print: decrypted %d bytes", len(payload))
        except InvalidTag:
            log.error("print: decrypt FAILED (bad token / tampered payload)")
            return jsonify({"error": "decrypt failed"}), 401
        except ValueError:
            log.error("print: decrypt FAILED (invalid payload format)")
            return jsonify({"error": "invalid payload"}), 400

        opts = _parse_options(request.form.get("options"))
        if opts is None:
            log.warning("print: malformed options field: %r",
                        request.form.get("options"))
            return jsonify({"error": "invalid options"}), 400
        if opts:
            log.info("print: options=%r", opts)

        fmt = sniff_format(payload)
        suffix = {"pdf": ".pdf", "xps": ".xps", "emf": ".emf",
                  "text": ".txt", "binary": ".bin", "docx": ".docx",
                  "doc": ".doc", "xlsx": ".xlsx", "pptx": ".pptx",
                  "png": ".png", "jpg": ".jpg", "gif": ".gif",
                  "bmp": ".bmp", "webp": ".webp", "tiff": ".tiff"}[fmt]
        fd, path = tempfile.mkstemp(suffix=suffix, dir=queue_dir)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(payload)
            printer_name = auth["printer"]["local_name"]
            log.info("print: staged to %s (fmt=%s), target printer '%s'",
                     path, fmt, printer_name)
            st = printer_status(printer_name)
            log.info("print: printer status offline=%s paused=%s error=%s jobs=%d port=%s",
                     st["offline"], st["paused"], st["error"], st["jobs_queued"], st["port"])
            if st["offline"]:
                log.warning("print: printer '%s' is OFFLINE", printer_name)
                return jsonify({"error": "printer is offline"}), 503
            if fmt == "emf":
                emf = extract_emf(payload)
                if emf is None:
                    raise RuntimeError("EMF classified but not extractable")
                print_emf(emf, printer_name,
                          f"PrintLink {upload.filename or 'job'}")
                log.info("print: EMF rendered on '%s' (%d bytes)", printer_name, len(emf))
            elif fmt == "pdf":
                print_pdf(path, printer_name, opts)
                log.info("print: PDF printed on '%s' (options=%r)", printer_name, opts)
            elif fmt == "text":
                try:
                    copies = max(1, int(opts.get("copies") or 1))
                except (TypeError, ValueError):
                    copies = 1
                print_text(payload, printer_name,
                           f"PrintLink {upload.filename or 'job'}", copies=copies)
                log.info("print: text spooled via TEXT datatype on '%s' "
                         "(copies=%d)", printer_name, copies)
            elif fmt in ("docx", "doc"):
                print_word(path, printer_name, opts)
                log.info("print: Word document printed on '%s' (%s)",
                         printer_name, fmt)
            elif fmt in ("png", "jpg", "gif", "bmp", "webp", "tiff"):
                print_image(path, printer_name, opts)
                log.info("print: image %s rendered on '%s'", fmt, printer_name)
            else:
                if opts:
                    log.warning("print: options not applicable to %s; ignored", fmt)
                print_via_shell(path, printer_name)
            log.info("print: job spooled+printed on '%s' (alias %s)",
                     printer_name, auth["printer"]["alias"])
            return jsonify({"status": "accepted", "printer": auth["printer"]["alias"]})
        except Exception as e:
            log.error("print: FAILED on '%s': %r", auth["printer"]["local_name"], e)
            # Generic body: exception text can carry local paths/hostnames.
            return jsonify({"error": "print failed"}), 500
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return app


def run_in_thread(app: Flask, port: int = LISTEN_PORT,
                  tls_cert: Path | None = None) -> threading.Thread:
    """Serve the app on a daemon thread over TLS (stdlib threaded WSGI).

    Deliberately not waitress/gunicorn: they are proxy-oriented and this
    waitress build ships no TLS. A ThreadingHTTPServer wrapped in an
    SSLContext is plenty for LAN print traffic and keeps us stdlib-only."""
    tls = tls_cert or TLS_CERT_FILE
    ctx = None
    scheme = "HTTP"
    if Path(tls).exists():
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(tls))          # PEM holds key + cert together
        scheme = "HTTPS"
    log.info("%s server listening on 0.0.0.0:%d", scheme, port)

    class Handler(_TLSWSGIHandler):
        app_ref = app
        max_body = MAX_JOB_MB * 1024 * 1024

    httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    httpd.daemon_threads = True
    httpd.tls = ctx is not None
    if ctx is not None:
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    def _serve():
        try:
            httpd.serve_forever()
        except Exception:
            log.exception("server crashed")

    threading.Thread(target=_serve, daemon=True,
                     name="printlink-server").start()
    return None


class _TLSWSGIHandler(BaseHTTPRequestHandler):
    """Minimal WSGI bridge over BaseHTTPRequestHandler (GET/POST/HEAD —
    the whole PrintLink API surface)."""
    protocol_version = "HTTP/1.1"
    app_ref = None
    max_body = 100 * 1024 * 1024

    def _wsgi_env(self, body: bytes) -> dict:
        parsed = urlsplit(self.path)
        addr = self.server.server_address
        env = {
            "REQUEST_METHOD": self.command,
            "PATH_INFO": unquote(parsed.path),
            "QUERY_STRING": parsed.query,
            "SERVER_NAME": str(addr[0]),
            "SERVER_PORT": str(addr[1]),
            "SERVER_PROTOCOL": self.request_version,
            "CONTENT_LENGTH": str(len(body)),
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "https" if getattr(self.server, "tls", False)
                               else "http",
            "wsgi.input": io.BytesIO(body),
            "wsgi.errors": io.StringIO(),
            "wsgi.multithread": True,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
        }
        for k, v in self.headers.items():
            env["HTTP_" + k.upper().replace("-", "_")] = v
        return env

    def _dispatch(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length > self.max_body:
            self.send_error(413, "Payload too large")
            return
        body = self.rfile.read(length) if length else b""
        captured: dict = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        try:
            result = self.app_ref(self._wsgi_env(body), start_response)
            try:
                payload = b"".join(result)
            finally:
                if hasattr(result, "close"):
                    result.close()
        except Exception:
            log.exception("request %s %s crashed", self.command, self.path)
            try:
                self.send_error(500, "internal error")
            except OSError:
                pass
            return
        code = int(captured["status"].split()[0])
        headers = [(k, v) for k, v in captured["headers"]
                   if k.lower() not in ("content-length", "connection",
                                        "transfer-encoding")]
        self.send_response(code)
        self.send_header("Content-Length", str(len(payload)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = _dispatch
    do_POST = _dispatch
    do_HEAD = _dispatch

    def log_message(self, fmt, *args):     # route into printlink logging
        log.debug("%s %s", self.address_string(), fmt % args)
