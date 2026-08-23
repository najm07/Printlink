"""logutil.clean(): client-controlled strings must not forge/flood logs."""
from logutil import clean


def test_clean_flattens_newlines():
    assert clean("line1\nline2\r\nend") == "line1\\nline2\\r\\nend"


def test_clean_truncates_long_values():
    out = clean("A" * 500, limit=50)
    assert len(out) == 51
    assert out.endswith("…")


def test_clean_handles_none_and_non_strings():
    assert clean(None) == ""
    assert clean(42) == "42"
