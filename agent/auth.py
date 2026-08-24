"""PrintLink wire authentication: per-request HMAC proofs.

The pairing token is both the AES-GCM payload key and the access credential.
Pre-0.3 the credential itself rode in a plaintext X-Token header on every
job, which made anyone who could observe one HTTP request able to decrypt
every future job. Since 0.3 the token never crosses the network:

  1. sender: GET /auth-challenge?sender_id=<id>  -> {"nonce": "<urlsafe>"}
  2. sender sends X-Sender-ID, X-Token-Hint, X-Signature where
       hint     = sha256(token)[:16]              (lookup aid, not secret)
       signature= hmac_sha256(sha256(token), nonce)
  3. host finds the grant by (sender, hint), recomputes the HMAC over its
     stored nonce (single-use, short TTL) and compares in constant time.

Replay is dead because nonces are consumed; sniffing yields nothing usable.
Since 1.0 this is the ONLY wire auth — the pre-0.3 plaintext X-Token path
was removed from both ends.
"""
import hashlib
import hmac
import secrets

from crypto import _key_from_token

HINT_LEN = 16  # hex chars of sha256(token) used as the grant-lookup hint


def token_hint(token: str) -> str:
    """Stable, non-secret grant-lookup hint derived from the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:HINT_LEN]


def new_nonce() -> str:
    """One-time challenge value issued by the host (~192 bits)."""
    return secrets.token_urlsafe(24)


def sign_nonce(token: str, nonce: str) -> str:
    """Proof that we hold `token`, bound to the host's fresh nonce."""
    key = _key_from_token(token)
    return hmac.new(key, nonce.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_nonce(token: str, nonce: str, proof_hex: str | None) -> bool:
    if not nonce or not proof_hex:
        return False
    return hmac.compare_digest(sign_nonce(token, nonce), proof_hex)
