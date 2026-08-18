"""Format sniffing: PDF vs OOXML (docx/xlsx/pptx) vs XPS vs text vs binary."""
import io
import sys
import zipfile
import pytest

sys.path.insert(0, "agent")  # real printer_local, not the test_api stub
sys.modules.pop("printer_local", None)

from printer_local import sniff_format, parse_page_spec


def test_print_pdf_page_range_subset_path(tmp_path, monkeypatch):
    """Regression: print_pdf's PyMuPDF subset path must reach Sumatra lookup
    (it once died on 'name 'tempfile' is not defined' before printing)."""
    import pymupdf
    from printer_local import print_pdf
    pdf = tmp_path / "p.pdf"
    with pymupdf.open() as doc:
        doc.new_page()
        doc.new_page()
        doc.save(pdf)

    def boom(*a, **k):
        raise AssertionError("reached _find_sumatra (subset path OK)")

    monkeypatch.setattr("printer_local._find_sumatra", boom)
    with pytest.raises(AssertionError, match="reached _find_sumatra"):
        print_pdf(str(pdf), "Fake Printer", {"pages": "1"})


def test_pages_all():
    assert parse_page_spec("", 10) is None
    assert parse_page_spec("all", 10) is None
    assert parse_page_spec("*", 10) is None


def test_pages_ranges():
    assert parse_page_spec("1-3,5,8-10", 10) == [0, 1, 2, 4, 7, 8, 9]


def test_pages_invalid():
    assert parse_page_spec("0-5", 10) is None
    assert parse_page_spec("3-1", 10) is None
    assert parse_page_spec("12", 10) is None
    assert parse_page_spec("x-y", 10) is None


def test_sumatra_settings_no_copies_token():
    """Copies are printed via one Sumatra invocation per copy (the 'Nx'
    multiplier is not honored reliably with -print-to), so -print-settings
    must never carry a copies token."""
    from printer_local import _sumatra_settings
    assert _sumatra_settings({"copies": 3}) == "scale=fit"
    assert _sumatra_settings({"copies": 2, "fit": "actual"}) == "scale=none"
    assert "copies=" not in _sumatra_settings({"copies": 5})
    assert "x" not in _sumatra_settings({"copies": 5}).split(",")[-1]
    assert _sumatra_settings({"copies": 1, "duplex": "long"}) == "scale=fit,duplex=long"


def test_print_pdf_runs_sumatra_once_per_copy(tmp_path, monkeypatch):
    """Regression: copies=2 printed only 1 copy on hosts where SumatraPDF
    ignores the 'Nx' settings multiplier. print_pdf must invoke Sumatra
    once per copy."""
    import subprocess
    from printer_local import print_pdf
    f = tmp_path / "doc.pdf"
    f.write_bytes(b"%PDF-1.7 test")
    monkeypatch.setattr("printer_local._find_sumatra",
                        lambda: "C:/SumatraPDF.exe")
    calls = []

    def fake_run(cmd, capture_output=True, timeout=0):
        calls.append((cmd, timeout))
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr("subprocess.run", fake_run)
    print_pdf(str(f), "Fake Printer", {"copies": 3}, timeout=90)
    assert len(calls) == 3
    for cmd, timeout in calls:
        assert cmd[0] == "C:/SumatraPDF.exe"
        assert "-print-settings" not in cmd or "copies=" not in cmd[-1]
        assert cmd[-1].endswith("doc.pdf")
        assert timeout == 30  # 90 // 3


def test_preview_sniff_docx(tmp_path):
    from preview import _sniff_head
    p = tmp_path / "d.docx"
    p.write_bytes(_zip({"[Content_Types].xml": b"<x/>",
                        "word/document.xml": b"<w/>"}))
    assert _sniff_head(str(p)) == "docx"


def test_preview_sniff_png_head_only(tmp_path):
    from preview import _sniff_head
    p = tmp_path / "p.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32 + b"x" * 100_000)
    assert _sniff_head(str(p)) == "png"


def test_normalize_page_spec():
    from preview import normalize_page_spec
    assert normalize_page_spec("1-3, 5") == "1-3,5"
    assert normalize_page_spec("1-3", 5) == "1-3"
    assert normalize_page_spec("2") == "2"
    import pytest
    with pytest.raises(ValueError):
        normalize_page_spec("3-1")
    with pytest.raises(ValueError):
        normalize_page_spec("1-9", 5)
    with pytest.raises(ValueError):
        normalize_page_spec("0")
    with pytest.raises(ValueError):
        normalize_page_spec("x")


def test_preview_printer_labels():
    from preview import PreviewDialog
    assert PreviewDialog._printer_label(("656055745", "IT", "Reception Canon")) == "IT @ Reception Canon"
    assert PreviewDialog._printer_label(("656055745", "IT")) == "IT @ 656055745"
    assert PreviewDialog._printer_label(("656055745", "IT", None)) == "IT @ 656055745"


def _zip(pairs: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in pairs.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_pdf():
    assert sniff_format(b"%PDF-1.4\n%EOF") == "pdf"


def test_docx():
    data = _zip({"[Content_Types].xml": b"<x/>",
                 "word/document.xml": b"<w:document/>"})
    assert sniff_format(data) == "docx"


def test_xlsx():
    data = _zip({"[Content_Types].xml": b"<x/>",
                 "xl/workbook.xml": b"<workbook/>"})
    assert sniff_format(data) == "xlsx"


def test_pptx():
    data = _zip({"[Content_Types].xml": b"<x/>",
                 "ppt/presentation.xml": b"<p:presentation/>"})
    assert sniff_format(data) == "pptx"


def test_xps_zip_fallback():
    data = _zip({"[Content_Types].xml": b"<x/>",
                 "FixedDocumentSequence.fdseq": b"<fdseq/>"})
    assert sniff_format(data) == "xps"


def test_ole_doc():
    assert sniff_format(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
                        + b"\x00" * 64) == "doc"


def test_text():
    assert sniff_format(b"hello world\nsecond line\n") == "text"


def test_binary():
    assert sniff_format(bytes(range(256))) == "binary"


def test_png():
    assert sniff_format(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) == "png"


def test_jpg():
    assert sniff_format(b"\xff\xd8\xff\xe0" + b"\x00" * 32) == "jpg"


def test_gif():
    assert sniff_format(b"GIF89a" + b"\x00" * 32) == "gif"


def test_bmp():
    assert sniff_format(b"BM" + b"\x00" * 32) == "bmp"


def test_webp():
    assert sniff_format(b"RIFF" + b"\x00" * 4 + b"WEBP") == "webp"


def test_tiff_le():
    assert sniff_format(b"II*\x00" + b"\x00" * 32) == "tiff"


def test_tiff_be():
    assert sniff_format(b"MM\x00*" + b"\x00" * 32) == "tiff"