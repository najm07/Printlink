"""PrintLink self-update: check GitHub releases, offer, download, install.

Flow (always user-consented — never auto-installs):
  1. GET <repo>/releases/latest  -> tag_name + PrintLinkSetup-*.exe asset
  2. compare semver-ish against the running VERSION
  3. if newer: tray asks "Update now?"
  4. yes -> download the Setup exe to %TEMP%, launch it elevated (UAC),
     and exit this agent so the installer can replace the binary;
     its [Run] section starts the updated agent afterwards.

Auto-checks are throttled to once a day (stamp file in the shared data
dir); the tray menu item always checks immediately.
"""
import os
import re
import threading
import time
from pathlib import Path

import requests

from config import VERSION
from logutil import get_logger

log = get_logger("updater")

RELEASES_API_URL = "https://api.github.com/repos/najm07/Printlink/releases/latest"
ASSET_PREFIX = "PrintLinkSetup-"
CHECK_STAMP_FILE = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / \
    "PrintLink" / ".last_update_check"
CHECK_INTERVAL_S = 24 * 3600
DOWNLOAD_TIMEOUT_S = (10, 600)      # connect, read (installer is ~50 MB)


def parse_version(text: str) -> tuple[int, ...]:
    """'v0.3.10' -> (0, 3, 10); non-numeric parts ignored. Empty -> ()."""
    m = re.search(r"(\d+(?:\.\d+)*)", text or "")
    return tuple(int(p) for p in m.group(1).split(".")) if m else ()


def is_newer(remote: str, local: str = VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def extract_asset(release_json: dict) -> str | None:
    """Browser-download URL of the installer asset, None when absent."""
    for asset in release_json.get("assets") or []:
        name = asset.get("name", "")
        url = asset.get("browser_download_url")
        if name.startswith(ASSET_PREFIX) and name.endswith(".exe") and url:
            return url
    return None


def fetch_latest_release(url: str = RELEASES_API_URL,
                         timeout: float = 10) -> dict | None:
    """{'tag': ..., 'version': ..., 'asset_url': ...} or None."""
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"Accept": "application/vnd.github+json"})
        j = r.json()
    except Exception as e:
        log.info("release check failed: %r", e)
        return None
    if r.status_code != 200 or not isinstance(j, dict) or "tag_name" not in j:
        log.info("release check: HTTP %d, unexpected body", r.status_code)
        return None
    return {"tag": j["tag_name"], "version": j["tag_name"],
            "asset_url": extract_asset(j)}


def check_update(local: str = VERSION, url: str = RELEASES_API_URL) -> dict | None:
    """Update info when the latest release beats `local`, else None."""
    rel = fetch_latest_release(url)
    if not rel or not is_newer(rel["version"], local):
        return None
    log.info("update available: %s (running %s)", rel["version"], local)
    return rel


def _stamp_now() -> None:
    try:
        CHECK_STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHECK_STAMP_FILE.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def should_auto_check() -> bool:
    try:
        return time.time() - os.path.getmtime(CHECK_STAMP_FILE) > CHECK_INTERVAL_S
    except OSError:
        return True


def download_and_install(asset_url: str, version_tag: str) -> tuple[bool, str]:
    """Download the Setup exe to %TEMP% and launch it elevated.

    Returns (started, message). The caller should exit the agent shortly
    after success so the installer can swap the binary and restart it."""
    dest = Path(os.environ.get("TEMP", ".")) / f"{ASSET_PREFIX}{version_tag}.exe"
    try:
        log.info("downloading %s -> %s", asset_url, dest)
        with requests.get(asset_url, stream=True,
                          timeout=DOWNLOAD_TIMEOUT_S) as r:
            r.raise_for_status()
            tmp = dest.with_suffix(dest.suffix + ".part")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
            os.replace(tmp, dest)
        size_mb = dest.stat().st_size / (1 << 20)
        if size_mb < 5:
            return False, f"Downloaded file looks wrong ({size_mb:.1f} MB)."
        import win32api
        win32api.ShellExecute(0, "runas", str(dest), None, None, 0)
        log.info("installer launched (%.1f MB); agent will exit", size_mb)
        return True, f"Update {version_tag} downloaded — installing now."
    except Exception as e:
        log.warning("update install failed: %r", e)
        return False, f"Update failed: {e}"


def background_check(on_update=None, force: bool = False) -> threading.Thread | None:
    """Throttled startup check; on_update(info) fires only when newer.
    Returns the thread (already started) or None when throttled."""

    def run():
        try:
            info = check_update()
            if info and on_update:
                on_update(info)
        except Exception:
            log.exception("background update check crashed")
        finally:
            _stamp_now()

    if not force and not should_auto_check():
        return None
    t = threading.Thread(target=run, daemon=True, name="printlink-update")
    t.start()
    return t
