import pytest
from crypto import encrypt_payload, decrypt_payload, new_pairing_token


def test_roundtrip():
    tok = new_pairing_token()
    data = b"%PDF-1.4 secret document" * 100
    blob = encrypt_payload(data, tok)
    assert blob != data and len(blob) == len(data) + 12 + 16
    assert decrypt_payload(blob, tok) == data


def test_wrong_token_fails():
    from cryptography.exceptions import InvalidTag
    blob = encrypt_payload(b"hello", new_pairing_token())
    with pytest.raises(InvalidTag):
        decrypt_payload(blob, new_pairing_token())


def test_tamper_fails():
    from cryptography.exceptions import InvalidTag
    tok = new_pairing_token()
    blob = bytearray(encrypt_payload(b"hello", tok))
    blob[20] ^= 1
    with pytest.raises(InvalidTag):
        decrypt_payload(bytes(blob), tok)


def test_too_short():
    with pytest.raises(ValueError):
        decrypt_payload(b"tiny", new_pairing_token())


def test_token_format():
    tok = new_pairing_token()
    assert len(tok) == 64 and all(c in "0123456789abcdef" for c in tok)
