"""PrintLink transport TLS: one persistent self-signed host certificate.

Since 1.0 the agent's HTTP API speaks HTTPS on :9100. There is no CA —
trust is pinned per pairing:

- The HOST generates a self-signed certificate once (CN = its PC ID) and
  keeps it in the shared data dir; every grant is implicitly bound to it.
- The SENDer records the host certificate's SHA256 fingerprint at pairing
  (TOFU: protected by the human accept dialog + /ping ID verification)
  and refuses to talk to any other certificate afterwards — an active
  MITM can no longer read or tamper with jobs, only disconnect them.

Residual risk, stated honestly: a MITM present during the very first
pairing handshake could pin their own cert. That window is seconds long,
requires LAN position, and both users see confirmations. Re-pairing after
a host reinstall re-pins intentionally.
"""
import datetime
import hashlib
import ssl
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from logutil import get_logger

log = get_logger("tls")

CERT_VALIDITY_DAYS = 3650


def generate_host_cert(cert_file: Path, pc_id: str) -> None:
    """Create cert_file (PEM: key + cert) for pc_id if absent."""
    if cert_file.exists():
        return
    key = ec.generate_private_key(ec.SECP256R1())
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"PrintLink {pc_id}"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PrintLink"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=CERT_VALIDITY_DAYS))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None),
                           critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("printlink.local")]),
                critical=False)
            .sign(key, hashes.SHA256()))
    pem = (key.private_bytes(serialization.Encoding.PEM,
                             serialization.PrivateFormat.TraditionalOpenSSL,
                             serialization.NoEncryption())
           + cert.public_bytes(serialization.Encoding.PEM))
    tmp = cert_file.with_suffix(".pem.tmp")
    tmp.write_bytes(pem)
    import os
    os.replace(tmp, cert_file)          # atomic same-volume
    log.info("generated host TLS identity for %s (%s)", pc_id, cert_file.name)


def load_cert_fingerprint(cert_file: Path) -> str:
    """SHA256 hex of the DER certificate — the value senders pin."""
    data = Path(cert_file).read_bytes()
    cert = x509.load_pem_x509_certificate(data)
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def probe_fingerprint(host: str, port: int, timeout: float = 5) -> str | None:
    """Fetch the presented certificate without trusting it (TOFU capture).

    Returns sha256-hex of the DER cert, or None when unreachable/not TLS."""
    try:
        der = ssl.get_server_certificate((host, port),
                                         timeout=timeout).encode("ascii")
        return hashlib.sha256(ssl.PEM_cert_to_DER_cert(der.decode("ascii"))).hexdigest()
    except Exception as e:
        log.warning("tls probe %s:%s failed: %r", host, port, e)
        return None


class PinnedAdapterHosts:
    """Builds requests adapters whose urllib3 pool hard-fails on any
    certificate not matching the pinned fingerprint."""

    @staticmethod
    def adapter(fingerprint_hex: str):
        import requests.adapters
        import urllib3
        fp = ":".join(fingerprint_hex[i:i + 2]
                      for i in range(0, len(fingerprint_hex), 2)).lower()
        adapter = requests.adapters.HTTPAdapter()
        # fingerprint IS the trust anchor: skip CA + hostname verification,
        # only the pinned fingerprint matters. Without cert_reqs=CERT_NONE the
        # self-signed cert still fails CA verification before the fingerprint
        # is even checked, and without assert_hostname=False an IP-based URL
        # fails because the cert only carries DNS:printlink.local.
        adapter.poolmanager = urllib3.PoolManager(
            assert_fingerprint=fp, cert_reqs=ssl.CERT_NONE,
            assert_hostname=False)
        return adapter
