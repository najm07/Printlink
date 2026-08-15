"""End-to-end API tests: real Flask app + test client, pywin32 stubbed."""
import io
import sys
import types
import pytest

# stub Windows-only printing before server imports it
fake = types.ModuleType("printer_local")
fake.DEFAULT_OPTIONS = {"copies": 1, "pages": "", "paper": "auto",
                        "color": "color", "duplex": "off",
                        "orientation": "auto", "fit": "fit"}
fake.list_printers = lambda: ["HP LaserJet Pro"]
fake.printer_status = lambda n: {"offline": False, "paused": False,
                                 "error": False, "jobs_queued": 0,
                                 "port": "USB001", "name": n}
PRINTED = []
fake.print_via_shell = lambda path, printer: PRINTED.append((open(path, "rb").read(), printer))
fake.print_text = lambda data, printer, job="PrintLink Job", copies=1: PRINTED.append((data, printer))
fake.print_emf = lambda data, printer, job="PrintLink Job": PRINTED.append((data, printer))
fake.print_word = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer))
fake.print_image = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer))
fake.print_pdf = lambda path, printer, opts=None: PRINTED.append((open(path, "rb").read(), printer, opts))
fake.sniff_format = lambda d: ("pdf" if d[:5] == b"%PDF-" else
                               "docx" if b"word/document.xml" in d else
                               "png" if d[:4] == b"\x89PNG" else
                               "xps" if d[:4] == b"PK\x03\x04" else
                               "text" if b"\x00" not in d[:1024] else "binary")
fake.extract_emf = lambda d: None
sys.modules["printer_local"] = fake

from db import Database
from server import create_app
from crypto import encrypt_payload


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_shared_printer("HP LaserJet Pro", "Accounting-HP")
    app = create_app(db, "482 917 305", on_share_request=lambda *a: True)
    return app.test_client(), db


def request_share(client, sender="111 222 333", alias="Accounting-HP"):
    r = client.post("/request-share", json={"sender_id": sender, "sender_name": "Test-PC",
                                            "printer_alias": alias, "days": 7})
    return r


def do_print(client, token, sender="111 222 333", payload=b"%PDF-1.4 test",
             options=None):
    body = encrypt_payload(payload, token)
    fields = {"file": (io.BytesIO(body), "doc.pdf")}
    if options is not None:
        import json
        fields["options"] = json.dumps(options)
    return client.post("/print", headers={"X-Sender-ID": sender, "X-Token": token},
                       data=fields, content_type="multipart/form-data")


def test_ping(env):
    client, _ = env
    assert client.get("/ping").get_json()["id"] == "482 917 305"


def test_share_accept_and_print(env):
    client, _ = env
    r = request_share(client)
    assert r.status_code == 200
    token = r.get_json()["token"]
    r = do_print(client, token)
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"


def test_unknown_printer_404(env):
    client, _ = env
    assert request_share(client, alias="Ghost").status_code == 404


def test_decline_403(tmp_path):
    db = Database(tmp_path / "t.db")
    db.add_shared_printer("HP", "a")
    app = create_app(db, "1", on_share_request=lambda *a: False)
    r = app.test_client().post("/request-share",
                               json={"sender_id": "111 222 333", "printer_alias": "a"})
    assert r.status_code == 403


def test_revoke_grant(env):
    client, db = env
    token = request_share(client).get_json()["token"]
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333", "printer_alias": "Ghost",
                          "token": token})
    assert r.status_code == 404
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333", "printer_alias": "Accounting-HP",
                          "token": "wrong"})
    assert r.status_code == 404
    r = client.post("/revoke-grant",
                    json={"sender_id": "111 222 333", "printer_alias": "Accounting-HP",
                          "token": token})
    assert r.status_code == 200 and r.get_json()["status"] == "revoked"
    assert db.list_grants()[0]["status"] == "revoked"


def test_bad_token_403(env):
    client, _ = env
    assert do_print(client, "wrong").status_code == 403


def test_no_file_400(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = client.post("/print", headers={"X-Sender-ID": "111 222 333", "X-Token": token})
    assert r.status_code == 400


def test_printers_listing(env):
    client, _ = env
    rows = client.get("/printers").get_json()
    assert rows[0]["alias"] == "Accounting-HP" and not rows[0]["status"]["offline"]


def test_print_word_docx(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    docx = (b"PK\x03\x04\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            b"\x00\x00\x00\x00\x00\x00\x00\x00\x00[Content_Types].xml"
            b"\x00\x00\x00\x00\x00\x00\x00\x00"
            b"word/document.xml")
    body = encrypt_payload(docx, token)
    r = client.post("/print", headers={"X-Sender-ID": "111 222 333", "X-Token": token},
                    data={"file": (io.BytesIO(body), "doc.docx")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"
    assert b"word/document.xml" in PRINTED[-1][0]


def test_print_png(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    body = encrypt_payload(png, token)
    r = client.post("/print", headers={"X-Sender-ID": "111 222 333", "X-Token": token},
                    data={"file": (io.BytesIO(body), "pic.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][1] == "HP LaserJet Pro"
    assert PRINTED[-1][0].startswith(b"\x89PNG")


def test_print_pdf_with_options(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token,
                 options={"copies": 2, "pages": "1-3,5", "paper": "A4",
                          "color": "mono", "duplex": "long", "fit": "fit"})
    assert r.status_code == 200 and r.get_json()["status"] == "accepted"
    assert PRINTED[-1][2] == {"copies": 2, "pages": "1-3,5", "paper": "A4",
                              "color": "mono", "duplex": "long", "fit": "fit"}


def test_print_bad_options_400(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token, options="not-json{")
    assert r.status_code == 400


def test_print_text_copies(env):
    client, _ = env
    token = request_share(client).get_json()["token"]
    r = do_print(client, token, payload=b"hello text\n",
                 options={"copies": 3})
    assert r.status_code == 200
    assert PRINTED[-1] == (b"hello text\n", "HP LaserJet Pro")
