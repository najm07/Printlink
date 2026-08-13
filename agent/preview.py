"""Sender-side print preview + preferences dialog (shown before delivery).

Renders a thumbnail for PDFs and images (PyMuPDF/Pillow), a placeholder for
office docs, and lets the user pick copies / page range / paper / color /
duplex / orientation / fit. The chosen options travel with the job to the
receiver, which applies them per format (see printer_local).
"""
import io
import os
import tkinter as tk
from tkinter import ttk, messagebox

from PIL import Image, ImageDraw, ImageTk

from logutil import get_logger
from printer_local import DEFAULT_OPTIONS, sniff_format

log = get_logger("preview")

MULTI_PAGE = {"pdf", "docx", "doc"}
PAPERS = ["auto", "A4", "A5", "A3", "Letter", "Legal"]
COLORS = ["color", "mono"]
DUPLEX = ["off", "long", "short"]
ORIENTATIONS = ["auto", "portrait", "landscape"]
FITS = ["fit", "shrink", "actual"]


def _placeholder(text: str) -> Image.Image:
    img = Image.new("RGB", (240, 300), (245, 245, 245))
    d = ImageDraw.Draw(img)
    d.rectangle([1, 1, 238, 298], outline=(180, 180, 180))
    d.text((90, 120), text, fill=(120, 120, 120))
    d.text((55, 145), "preview not available", fill=(150, 150, 150))
    return img


def _thumbnail(data: bytes, fmt: str, max_w: int = 240, max_h: int = 300) -> Image.Image:
    try:
        if fmt == "pdf":
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                pm = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2))
                img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        elif fmt in ("png", "jpg", "gif", "bmp", "webp", "tiff"):
            img = Image.open(io.BytesIO(data)).convert("RGB")
        else:
            img = _placeholder(fmt.upper())
    except Exception as e:
        log.warning("thumbnail failed for %s: %r", fmt, e)
        img = _placeholder(fmt.upper())
    img.thumbnail((max_w, max_h))
    return img


class _Dialog:
    def __init__(self, root: tk.Tk, filepath: str, data: bytes, fmt: str,
                 printer_label: str):
        self.result: dict | None = None
        root.title(f"Print with PrintLink — {os.path.basename(filepath)}")
        root.resizable(False, False)
        body = ttk.Frame(root, padding=12)
        body.grid()

        ttk.Label(body, text=f"Printer: {printer_label}",
                  font=("Segoe UI", 10, "bold")).grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        img = _thumbnail(data, fmt)
        self._photo = ImageTk.PhotoImage(img)
        ttk.Label(body, image=self._photo, relief="solid",
                  borderwidth=1).grid(row=1, column=0, rowspan=6,
                                      padx=(0, 14), sticky="n")

        self.copies = tk.IntVar(value=1)
        ttk.Label(body, text="Copies:").grid(row=1, column=1, sticky="e")
        ttk.Spinbox(body, from_=1, to=99, textvariable=self.copies,
                    width=6).grid(row=1, column=2, sticky="w")

        self.pages = tk.StringVar(value="all")
        ttk.Label(body, text="Pages (1-3,5):").grid(row=2, column=1, sticky="e")
        self.pages_entry = ttk.Entry(body, textvariable=self.pages, width=16)
        self.pages_entry.grid(row=2, column=2, sticky="w")
        if fmt not in MULTI_PAGE:
            self.pages.set("")
            self.pages_entry.state(["disabled"])

        self.paper = tk.StringVar(value="auto")
        ttk.Label(body, text="Paper:").grid(row=3, column=1, sticky="e")
        ttk.Combobox(body, textvariable=self.paper, values=PAPERS,
                     state="readonly", width=10).grid(row=3, column=2, sticky="w")

        self.color = tk.StringVar(value="color")
        ttk.Label(body, text="Color:").grid(row=4, column=1, sticky="e")
        ttk.Combobox(body, textvariable=self.color, values=COLORS,
                     state="readonly", width=10).grid(row=4, column=2, sticky="w")

        self.duplex = tk.StringVar(value="off")
        ttk.Label(body, text="Duplex:").grid(row=5, column=1, sticky="e")
        ttk.Combobox(body, textvariable=self.duplex, values=DUPLEX,
                     state="readonly", width=10).grid(row=5, column=2, sticky="w")

        self.orientation = tk.StringVar(value="auto")
        self.fit = tk.StringVar(value="fit")
        opts_row = ttk.Frame(body)
        opts_row.grid(row=6, column=1, columnspan=2, sticky="w")
        ttk.Label(opts_row, text="Orientation:").grid(row=0, column=0, sticky="e")
        ttk.Combobox(opts_row, textvariable=self.orientation,
                     values=ORIENTATIONS, state="readonly",
                     width=9).grid(row=0, column=1, padx=(6, 12))
        ttk.Label(opts_row, text="Fit:").grid(row=0, column=2, sticky="e")
        ttk.Combobox(opts_row, textvariable=self.fit, values=FITS,
                     state="readonly", width=9).grid(row=0, column=3)

        btns = ttk.Frame(body)
        btns.grid(row=7, column=0, columnspan=3, sticky="e", pady=(14, 0))
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(
            row=0, column=0, padx=(0, 8))
        ttk.Button(btns, text="Print", command=self._ok).grid(row=0, column=1)

    def _ok(self):
        try:
            copies = self.copies.get()
            if copies < 1 or copies > 99:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showwarning("PrintLink", "Copies must be 1-99.")
            return
        pages = self.pages.get().strip()
        if pages and pages.lower() not in ("all", "*") and \
                not all(c in "0123456789,-* " for c in pages):
            messagebox.showwarning(
                "PrintLink", "Pages must be like 'all', '1-3' or '1-3,5'.")
            return
        self.result = {
            "copies": copies,
            "pages": "" if pages.lower() in ("all", "*") else pages,
            "paper": self.paper.get(),
            "color": self.color.get(),
            "duplex": self.duplex.get(),
            "orientation": self.orientation.get(),
            "fit": self.fit.get(),
        }
        self._root.destroy()

    def _cancel(self):
        self._root.destroy()


def ask_print_options(filepath: str, printer_label: str) -> dict | None:
    """Show the preview dialog; returns an options dict, or None if cancelled.

    Falls back to default options (dialog skipped) when no GUI is available.
    """
    try:
        with open(filepath, "rb") as f:
            data = f.read()
        fmt = sniff_format(data)
    except OSError as e:
        log.warning("cannot read %s for preview: %r", filepath, e)
        fmt = "binary"
        data = b""
    try:
        root = tk.Tk()
        root.withdraw()  # keep the hidden parent out of the taskbar
        dlg = _Dialog(root, filepath, data, fmt, printer_label)
        dlg._root = root
        root.deiconify()
        root.wait_window(root)
        return dlg.result if dlg.result is not None else None
    except Exception as e:
        log.warning("preview dialog unavailable (%r); sending with defaults", e)
        return dict(DEFAULT_OPTIONS)