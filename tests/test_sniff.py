"""Format sniffing: PDF vs OOXML (docx/xlsx/pptx) vs XPS vs text vs binary."""
import io
import sys
import zipfile

sys.path.insert(0, "agent")  # real printer_local, not the test_api stub
sys.modules.pop("printer_local", None)

from printer_local import sniff_format, parse_page_spec


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