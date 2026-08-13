"""PrintLink receiver: Flask HTTP API hosted on every PC.

Endpoints:
  POST /request-share   client asks to use one of our printers (shows tray dialog)
  POST /print           authenticated print job (PDF/image upload)
  GET  /printers        list printers this host shares (for mDNS-less browsing)
  GET  /ping            liveness + host ID

Run in a background thread from the tray app.
"""
import json
import os
import tempfile
import threading
from pathlib import Path
from flask import Flask, request, jsonify
from cryptography.exceptions import InvalidTag

from db import Database
from shares import create_grant, authorize_print, DEFAULT_SHARE_DAYS
from printer_local import (list_printers, printer_status, print_via_shell,
                           print_text, print_emf, print_word, print_image,
                           print_pdf, sniff_format, extract_emf,
                           DEFAULT_OPTIONS)
from identity import normalize_id
from crypto import decrypt_payload
from config import LISTEN_PORT, MAX_JOB_MB, INBOX_DIR_NAME
from logutil import get_logger

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
    log.info("HTTP server created on port %d (id=%s, max job %d MB)",
             LISTEN_PORT, my_id, MAX_JOB_MB)

    @app.get("/ping")
    def ping():
        log.info("GET /ping from %s", request.remote_addr)
        return jsonify({"id": my_id, "ok": True})

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
        data = request.get_json(force=True)
        sender_id = normalize_id(data.get("sender_id", ""))
        sender_name = data.get("sender_name", "unknown")
        alias = data.get("printer_alias", "")
        days = int(data.get("days", DEFAULT_SHARE_DAYS))
        log.info("POST /request-share from %s: %s (%s) wants '%s' for %d days",
                 request.remote_addr, sender_name, sender_id, alias, days)

        shared = next((p for p in db.list_shared_printers() if p["alias"] == alias), None)
        if shared is None:
            log.warning("request-share refused: '%s' is not a shared printer", alias)
            return jsonify({"status": "refused", "reason": "printer not shared"}), 404

        # Ask the local user via the tray callback
        accepted = on_share_request(sender_id, sender_name, alias, days) if on_share_request else False
        if not accepted:
            log.info("request-share: local user DECLINED %s", sender_id)
            return jsonify({"status": "refused", "reason": "user declined"}), 403

        grant = create_grant(db, sender_id, sender_name, shared["id"], days=days)
        log.info("request-share ACCEPTED: grant for %s on '%s' expires %s",
                 sender_id, alias, grant["expires_at"])
        return jsonify({"status": "accepted", "token": grant["token"],
                        "expires_at": grant["expires_at"]})

    @app.post("/print")
    def receive_print():
        sender_id = request.headers.get("X-Sender-ID", "")
        token = request.headers.get("X-Token", "")
        log.info("POST /print from %s (sender_id=%s token=%s...)", request.remote_addr,
                 sender_id, (token[:8] + "…") if token else "MISSING")
        auth = authorize_print(db, sender_id, token)
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
                 upload.filename, len(raw))

        try:
            payload = decrypt_payload(raw, auth["grant"]["token"])
            log.info("print: decrypted %d bytes", len(payload))
        except InvalidTag:
            log.error("print: decrypt FAILED (bad token / tampered payload)")
            return jsonify({"error": "decrypt failed"}), 500
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
            return jsonify({"error": str(e)}), 500
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return app


def run_in_thread(app: Flask, port: int = LISTEN_PORT) -> threading.Thread:
    log.info("HTTP server listening on 0.0.0.0:%d", port)
    t = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False),
        daemon=True, name="printlink-server")
    t.start()
    return t
