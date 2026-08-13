"""PrintLink local printing: enumerate and print via Windows spooler (pywin32).

PDFs/pictures are printed by delegating to the shell association ("printto"),
which works for PDF (Edge/Adobe/Sumatra registered handler) and images
(Photos/mspaint). Raw data path is provided for future ESC/POS needs.
"""
import os
import time
import win32print
import win32api

from logutil import get_logger

log = get_logger("printer_local")


def list_printers() -> list[str]:
    """All locally installed printers (USB, network, virtual)."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def default_printer() -> str:
    return win32print.GetDefaultPrinter()


def printer_status(printer_name: str) -> dict:
    """Online/offline/paused and job count for a given printer."""
    h = win32print.OpenPrinter(printer_name)
    try:
        info = win32print.GetPrinter(h, 2)
        status = info["Status"]
        attrs = info["Attributes"]
        return {
            "name": printer_name,
            "offline": bool(status & win32print.PRINTER_STATUS_OFFLINE),
            "paused": bool(status & win32print.PRINTER_STATUS_PAUSED),
            "error": bool(status & (win32print.PRINTER_STATUS_ERROR
                                    | win32print.PRINTER_STATUS_PAPER_OUT
                                    | win32print.PRINTER_STATUS_NO_TONER)),
            "jobs_queued": info["cJobs"],
            "port": info["pPortName"],
        }
    finally:
        win32print.ClosePrinter(h)


def print_via_shell(filepath: str, printer_name: str, timeout: int = 120) -> None:
    """Print PDF/image through the registered shell handler ('printto' verb).

    PDFs go through SumatraPDF's CLI when installed (file-association
    independent, survives broken/missing default app), else the 'printto'
    verb is used. Images always use 'printto' (Photos/mspaint).
    Raises RuntimeError if no handler works or the job doesn't complete.
    """
    if os.path.splitext(filepath)[1].lower() == ".pdf":
        sumatra = _find_sumatra()
        if sumatra:
            _print_with_sumatra(sumatra, filepath, printer_name, timeout)
            return
    try:
        win32api.ShellExecute(0, "printto", os.path.abspath(filepath),
                              f'"{printer_name}"', ".", 0)
    except Exception as e:
        raise RuntimeError(
            f"ShellExecute printto failed ({e}); no default PDF app with a "
            f"'printto' verb? Install SumatraPDF on this host") from e
    log.info("print_via_shell: ShellExecute printto '%s' <- %s (polling up to %ds)",
             printer_name, filepath, timeout)
    # ShellExecute is async; poll the queue until the job appears/drains
    deadline = time.time() + timeout
    job_seen = False
    while time.time() < deadline:
        st = printer_status(printer_name)
        if st["jobs_queued"] > 0:
            if not job_seen:
                log.info("print_via_shell: job appeared in spool queue (%d queued)",
                         st["jobs_queued"])
            job_seen = True
        elif job_seen and st["jobs_queued"] == 0:
            log.info("print_via_shell: spool drained — job complete on '%s'",
                     printer_name)
            return  # spooled and drained
        time.sleep(1)
    raise RuntimeError(f"print job did not complete within {timeout}s")


_SUMATRA_PATHS = (
    os.path.expandvars(r"%LOCALAPPDATA%\SumatraPDF\SumatraPDF.exe"),
    r"C:\Program Files\SumatraPDF\SumatraPDF.exe",
    r"C:\Program Files (x86)\SumatraPDF\SumatraPDF.exe",
)


def _find_sumatra() -> str | None:
    """Locate SumatraPDF: 'App Paths' registry first, then usual install dirs."""
    import winreg
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe") as k:
            exe, _ = winreg.QueryValueEx(k, "")
            if exe and os.path.isfile(exe):
                return exe
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\SumatraPDF.exe") as k:
            exe, _ = winreg.QueryValueEx(k, "")
            if exe and os.path.isfile(exe):
                return exe
    except OSError:
        pass
    for p in _SUMATRA_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _print_with_sumatra(sumatra: str, filepath: str, printer_name: str,
                        timeout: int) -> None:
    """SumatraPDF CLI: -print-to <printer> -silent <pdf> (silent, then exits)."""
    import subprocess
    cmd = [sumatra, "-print-to", printer_name, "-silent",
           "-exit-on-print", os.path.abspath(filepath)]
    log.info("print_via_shell: %s (timeout=%ds)", " ".join(cmd), timeout)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"SumatraPDF print timed out after {timeout}s") from e
    if r.returncode != 0:
        err = (r.stderr.decode("utf-8", "replace")
               + r.stdout.decode("utf-8", "replace")).strip()
        raise RuntimeError(f"SumatraPDF exit {r.returncode}: {err[:300]}")
    log.info("print_via_shell: SumatraPDF printed '%s' on '%s'",
             os.path.basename(filepath), printer_name)


def print_word(filepath: str, printer_name: str, timeout: int = 180) -> None:
    """Print a Word document (.docx/.doc) via Word COM automation.

    Uses the registered ActivePrinter; requires Microsoft Word on the host.
    Runs in a watchdog thread so a hung Word cannot block the HTTP handler.
    """
    import pythoncom
    import subprocess
    import threading
    import win32com.client

    result: dict = {}

    def worker():
        pythoncom.CoInitialize()
        word = None
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            word.DisplayAlerts = 0
            try:
                word.ActivePrinter = printer_name
            except Exception:
                log.warning("print_word: could not set ActivePrinter '%s'; "
                            "using Word default", printer_name)
            doc = word.Documents.Open(os.path.abspath(filepath), ReadOnly=True)
            try:
                doc.PrintOut()
            finally:
                doc.Close(SaveChanges=0)
            log.info("print_word: printed '%s' on '%s'",
                     os.path.basename(filepath), printer_name)
        except Exception as e:
            result["error"] = f"Word print failed: {e!r}"
        finally:
            if word is not None:
                try:
                    word.Quit()
                except Exception:
                    pass
            pythoncom.CoUninitialize()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        subprocess.run(["taskkill", "/IM", "WINWORD.EXE", "/F"],
                       capture_output=True)
        raise RuntimeError(f"Word print timed out after {timeout}s")
    if "error" in result:
        raise RuntimeError(result["error"])


def print_raw(data: bytes, printer_name: str, job_name: str = "PrintLink Job") -> None:
    """Send raw bytes straight to the spooler (for future ESC/POS / ZPL)."""
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, (job_name, None, "RAW"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


def print_text(data: bytes, printer_name: str, job_name: str = "PrintLink Job") -> None:
    """Print plain-text bytes via the spooler's TEXT print processor.

    Spooled data from a "Generic / Text Only" sender queue is raw text; the
    spooler's TEXT datatype renders it on any driver (LF -> CRLF, form feed).
    """
    h = win32print.OpenPrinter(printer_name)
    try:
        win32print.StartDocPrinter(h, 1, (job_name, None, "TEXT"))
        win32print.StartPagePrinter(h)
        win32print.WritePrinter(h, data)
        win32print.EndPagePrinter(h)
        win32print.EndDocPrinter(h)
    finally:
        win32print.ClosePrinter(h)


def is_binary_document(data: bytes) -> bool:
    """True when the payload is a real XPS/PDF (zip/PDF magic), False for text."""
    return data.startswith(b"PK\x03\x04") or data.startswith(b"%PDF-")


_EMF_PRINT_PS = r"""param([string]$Path, [string]$Printer)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Drawing.Printing
$img = [System.Drawing.Image]::FromFile($Path)
$doc = New-Object System.Drawing.Printing.PrintDocument
$doc.PrinterSettings.PrinterName = $Printer
$doc.DocumentName = "PrintLink EMF"
$doc.add_PrintPage({
    param($sender, $e)
    $e.Graphics.DrawImage($img, 0, 0)
    $e.HasMorePages = $false
})
try { $doc.Print() }
finally {
    $doc.Dispose()
    $img.Dispose()
}
"""


def _run_gdi_print(path: str, printer_name: str, timeout: int = 180) -> None:
    """Render an image file (EMF/PNG/JPG/GIF/BMP/...) via GDI+ onto the printer.

    Uses the built-in .NET System.Drawing pipeline (no external app needed).
    """
    import subprocess
    import tempfile
    script = os.path.join(tempfile.gettempdir(), "pl_print_emf.ps1")
    with open(script, "w", encoding="utf-8") as f:
        f.write(_EMF_PRINT_PS)
    r = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", script, "-Path", path, "-Printer", printer_name],
        capture_output=True, timeout=timeout)
    if r.returncode != 0:
        err = r.stderr.decode("utf-8", "replace") + r.stdout.decode("utf-8", "replace")
        raise RuntimeError(f"GDI+ print failed: {err[:400]}")
    log.info("print_image: rendered '%s' on '%s'", os.path.basename(path), printer_name)


def print_emf(data: bytes, printer_name: str, job_name: str = "PrintLink Job") -> None:
    """Print an EMF metafile by rendering it with GDI+ onto the printer.

    EMF carries page graphics, not text; the spooler's TEXT/RAW datatypes
    cannot render it. DrawImage onto PrintDocument is the standard GDI path.
    """
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".emf", dir=tempfile.gettempdir())
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        _run_gdi_print(path, printer_name)
        log.info("print_emf: rendered EMF on '%s'", printer_name)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def print_image(filepath: str, printer_name: str, timeout: int = 180) -> None:
    """Print a PNG/JPG/GIF/BMP/TIFF/WEBP file via GDI+ (no app required)."""
    _run_gdi_print(os.path.abspath(filepath), printer_name, timeout=timeout)


def extract_emf(data: bytes) -> bytes | None:
    """Locate the EMF stream inside a spooler-rendered payload.

    The despool pre-fills ReadPort buffers with zeroed staging, so the real
    EMF can be preceded by a zero pad (and a stray prefix). The EMF header's
    dSignature (' EMF') sits at EMF offset 40, with iType == 1 at offset 0.
    """
    sig = b" EMF"
    idx = data.find(sig)
    while idx != -1:
        emf_start = idx - 40
        if emf_start >= 0 and data[emf_start:emf_start + 4] == b"\x01\x00\x00\x00":
            nsize = int.from_bytes(data[emf_start + 4:emf_start + 8], "little")
            emf = data[emf_start:emf_start + nsize]
            if len(emf) == nsize and nsize > 80:
                return emf
        idx = data.find(sig, idx + 1)
    return None


def sniff_format(data: bytes) -> str:
    """Classify the payload before assigning an extension / print path."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if data[:4] == b"PK\x03\x04":
        # OOXML docs and XPS are both zips; distinguish by inner files
        import io
        import zipfile
        try:
            names = set(zipfile.ZipFile(io.BytesIO(data)).namelist())
        except Exception:
            return "xps"
        if "word/document.xml" in names:
            return "docx"
        if "xl/workbook.xml" in names:
            return "xlsx"
        if "ppt/presentation.xml" in names:
            return "pptx"
        return "xps"
    if data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "doc"  # OLE compound document (legacy .doc)
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:2] == b"BM":
        return "bmp"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if extract_emf(data) is not None:
        return "emf"
    n = min(len(data), 1024)
    if n:
        printable = sum(1 for b in data[:n] if 9 <= b <= 13 or 32 <= b <= 126)
        if printable / n > 0.85:
            return "text"
    return "binary"
