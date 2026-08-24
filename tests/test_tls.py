"""tlsutil: host identity generation + fingerprint pinning (live TLS check).

The live-server test binds a real HTTPS listener so we know waitress's
ssl_context wiring, the generated PEM, and our fingerprint math all agree.
"""
import time

import pytest
import requests

from tlsutil import (generate_host_cert, load_cert_fingerprint,
                     probe_fingerprint)


def test_generate_host_cert_is_stable_and_identifies_pc(tmp_path):
    cert = tmp_path / "tls.pem"
    generate_host_cert(cert, "482917305")
    fp1 = load_cert_fingerprint(cert)
    generate_host_cert(cert, "482917305")          # must be a no-op
    assert load_cert_fingerprint(cert) == fp1
    assert len(fp1) == 64
    import cryptography.x509 as x509
    cert_obj = x509.load_pem_x509_certificate(cert.read_bytes())
    cn = cert_obj.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)[0]
    assert "482917305" in cn.value


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def https_server(tmp_path, monkeypatch):
    from flask import Flask, jsonify
    import server as server_mod
    from server import run_in_thread
    cert = tmp_path / "tls.pem"
    generate_host_cert(cert, "111222333")
    monkeypatch.setattr(server_mod, "TLS_CERT_FILE", cert)
    app = Flask(__name__)

    @app.get("/ping")
    def ping():
        return jsonify({"id": "111 222 333", "ok": True, "tls": True})

    port = _free_port()
    run_in_thread(app, port)
    deadline = time.time() + 10
    while time.time() < deadline:      # wait for waitress to accept
        try:
            requests.get(f"https://127.0.0.1:{port}/ping", verify=False,
                         timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    yield {"port": port, "cert": cert}
    # daemon thread dies with the process; nothing to join


def test_probe_and_https_roundtrip(https_server):
    port = https_server["port"]
    expected = load_cert_fingerprint(https_server["cert"])

    seen = probe_fingerprint("127.0.0.1", port)
    assert seen == expected            # TOFU capture sees the pinned identity

    r = requests.get(f"https://127.0.0.1:{port}/ping", verify=False,
                     timeout=5)
    assert r.status_code == 200 and r.json()["id"] == "111 222 333"


def test_pinned_session_pool_manager_rejects_wrong_cert(https_server):
    """A session pinned to a DIFFERENT fingerprint must fail the handshake."""
    port = https_server["port"]
    from tlsutil import PinnedAdapterHosts
    s = requests.Session()
    s.mount("https://", PinnedAdapterHosts.adapter("ab" * 32))
    with pytest.raises(Exception):
        s.get(f"https://127.0.0.1:{port}/ping", timeout=5)


def test_ping_advertises_tls_fields(https_server):
    port = https_server["port"]
    r = requests.get(f"https://127.0.0.1:{port}/ping", verify=False,
                     timeout=5)
    body = r.json()
    assert body.get("tls") is True
