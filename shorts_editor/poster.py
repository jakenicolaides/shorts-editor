"""Hand an approved video to the posting app (shorts.unseenforms.com), which holds
it until its slot, notifies whoever posts that game, and gives them the file and
the caption on their phone. Posting itself stays manual and in-app: that is the
point (see CLAUDE.md).

Three calls: announce the puzzle and get an upload URL, PUT the file to it (in
production a presigned S3 URL, so this Mac holds no AWS keys and the file never
passes through the app's PHP), then confirm. The app keeps one video per puzzle,
so approving a re-edit replaces the earlier upload and keeps its slot.

Config is two lines in .env, minted per computer on the app's Settings page:
  POSTER_URL=https://shorts.unseenforms.com
  POSTER_TOKEN=...
Without them the editor works exactly as before and approve only saves to Dropbox.
"""
import http.client
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from .cancel import Token

ROOT = Path(__file__).resolve().parent.parent
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) ([a-z0-9]+(?:-[a-z0-9]+)*) (daily|hard)$")
TRACK = {"daily": "daily", "hard": "bonus"}


def config() -> dict:
    cfg = {}
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(("POSTER_URL=", "POSTER_TOKEN=")):
                k, v = line.split("=", 1)
                cfg[k] = v.strip()
    for k in ("POSTER_URL", "POSTER_TOKEN"):   # the environment wins, as for ARCHIVE_DIR: a test instance beside the real one
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    return cfg if cfg.get("POSTER_URL") and cfg.get("POSTER_TOKEN") else {}


def enabled() -> bool:
    return bool(config())


def puzzle_from_name(name: str, game: str) -> dict:
    """The job's name IS the puzzle's identity (title.py writes it from the schedule,
    and it is what the reviewer sees and can correct), so it is what gets sent."""
    m = NAME_RE.match((name or "").strip().lower())
    if not m:
        raise ValueError("to schedule it, the name must read <date> <puzzle> <daily|hard>, e.g. 2026-09-19 pros-towed hard")
    if game not in ("twixtle", "vowelsweeper"):
        raise ValueError("cannot tell which game this is, so there is nobody to schedule it for")
    date, puzzle, diff = m.groups()
    out = {"game": game, "track": TRACK[diff], "puzzle_date": date, "start_word": "", "end_word": "", "puzzle_key": ""}
    if game == "twixtle":
        if puzzle.count("-") != 1:
            raise ValueError("a Twixtle name needs both words: <date> <start>-<end> <daily|hard>")
        out["start_word"], out["end_word"] = puzzle.split("-")
    else:
        out["puzzle_key"] = puzzle
    return out


def _call(cfg, path, body=None):
    req = urllib.request.Request(cfg["POSTER_URL"].rstrip("/") + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + cfg["POSTER_TOKEN"],
                                          "Content-Type": "application/json", "User-Agent": "shorts-editor"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error")
        except ValueError:
            err = None
        if e.code == 401:
            raise RuntimeError("the posting app refused this editor's token (POSTER_TOKEN in .env): mint a new one in its Settings")
        raise RuntimeError(f"posting app: {err or 'HTTP ' + str(e.code)}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise RuntimeError(f"posting app unreachable: {getattr(e, 'reason', e)}")


def ping() -> dict:
    return _call(config(), "/api/editor/ping.php")


def _put(url: str, path: Path, headers: dict, token: Token, on_progress=None):
    """Stream the file in 1 MB pieces, so a cancel lands between pieces and the page has a progress bar."""
    u = urlparse(url)
    conn = (http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection)(u.netloc, timeout=120)
    size = path.stat().st_size
    try:
        conn.putrequest("PUT", u.path + ("?" + u.query if u.query else ""))
        for k, v in {"Content-Length": str(size), "Content-Type": "video/mp4", **headers}.items():
            conn.putheader(k, v)
        conn.endheaders()
        sent = 0
        with open(path, "rb") as f:
            while chunk := f.read(1 << 20):
                token.check()
                conn.send(chunk)
                sent += len(chunk)
                if on_progress:
                    on_progress(sent / size)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status not in (200, 201, 204):
            raise RuntimeError(f"upload refused: HTTP {resp.status} {body[:200].decode(errors='replace')}")
    finally:
        conn.close()


def schedule(video: Path, name: str, game: str, duration_s: float, token: Token = None, on_progress=None) -> dict:
    """Returns {"uid", "due_at" (UTC ISO), "due_now", "replaced"}."""
    cfg = config()
    token = token or Token()
    meta = puzzle_from_name(name, game)
    made = _call(cfg, "/api/editor/videos.php", {**meta, "title": name, "bytes": video.stat().st_size, "duration_s": duration_s})
    token.check()
    up = made["upload"]
    _put(up["url"], video, {"Authorization": "Bearer " + cfg["POSTER_TOKEN"]} if up.get("send_token") else {}, token, on_progress)
    done = _call(cfg, "/api/editor/complete.php", {"uid": made["uid"]})
    return {"uid": made["uid"], "due_at": done["due_at"], "due_now": done.get("due_now", False), "replaced": made.get("replaced", False)}



def record_candidates(days: int = 8) -> list:
    """Every puzzle from yesterday for `days` days with its standing, for the checklist:
    [{"game", "track", "date", "due_at", "status": wanted|recorded|posted, "suggested", "label"}]."""
    return _call(config(), "/api/editor/record-links.php", {"mode": "list", "days": int(days)})["candidates"]


def record_open(picks: list) -> list:
    """Signed links for exactly these [{"game", "track", "date"}], in date order."""
    return _call(config(), "/api/editor/record-links.php", {"mode": "open", "picks": list(picks)})["links"]


def record_links(days: int, games: list, probe_only: bool = False) -> dict:
    """Signed links that open scheduled puzzles for recording, from the posting app
    (which shares a secret with the games). {"from", "depth": {game: days queued},
    "links": [{"game", "track", "date", "label", "url"}], "expires_at"}."""
    return _call(config(), "/api/editor/record-links.php", {"days": int(days), "games": list(games), "probe_only": bool(probe_only)})


if __name__ == "__main__":
    import sys
    if sys.argv[1:] == ["ping"]:   # the installer's check: is this Mac connected?
        if not enabled():
            sys.exit("not connected: no POSTER_URL / POSTER_TOKEN in .env")
        try:
            r = ping()
        except RuntimeError as e:
            sys.exit(str(e))
        print(f"connected to {config()['POSTER_URL']} as {r['user']}" + ("" if r.get("anthropic_key") else " (no shared Anthropic key set there yet)"))
        sys.exit(0)
    assert puzzle_from_name("2026-09-19 pros-towed hard", "twixtle") == {
        "game": "twixtle", "track": "bonus", "puzzle_date": "2026-09-19", "start_word": "pros", "end_word": "towed", "puzzle_key": ""}
    assert puzzle_from_name("2026-09-15 Prove daily", "vowelsweeper")["puzzle_key"] == "prove"
    assert puzzle_from_name("2026-09-15 mists-2 daily", "vowelsweeper")["puzzle_key"] == "mists-2"   # a key with a collision suffix
    for bad, game in [("2026-09-19 pros-toad", "twixtle"), ("2026-09-19 pros hard", "twixtle"), ("pros-towed hard", "twixtle"),
                      ("2026-09-19 pros-towed hard", "auto")]:
        try:
            puzzle_from_name(bad, game); raise SystemExit(f"accepted {bad!r}")
        except ValueError:
            pass
    print("ok")
