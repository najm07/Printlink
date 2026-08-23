"""Sender-side print preview + preferences dialog (shown before delivery).

Renders a first-page thumbnail for PDFs (PyMuPDF) and images (Pillow), a
placeholder for office/EMF payloads, and lets the user pick copies / page
range / paper / color / duplex / orientation / fit. The chosen options travel
with the job to the receiver, which applies them per format (see
printer_local).

Threading contract
------------------
Tk widgets may ONLY be created on the thread that owns the Tk root.

- Tray mode:  the tray owns the one root + mainloop on the main thread.
  Worker threads (sender retry loop) must marshal the call with
  root.after(...) — the same pattern tray.py uses for the share accept/refuse
  dialog — and pass parent=<tray root>. ask_print_options then blocks the
  CALLING thread until the dialog closes.
- CLI mode:   parent=None creates a standalone Tk root. If no display is
  available, falls back to DEFAULT_OPTIONS so headless sending still works.

Licensing: thumbnails use PyMuPDF (AGPL-3.0 / commercial dual license).
Anyone redistributing PrintLink commercially must swap in pypdfium2 or drop
the preview. Keep the README notice in sync.

Look & feel
-----------
Styling lives in the palette + _setup_style() below. Deliberately pinned to
the 'clam' ttk theme rather than Windows' native 'vista' theme: vista draws
button/combobox chrome via the OS theme engine and mostly ignores ttk style
colors, so it can't produce the accent Print button or card backgrounds
below. clam is a pure-Tk theme — renders identically on every platform, so
it's safe to reason about (and screenshot-test off of Windows).
"""
import os
import re
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox

from PIL import Image, ImageDraw, ImageFont, ImageTk

from logutil import get_logger
from printer_local import DEFAULT_OPTIONS, sniff_format

log = get_logger("preview")

# Formats whose page count is cheap to know (enables range clamping).
# docx/doc are multi-page but the count is unknown without rendering, so
# their entry stays enabled but unclamped.
COUNTABLE = {"pdf", "tiff"}
MULTI_PAGE_UNKNOWN = {"docx", "doc"}

PAPERS = ["auto", "A4", "A5", "A3", "Letter", "Legal"]
COLORS = ["color", "mono"]
DUPLEX = ["off", "long", "short"]
ORIENTATIONS = ["auto", "portrait", "landscape"]
FITS = ["fit", "shrink", "actual"]

THUMB_W, THUMB_H = 240, 300

# --------------------------------------------------------------------- #
# Palette. One accent, one neutral scale. Change these five colors and
# the whole dialog re-themes — nothing else below hardcodes a color.
# --------------------------------------------------------------------- #
BG_APP = "#F4F6F9"          # dialog background
BG_CARD = "#FFFFFF"         # elevated surfaces (thumbnail, would-be cards)
BORDER = "#E2E6ED"          # hairlines / field borders
SHADOW = "#D4D9E1"          # faux drop-shadow behind the thumbnail card
TEXT_PRIMARY = "#1B2430"    # headings
TEXT_LABEL = "#4A5568"      # field labels
TEXT_HINT = "#96A0B2"       # helper text
ACCENT = "#1D5FB8"          # brand / primary action
ACCENT_HOVER = "#164A92"
ACCENT_PRESSED = "#0F3872"


def _best_family(root) -> str:
    """Pick the nicest font actually installed. Segoe UI on Windows (where
    this ships); a sane fallback everywhere else (where it's only ever
    dev/test)."""
    families = set(tkfont.families(root))
    for candidate in ("Segoe UI", "Helvetica Neue", "Liberation Sans",
                      "DejaVu Sans", "Arial"):
        if candidate in families:
            return candidate
    return "TkDefaultFont"


def _setup_style(root) -> dict:
    """Configure ttk for this interpreter and return the font tuples to
    reuse on plain tk widgets. Re-running this on an already-styled root
    (tray mode reuses one root across many dialogs) is harmless — it just
    re-applies the same values — so no readiness flag: CLI mode creates a
    fresh Tk() per call, which is a fresh interpreter, and a global flag
    would wrongly skip styling it on the second call."""
    family = _best_family(root)
    fonts = {
        "title": (family, 13, "bold"),
        "section": (family, 10, "bold"),
        "label": (family, 10),
        "hint": (family, 9),
        "button": (family, 10, "bold"),
    }

    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass  # exotic Tk build without clam; degrade to its default theme

    style.configure("TFrame", background=BG_APP)
    style.configure("Card.TFrame", background=BG_CARD)

    style.configure("TLabel", background=BG_APP, foreground=TEXT_LABEL,
                    font=fonts["label"])
    style.configure("Title.TLabel", background=BG_APP, foreground=TEXT_PRIMARY,
                    font=fonts["title"])
    style.configure("Hint.TLabel", background=BG_APP, foreground=TEXT_HINT,
                    font=fonts["hint"])
    style.configure("DocTitle.TLabel", background=BG_APP,
                    foreground=TEXT_PRIMARY, font=(family, 12, "bold"))
    style.configure("Summary.TLabel", background=BG_CARD,
                    foreground=ACCENT, font=(family, 9, "bold"))

    style.configure("TLabelframe", background=BG_APP, bordercolor=BORDER,
                    relief="solid", borderwidth=1)
    style.configure("TLabelframe.Label", background=BG_APP, foreground=ACCENT,
                    font=fonts["section"])

    style.configure("TEntry", fieldbackground=BG_CARD, foreground=TEXT_PRIMARY,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=5)
    style.configure("TSpinbox", fieldbackground=BG_CARD, foreground=TEXT_PRIMARY,
                    bordercolor=BORDER, arrowsize=13, padding=4)
    style.configure("TCombobox", fieldbackground=BG_CARD, foreground=TEXT_PRIMARY,
                    bordercolor=BORDER, arrowsize=13, padding=4)
    style.map("TCombobox",
              fieldbackground=[("readonly", BG_CARD)],
              foreground=[("readonly", TEXT_PRIMARY)])

    style.configure("TButton", font=fonts["button"], padding=(14, 7),
                    background=BG_CARD, foreground=TEXT_PRIMARY,
                    bordercolor=BORDER, relief="flat")
    style.map("TButton", background=[("active", "#ECEFF4")])

    style.configure("Accent.TButton", font=fonts["button"], padding=(18, 7),
                    background=ACCENT, foreground="white",
                    bordercolor=ACCENT, relief="flat")
    style.map("Accent.TButton",
              background=[("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)],
              bordercolor=[("pressed", ACCENT_PRESSED), ("active", ACCENT_HOVER)])

    style.configure("TSeparator", background=BORDER)

    return fonts


_SPEC_PART = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d+)\s*)?$")


def normalize_page_spec(spec: str, max_page: int | None = None) -> str:
    """Validate and normalize a '1-3, 5' style page spec -> '1-3,5'.

    printer_local MUST re-parse the spec receiver-side with THIS function, so
    the dialog can never emit something the receiver would reject.
    Raises ValueError on anything invalid.
    """
    if not spec or not spec.strip():
        raise ValueError("empty page spec")
    out = []
    for chunk in spec.split(","):
        m = _SPEC_PART.match(chunk)
        if not m:
            raise ValueError(f"bad page range: {chunk.strip()!r}")
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a < 1:
            raise ValueError("pages start at 1")
        if b < a:
            raise ValueError(f"range {a}-{b} is backwards")
        if max_page is not None and b > max_page:
            raise ValueError(f"document has only {max_page} page(s)")
        out.append(f"{a}-{b}" if b != a else str(a))
    return ",".join(out)


def _page_count(filepath: str, fmt: str) -> int | None:
    """Page count when cheaply knowable, else None (unclamped entry)."""
    try:
        if fmt == "pdf":
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf
            with pymupdf.open(filepath) as doc:
                return len(doc)
        if fmt == "tiff":
            with Image.open(filepath) as im:
                return getattr(im, "n_frames", 1)
    except Exception as e:
        log.warning("page count failed for %s: %r", filepath, e)
    return None


def _pil_font(size: int, bold: bool = False):
    """Best-effort TrueType font for the placeholder thumbnail. Falls all
    the way back to PIL's built-in bitmap font, so this never raises."""
    names = ["segoeuib.ttf", "seguisb.ttf"] if bold else ["segoeui.ttf"]
    names += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
        "Arial Bold.ttf" if bold else "arial.ttf",
    ]
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _placeholder(text: str) -> Image.Image:
    img = Image.new("RGB", (THUMB_W, THUMB_H), BG_CARD)
    d = ImageDraw.Draw(img)

    # a plain document silhouette with a folded corner, rather than bare text
    m, fold = 30, 20
    x0, y0, x1, y1 = m, 46, THUMB_W - m, THUMB_H - 66
    d.polygon([(x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1)],
              outline=BORDER, width=2, fill="#FBFCFE")
    d.line([(x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold)],
           fill=BORDER, width=2)

    label_font = _pil_font(16, bold=True)
    sub_font = _pil_font(10)
    label = text.upper()
    lw = d.textlength(label, font=label_font)
    d.text(((THUMB_W - lw) / 2, (y0 + y1) / 2 - 10), label, fill=ACCENT,
           font=label_font)
    sub = "preview not available"
    sw = d.textlength(sub, font=sub_font)
    d.text(((THUMB_W - sw) / 2, y1 + 16), sub, fill=TEXT_HINT, font=sub_font)
    return img


def _thumbnail(filepath: str, fmt: str) -> Image.Image:
    """First-page thumbnail rendered straight from disk (never reads the
    whole file — large scans stay cheap)."""
    try:
        if fmt == "pdf":
            try:
                import pymupdf
            except ImportError:
                import fitz as pymupdf
            with pymupdf.open(filepath) as doc:
                pm = doc[0].get_pixmap(matrix=pymupdf.Matrix(1.2, 1.2),
                                       alpha=False)
                img = Image.frombytes("RGB", (pm.width, pm.height), pm.samples)
        elif fmt in ("png", "jpg", "gif", "bmp", "webp", "tiff"):
            with Image.open(filepath) as im:
                img = im.convert("RGB")
        else:
            img = _placeholder(fmt.upper())
    except Exception as e:
        log.warning("thumbnail failed for %s (%s): %r", filepath, fmt, e)
        img = _placeholder(fmt.upper())
    img.thumbnail((THUMB_W, THUMB_H))
    return img


class PreviewDialog:
    """Modal preview/options sheet. .result holds (options_dict, printer_tuple)
    on Print, None on cancel/close. The window is destroyed either way."""

    def __init__(self, master, filepath: str, fmt: str,
                 page_count: int | None,
                 printers: list[tuple[str, str]] | None,
                 selected: tuple[str, str] | None,
                 standalone: bool = False):
        self.result: dict | None = None
        self._page_count = page_count
        self._printers = printers or []
        self._selected = selected

        if standalone:
            self.top = master            # master IS the Tk root
        else:
            self.top = tk.Toplevel(master)
            self.top.transient(master.winfo_toplevel())
            self.top.grab_set()          # modal: no clicking back into tray

        _setup_style(self.top)
        self.top.configure(background=BG_APP)
        self.top.title(f"Print with PrintLink — {os.path.basename(filepath)}")
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)   # X == cancel
        self.top.bind("<Escape>", lambda e: self._cancel())

        tk.Frame(self.top, background=ACCENT, height=4).grid(
            row=0, column=0, sticky="ew")

        body = ttk.Frame(self.top, padding=(24, 18, 24, 20))
        body.grid(row=1, column=0, sticky="nsew")

        header = ttk.Frame(body)
        header.grid(row=0, column=0, columnspan=2, sticky="ew",
                    pady=(0, 16))

        # document identity line: name + what/how-big/how-many
        ttk.Label(header, text=os.path.basename(filepath),
                  style="DocTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text=_meta_line(filepath, fmt, page_count),
                  style="Hint.TLabel").grid(row=1, column=0, sticky="w",
                                            pady=(2, 0))

        self.printer_var = tk.StringVar(self.top)
        self._printer_map: dict[str, tuple[str, str]] = {}

        # --- thumbnail, presented as a small elevated card ---------------
        thumb_wrap = tk.Frame(body, background=SHADOW)
        thumb_wrap.grid(row=1, column=0, padx=(0, 20), sticky="n")
        card = tk.Frame(thumb_wrap, background=BG_CARD,
                        highlightbackground=BORDER, highlightthickness=1)
        card.pack(padx=(0, 3), pady=(0, 3))

        img = _thumbnail(filepath, fmt)
        self._photo = ImageTk.PhotoImage(img)   # keep a reference or it GCs
        tk.Label(card, image=self._photo, background=BG_CARD,
                borderwidth=0).pack(padx=1, pady=1)

        # --- right column: destination, job, settings, summary ------------
        right_col = ttk.Frame(body)
        right_col.grid(row=1, column=1, sticky="n")

        dest_frame = ttk.Labelframe(right_col, text="Destination",
                                    padding=(14, 8, 14, 10))
        dest_frame.grid(row=0, column=0, sticky="ew")
        if self._printers:
            default = self._printers[0]
            if self._selected:
                default = next((p for p in self._printers
                                if p[0] == self._selected[0]
                                and p[1] == self._selected[1]), default)
            entries = []
            for p in self._printers:
                label = self._printer_label(p)
                entries.append(label)
                self._printer_map[label] = (p[0], p[1])
            self.printer_var.set(self._printer_label(default))
            cb = ttk.Combobox(dest_frame, textvariable=self.printer_var,
                              values=entries, state="readonly", width=32)
            cb.grid(row=0, column=0, sticky="ew")
        else:
            ttk.Label(dest_frame,
                      text=(self._selected or ("", "?"))[1],
                      style="TLabel").grid(row=0, column=0, sticky="w")

        job_frame = ttk.Labelframe(right_col, text="Copies & Pages",
                                   padding=(14, 8, 14, 12))
        job_frame.grid(row=1, column=0, sticky="ew", pady=(12, 0))

        self.copies = tk.IntVar(self.top, value=1)
        ttk.Label(job_frame, text="Copies:").grid(
            row=0, column=0, sticky="e", padx=(0, 10), pady=(0, 10))
        ttk.Spinbox(job_frame, from_=1, to=99, textvariable=self.copies,
                    width=6).grid(row=0, column=1, sticky="w", pady=(0, 10))

        pages_label = ("Pages:" if page_count is None
                       else f"Pages (1-{page_count}):")
        self.pages = tk.StringVar(self.top, value="")
        ttk.Label(job_frame, text=pages_label).grid(
            row=1, column=0, sticky="e", padx=(0, 10))
        self.pages_entry = ttk.Entry(job_frame, textvariable=self.pages,
                                     width=16)
        self.pages_entry.grid(row=1, column=1, sticky="w")
        ttk.Label(job_frame, text="e.g. 1-3,5 (blank = all)",
                  style="Hint.TLabel").grid(row=1, column=2, sticky="w",
                                            padx=(10, 0))
        if fmt not in COUNTABLE and fmt not in MULTI_PAGE_UNKNOWN:
            self.pages_entry.state(["disabled"])

        settings_frame = ttk.Labelframe(right_col, text="Settings",
                                        padding=(14, 8, 14, 12))
        settings_frame.grid(row=2, column=0, sticky="ew", pady=(12, 0))

        self.paper = tk.StringVar(self.top, value="auto")
        self.color = tk.StringVar(self.top, value="color")
        self.duplex = tk.StringVar(self.top, value="off")
        self.orientation = tk.StringVar(self.top, value="auto")
        self.fit = tk.StringVar(self.top, value="fit")

        settings_rows = [
            ("Paper:", self.paper, PAPERS),
            ("Color:", self.color, COLORS),
            ("Duplex:", self.duplex, DUPLEX),
            ("Orient:", self.orientation, ORIENTATIONS),
            ("Fit:", self.fit, FITS),
        ]
        for i, (label_text, var, values) in enumerate(settings_rows):
            r, c = divmod(i, 2)
            last_row = r == (len(settings_rows) - 1) // 2
            ttk.Label(settings_frame, text=label_text).grid(
                row=r, column=c * 2, sticky="e",
                padx=((0, 6), (18, 6))[c], pady=(0, 0 if last_row else 8))
            ttk.Combobox(settings_frame, textvariable=var, values=values,
                         state="readonly", width=10).grid(
                row=r, column=c * 2 + 1, sticky="w", pady=(0, 0 if last_row
                                                           else 8))

        # live one-line summary of exactly what is about to be sent
        self.summary_var = tk.StringVar(self.top)
        summary_card = tk.Frame(right_col, background=BG_CARD,
                                highlightbackground=BORDER,
                                highlightthickness=1)
        summary_card.grid(row=3, column=0, sticky="ew", pady=(14, 0))
        ttk.Label(summary_card, textvariable=self.summary_var,
                  style="Summary.TLabel", padding=(10, 6)).grid(
            row=0, column=0, sticky="ew")
        for var in (self.paper, self.color, self.duplex,
                    self.orientation, self.fit, self.pages):
            var.trace_add("write", lambda *_: self._update_summary())
        self.copies.trace_add("write", lambda *_: self._update_summary())
        self._update_summary()

        ttk.Separator(body, orient="horizontal").grid(
            row=2, column=0, columnspan=2, sticky="ew", pady=(20, 14))

        btns = ttk.Frame(body)
        btns.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(btns, text="Cancel", command=self._cancel).grid(
            row=0, column=0, padx=(0, 10))
        self.print_btn = ttk.Button(btns, text="Print", style="Accent.TButton",
                                    command=self._ok)
        self.print_btn.grid(row=0, column=1)

        self._center()
        self.print_btn.focus_set()
        self.top.bind("<Return>", lambda e: self._ok())   # Enter = Print

    def _update_summary(self):
        try:
            copies = max(1, int(self.copies.get()))
        except (tk.TclError, ValueError):
            copies = 1
        parts = [f"{copies} cop{'y' if copies == 1 else 'ies'}"]
        if self.paper.get() not in ("auto", ""):
            parts.append(self.paper.get())
        parts.append("mono" if self.color.get() == "mono" else "color")
        if self.duplex.get() not in ("off", ""):
            parts.append(f"duplex {self.duplex.get()}")
        if self.orientation.get() not in ("auto", ""):
            parts.append(self.orientation.get())
        if self.fit.get():
            parts.append(f"fit={self.fit.get()}")
        pages = self.pages.get().strip()
        if pages:
            parts.append(f"pages {pages}")
        self.summary_var.set("  ·  ".join(parts))

    def _center(self):
        self.top.update_idletasks()
        w = self.top.winfo_reqwidth()
        h = self.top.winfo_reqheight()
        parent = self.top.master
        if parent is not None and parent.winfo_viewable():
            px, py = parent.winfo_rootx(), parent.winfo_rooty()
            pw, ph = parent.winfo_width(), parent.winfo_height()
            x, y = px + (pw - w) // 2, py + (ph - h) // 2
        else:  # standalone: center on screen
            x = (self.top.winfo_screenwidth() - w) // 2
            y = (self.top.winfo_screenheight() - h) // 3
        self.top.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    @staticmethod
    def _printer_label(p: tuple) -> str:
        name = p[2] if len(p) > 2 else None
        if name:
            return f"{p[1]} @ {name}"
        return f"{p[1]} @ {p[0]}"

    def _chosen_printer(self) -> tuple[str, str]:
        label = self.printer_var.get()
        if label in self._printer_map:
            return self._printer_map[label]
        return self._selected or self._printers[0]

    def _ok(self):
        try:
            copies = self.copies.get()
            if not 1 <= copies <= 99:
                raise ValueError
        except (tk.TclError, ValueError):
            messagebox.showwarning("PrintLink", "Copies must be 1-99.",
                                   parent=self.top)
            return
        pages = self.pages.get().strip()
        if pages:
            try:
                # clamp to the real page count when we know it
                pages = normalize_page_spec(pages, self._page_count)
            except ValueError as e:
                messagebox.showwarning("PrintLink", f"Bad page range: {e}",
                                       parent=self.top)
                return
        self.result = (
            {
                "copies": copies,
                "pages": pages,
                "paper": self.paper.get(),
                "color": self.color.get(),
                "duplex": self.duplex.get(),
                "orientation": self.orientation.get(),
                "fit": self.fit.get(),
            },
            self._chosen_printer(),
        )
        self.top.destroy()

    def _cancel(self):
        self.result = None
        self.top.destroy()


def _human_size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.1f} MB"
    if n >= 1 << 10:
        return f"{n / (1 << 10):.0f} KB"
    return f"{n} B"


def _meta_line(filepath: str, fmt: str, page_count: int | None) -> str:
    """'PDF · 2.4 MB · 8 pages' — one glanceable line under the filename."""
    parts = [fmt.upper() if fmt != "binary" else "file"]
    try:
        parts.append(_human_size(os.path.getsize(filepath)))
    except OSError:
        pass
    if page_count is not None:
        parts.append(f"{page_count} page{'s' if page_count != 1 else ''}")
    return "  ·  ".join(parts)


def _sniff_head(filepath: str) -> str:
    """Format sniff without loading the whole file — except zip-family docs,
    whose central directory (and thus the docx/xlsx/pptx markers) lives at
    the END of the file, so those need the full read."""
    with open(filepath, "rb") as f:
        head = f.read(64)
    if head[:4] == b"PK\x03\x04":
        with open(filepath, "rb") as f:
            return sniff_format(f.read())
    return sniff_format(head)


def ask_print_options(filepath: str,
                      printers: list[tuple[str, str]] | None,
                      selected: tuple[str, str] | None = None,
                      parent: "tk.Misc | None" = None
                      ) -> tuple[dict, tuple[str, str]] | None:
    """Show the preview dialog; return (options_dict, (host_id, alias)) or
    None if cancelled.

    printers: active remote printers shown in the selector (None/[] hides
    the selector and keeps `selected`). parent given -> modal Toplevel on
    the existing root — MUST be called on the thread that owns `parent`.
    parent=None -> standalone root (CLI). No display -> defaults + selected.
    """
    try:
        fmt = _sniff_head(filepath)
    except OSError as e:
        log.warning("cannot read %s for preview: %r", filepath, e)
        fmt = "binary"

    page_count = _page_count(filepath, fmt)

    def fallback() -> tuple[dict, tuple[str, str]]:
        target = selected or (printers[0] if printers else None)
        if target is None:
            raise RuntimeError("no printer available")
        return dict(DEFAULT_OPTIONS), target

    if parent is not None:
        dlg = PreviewDialog(parent, filepath, fmt, page_count,
                            printers, selected)
        parent.wait_window(dlg.top)
        return dlg.result

    root = None
    try:
        root = tk.Tk()
        root.withdraw()                      # build hidden, show once centred
        dlg = PreviewDialog(root, filepath, fmt, page_count,
                            printers, selected, standalone=True)
        root.deiconify()
        root.wait_window(root)
        return dlg.result
    except tk.TclError as e:
        log.warning("preview dialog unavailable (%r); sending with defaults",
                    e, exc_info=True)
        return fallback()
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass                         # already destroyed by _ok/_cancel