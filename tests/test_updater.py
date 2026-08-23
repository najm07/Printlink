"""updater.py: version compare, release parsing, throttled auto-check."""
import time

import requests

import updater as upd


def test_parse_version_variants():
    assert upd.parse_version("v0.3.0") == (0, 3, 0)
    assert upd.parse_version("0.10.2") == (0, 10, 2)
    assert upd.parse_version("PrintLink 1.2") == (1, 2)
    assert upd.parse_version("") == ()
    # tuple compare does the right thing across lengths
    assert (0, 3,) < (0, 3, 1)
    assert (0, 10,) > (0, 9, 9)


def test_is_newer():
    assert upd.is_newer("v0.4.0", "0.3.0")
    assert not upd.is_newer("v0.3.0", "0.3.0")
    assert not upd.is_newer("v0.2.9", "0.3.0")


def _release(tag="v9.9.9", asset=True):
    assets = [{"name": "PrintLinkSetup-9.9.9.exe",
               "browser_download_url": "https://x/PrintLinkSetup-9.9.9.exe"}] \
        if asset else []
    return {"tag_name": tag, "assets": assets}


class FakeResp:
    def __init__(self, code, body):
        self.status_code = code
        self._body = body

    def json(self):
        return self._body


def test_fetch_latest_release_picks_installer_asset(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, headers=None:
                        FakeResp(200, _release()))
    rel = upd.fetch_latest_release()
    assert rel["tag"] == "v9.9.9"
    assert rel["asset_url"].endswith("PrintLinkSetup-9.9.9.exe")


def test_fetch_latest_release_without_asset(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, headers=None:
                        FakeResp(200, {"tag_name": "v9.9.9", "assets": []}))
    rel = upd.fetch_latest_release()
    assert rel["asset_url"] is None


def test_fetch_latest_release_network_error_returns_none(monkeypatch):
    def boom(url, timeout=None, headers=None):
        raise requests.RequestException("offline")

    monkeypatch.setattr(requests, "get", boom)
    assert upd.fetch_latest_release() is None


def test_check_update_none_when_current(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, headers=None:
                        FakeResp(200, _release(tag="v0.3.0")))
    assert upd.check_update(local="0.3.0") is None


def test_check_update_returns_info_when_newer(monkeypatch):
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, headers=None:
                        FakeResp(200, _release(tag="v99.0.0")))
    info = upd.check_update(local="0.3.0")
    assert info and info["asset_url"]


def test_should_auto_check_throttles_via_stamp(tmp_path, monkeypatch):
    stamp = tmp_path / ".last_update_check"
    stamp.write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setattr(upd, "CHECK_STAMP_FILE", stamp)
    assert upd.should_auto_check() is False          # checked just now
    old = time.time() - upd.CHECK_INTERVAL_S - 10
    import os
    os.utime(stamp, (old, old))
    assert upd.should_auto_check() is True           # a day has passed


def test_background_check_respects_throttle(tmp_path, monkeypatch):
    stamp = tmp_path / ".last_update_check"
    stamp.write_text(str(time.time()), encoding="utf-8")
    monkeypatch.setattr(upd, "CHECK_STAMP_FILE", stamp)
    fired = []
    t = upd.background_check(on_update=fired.append, force=False)
    assert t is None                                  # throttled out


def test_background_check_force_runs_and_stamps(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "CHECK_STAMP_FILE", tmp_path / ".stamp")
    monkeypatch.setattr(requests, "get",
                        lambda url, timeout=None, headers=None:
                        FakeResp(200, _release(tag="v0.3.0")))
    fired = []
    t = upd.background_check(on_update=fired.append, force=True)
    t.join(timeout=5)
    assert fired == []                                # same version -> no prompt
    assert (tmp_path / ".stamp").exists()
