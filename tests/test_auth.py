"""auth.py: HMAC proof primitives (hint, nonce signing)."""
from auth import HINT_LEN, new_nonce, sign_nonce, token_hint, verify_nonce

TOKEN = "8f3a" * 16          # 64 hex chars, as issued by create_grant
OTHER = "cd91" * 16


def test_token_hint_is_deterministic_and_not_the_token():
    h1, h2 = token_hint(TOKEN), token_hint(TOKEN)
    assert h1 == h2
    assert len(h1) == HINT_LEN
    assert h1 != TOKEN
    assert token_hint(OTHER) != h1


def test_new_nonce_is_random_and_reasonably_sized():
    nonces = {new_nonce() for _ in range(50)}
    assert len(nonces) == 50            # no repeats
    assert all(len(n) >= 24 for n in nonces)


def test_sign_and_verify_roundtrip():
    nonce = new_nonce()
    sig = sign_nonce(TOKEN, nonce)
    assert verify_nonce(TOKEN, nonce, sig)


def test_verify_rejects_wrong_token():
    nonce = new_nonce()
    assert not verify_nonce(OTHER, nonce, sign_nonce(TOKEN, nonce))


def test_verify_rejects_tampered_nonce_or_proof():
    nonce = new_nonce()
    sig = sign_nonce(TOKEN, nonce)
    assert not verify_nonce(TOKEN, new_nonce(), sig)
    assert not verify_nonce(TOKEN, nonce, "0" * 64)
    assert not verify_nonce(TOKEN, nonce, None)
    assert not verify_nonce(TOKEN, "", sig)
