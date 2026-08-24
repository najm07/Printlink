"""Expiry lifecycle regression tests (user-reported bugs).

Bug A: "cannot add printers after expiry until the other PC revokes them".
Bug B: "extending expired grants does not work".

These pin down the full round-trip: pair -> expire -> repair.
"""
from datetime import datetime, timedelta, timezone

import pytest

from db import Database
from shares import (create_grant, authorize_print_proof,
                    sweep_expired_grants, extend_grant)
from auth import sign_nonce, token_hint


def _auth(db, remote_id, token, nonce="n"):
    return authorize_print_proof(db, remote_id, token_hint(token),
                                 nonce, sign_nonce(token, nonce))


EXP_FMT = "%Y-%m-%d %H:%M:%S"


def _expire_all_grants(db):
    with db.connect_private() as con:
        con.execute("UPDATE grants SET expires_at = '2000-01-01 00:00:00'")


def _future(days=7):
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime(EXP_FMT)


@pytest.fixture
def host(tmp_path):
    db = Database(tmp_path / "h.db")
    pid = db.add_shared_printer("HP", "Office-HP")
    return db, pid


def test_readd_after_expiry_rotates_token(host):
    """Bug A, host side: re-pairing over an EXPIRED grant must succeed and
    reactivate it — no manual revoke required."""
    db, pid = host
    g1 = create_grant(db, "111222333", "Client-PC", pid)
    _expire_all_grants(db)
    sweep_expired_grants(db)
    assert _auth(db, "111222333", g1["token"])["error"] == "share expired"

    # client re-adds: same remote+printer -> upsert must rotate + reactivate
    g2 = create_grant(db, "111222333", "Client-PC", pid)
    assert g2["token"] != g1["token"]
    grant = db.list_grants()[0]
    assert grant["status"] == "active"
    assert grant["expires_at"] > _utcnow_str()      # renewed into the future
    # new token prints via the 0.3 proof path
    res = authorize_print_proof(db, "111222333", token_hint(g2["token"]),
                                "n", sign_nonce(g2["token"], "n"))
    assert res["ok"]


def test_extend_expired_grant_reactivates_for_existing_token(host):
    """Bug B, host side: extending an expired grant must reactivate it for
    the SAME token the client still holds."""
    db, pid = host
    g = create_grant(db, "111222333", "Client-PC", pid)
    _expire_all_grants(db)
    sweep_expired_grants(db)

    extend_grant(db, db.list_grants()[0]["id"], 7)
    grant = db.list_grants()[0]
    assert grant["status"] == "active"
    res = _auth(db, "111222333", g["token"])
    assert res["ok"], f"extended grant must accept the original token: {res}"


def _utcnow_str():
    return datetime.now(timezone.utc).strftime(EXP_FMT)


def test_client_side_expiry_is_advisory_not_a_hard_block(tmp_path):
    """Bug B root cause: the CLIENT's local sweeper marks its row 'expired',
    then get_usable_printer blocks every send even after the HOST extended
    the grant — the extension never propagates. The host is authoritative:
    local staleness must warn, not veto."""
    db = Database(tmp_path / "c.db")
    exp_past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(EXP_FMT)
    from shares import store_accepted_share
    store_accepted_share(db, "111222333", "Host-PC", "192.168.1.5",
                         "Office-HP", "tok", exp_past)
    # simulate the local sweeper having flipped status
    with db.connect_private() as con:
        con.execute("UPDATE remote_printers SET status='expired'")

    from shares import get_usable_printer
    res = get_usable_printer(db, "111222333", "Office-HP")
    # revoked/removed stay hard blocks; time-expiry lets the attempt through
    assert res["ok"] is True and res.get("stale") is True
