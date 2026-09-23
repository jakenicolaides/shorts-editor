"""Prepare puzzles: open the next N days of scheduled puzzles in Chrome, one tab
each, ready to record. The links come from the posting app (poster.record_links):
this Mac never holds the games' recording secret. Chrome is addressed by profile,
because the admin sign-in that some game surfaces expect lives in one profile and
`open -a` would land the tabs in whichever window was last used.
"""
import json
import subprocess
from pathlib import Path

CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
LOCAL_STATE = Path.home() / "Library/Application Support/Google/Chrome/Local State"


def chrome_profiles() -> list:
    """[{"dir": "Profile 2", "name": "Fifos x"}, ...] and the last-used one first."""
    try:
        j = json.loads(LOCAL_STATE.read_text())["profile"]
    except (OSError, ValueError, KeyError):
        return []
    last = j.get("last_used", "Default")
    out = [{"dir": k, "name": v.get("name") or k} for k, v in (j.get("info_cache") or {}).items()]
    return sorted(out, key=lambda p: (p["dir"] != last, p["name"].lower()))


def open_in_chrome(urls: list, profile: str = None) -> None:
    """Hand the URLs to Chrome in one go; a running Chrome opens them as tabs in the
    named profile's window. Detached, so the editor never waits on the browser."""
    if not urls:
        return
    if CHROME.exists():
        cmd = [str(CHROME)] + ([f"--profile-directory={profile}"] if profile else []) + urls
    else:
        cmd = ["open", "-a", "Google Chrome"] + urls
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
