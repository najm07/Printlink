"""PrintLink pipe reader: receives jobs from the C# port monitor.

The monitor (PrintLinkMonitor.dll) connects to \\\\.\\pipe\\PrintLinkSender and
streams each spooled job. Framing:
  [4B name length][name UTF-8][8B payload length (0 = unknown, read to EOF)][payload]

The user picks the remote destination in the tray BEFORE printing; this reader
hands the saved file to Sender.print_file() with the currently selected target.

Robustness: the despool can sit idle for minutes between OpenPort and its
first probe, so reads use OVERLAPPED I/O with a generous per-read timeout,
and each connection is handled on its OWN thread so a stalled connection
never blocks the accept loop (which would wedge every later job).
"""
import os
import struct
import tempfile
import threading
import time
from pathlib import Path

from config import PIPE_NAME, OUTBOX_DIR_NAME
from logutil import get_logger

log = get_logger("pipe_reader")

_jobs_dir = Path(tempfile.gettempdir()) / OUTBOX_DIR_NAME

IDLE_TIMEOUT_MS = 600_000  # 10 min: spooler may stall minutes between writes

_ERROR_BROKEN_PIPE = 109
_ERROR_HANDLE_EOF = 38
_ERROR_IO_PENDING = 997


class PipeReader:
    def __init__(self, sender, get_target):
        """sender: Sender instance. get_target() -> (host_id, printer_alias) or None,
        provided by tray.py (the user's currently chosen remote printer)."""
        self.sender, self.get_target = sender, get_target
        self._stop = threading.Event()
        _jobs_dir.mkdir(exist_ok=True)

    def start(self):
        threading.Thread(target=self._serve, daemon=True,
                         name="printlink-pipe").start()

    # --- Windows named pipe via pywin32 (imported lazily for testability) ---
    def _serve(self):
        import win32pipe, win32file, win32security
        sd = win32security.SECURITY_DESCRIPTOR()
        sd.SetSecurityDescriptorDacl(1, None, 0)  # null DACL: spoolsv (SYSTEM) connects
        sa = win32security.SECURITY_ATTRIBUTES()
        sa.bInheritHandle = False
        sa.SECURITY_DESCRIPTOR = sd
        log.info("pipe listener up on %s", PIPE_NAME)
        while not self._stop.is_set():
            pipe = win32pipe.CreateNamedPipe(
                PIPE_NAME,
                win32pipe.PIPE_ACCESS_INBOUND | win32file.FILE_FLAG_OVERLAPPED,
                win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_WAIT,
                win32pipe.PIPE_UNLIMITED_INSTANCES, 0, 65536, 0,
                sa)
            try:
                win32pipe.ConnectNamedPipe(pipe, None)
                log.info("accepted connection from port monitor (spoolsv)")
            except pywintypes.error as e:
                win32file.CloseHandle(pipe)
                if not self._stop.is_set():
                    log.warning("pipe accept error: %r", e)
                continue
            threading.Thread(target=self._handle_job, args=(pipe,), daemon=True,
                             name="printlink-job").start()

    def _handle_job(self, pipe):
        import win32file, win32event, pywintypes
        ov = pywintypes.OVERLAPPED()
        ov.hEvent = win32event.CreateEvent(None, 0, 0, None)

        def read_some(size):
            """One overlapped read. Returns bytes (empty = EOF), or None on
            idle timeout."""
            try:
                win32event.ResetEvent(ov.hEvent)
                rc, buf = win32file.ReadFile(pipe, size, ov)
            except pywintypes.error as e:
                if e.winerror in (_ERROR_BROKEN_PIPE, _ERROR_HANDLE_EOF):
                    return b""
                log.warning("pipe read error: %r", e)
                return b""
            if rc == _ERROR_IO_PENDING:
                if win32event.WaitForSingleObject(ov.hEvent, IDLE_TIMEOUT_MS) \
                        == win32event.WAIT_TIMEOUT:
                    win32file.CancelIo(pipe)
                    log.warning("pipe idle timeout (%d ms) - abandoning connection",
                                IDLE_TIMEOUT_MS)
                    return None
            try:
                n = win32file.GetOverlappedResult(pipe, ov, False)
                if isinstance(n, tuple):   # older pywin32: (hr, data) tuple
                    n = len(n[1])
            except pywintypes.error as e:
                if e.winerror in (_ERROR_BROKEN_PIPE, _ERROR_HANDLE_EOF):
                    return b""
                log.warning("pipe overlapped result error: %r", e)
                return b""
            if n <= 0:
                return b""
            return bytes(buf[:n])

        def read_exact(n: int) -> bytes:
            buf = b""
            while len(buf) < n:
                data = read_some(n - len(buf))
                if data is None:
                    raise EOFError("idle timeout mid-frame")
                if not data:
                    raise EOFError("pipe closed mid-frame")
                buf += data
            return buf

        name_len = struct.unpack("<I", read_exact(4))[0]
        job_name = read_exact(name_len).decode("utf-8", "replace")
        payload_len = struct.unpack("<Q", read_exact(8))[0]
        log.info("job header: name=%r payload_len=%s", job_name,
                 payload_len if payload_len else "streamed")

        suffix = Path(job_name).suffix or ".xps"
        fd, path = tempfile.mkstemp(suffix=suffix, dir=_jobs_dir,
                                    prefix=f"{int(time.time())}_")
        total = 0
        with os.fdopen(fd, "wb") as out:
            if remaining := payload_len:  # fixed length
                while remaining:
                    data = read_some(min(65536, remaining))
                    if data is None:
                        break  # idle timeout: keep what we got
                    if not data:
                        break
                    out.write(data); remaining -= len(data); total += len(data)
            else:          # streamed: read until the monitor closes the pipe
                while True:
                    data = read_some(65536)
                    if data is None:
                        break  # idle timeout: treat as end of job
                    if not data:
                        break
                    out.write(data); total += len(data)
        log.info("saved %d bytes to %s", total, path)

        try:
            import shutil
            debug_dir = Path(tempfile.gettempdir()) / "printlink_debug"
            debug_dir.mkdir(exist_ok=True)
            shutil.copy2(path, debug_dir / Path(path).name)
            with open(path, "rb") as f:
                head = f.read(64)
            log.info("payload head: %s",
                     " ".join(f"{b:02x}" for b in head))
        except Exception:
            pass  # debug copy is best-effort

        if total == 0:
            os.unlink(path)
            log.warning("job discarded: empty payload (probe/stale connection)")
            return
        target = self.get_target()
        if target is None:
            os.unlink(path)
            log.warning("job discarded: no remote printer selected in tray")
            return
        host_id, alias = target
        log.info("target selected: %s @ %s -> handing to sender", alias, host_id)
        ok, msg = self.sender.print_file(path, host_id, alias,
                                         delete_after=True)  # our temp file
        log.info("sender.print_file(%s) -> ok=%s msg=%r", path, ok, msg)

    def stop(self):
        self._stop.set()
