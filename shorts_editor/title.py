"""Work out the video's name from the clip: read the puzzle off a frame, then
ask the game which date that puzzle is scheduled for.

  <date> <puzzle> <difficulty>      date is year first, so the archive sorts
  Twixtle        2026-09-19 pros-towed hard
  Vowelsweeper   2026-09-15 prove daily

Difficulty is the schedule's track: `daily` is the Daily, `bonus` is what
players see as Daily Hard.

The frame read is a typed guess (Claude vision, flat schema like EditDecision);
the schedule is the authority. A name is only produced when the read matches a
scheduled puzzle, and the spelling, date and track in it are the schedule's,
never the model's. No match means no name, and Jake types it.

The lookup is each game's public `api/puzzle-date.php`: a reverse lookup that
only answers a caller who already names the puzzle (both Twixtle words; at
least 8 Vowelsweeper squares), so it needs no secret and cannot be used to read
the forward queue. That is what lets the editor run on a Mac with no access to
the prod box.
"""
import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Literal

from .cancel import Token

DIFFICULTY = {"daily": "daily", "bonus": "hard"}  # schedule track -> the word in the name
API = {game: os.environ.get(f"SHORTS_API_{game.upper()}", url) for game, url in {
    "twixtle": "https://twixtle.games/api/puzzle-date.php",
    "vowelsweeper": "https://vowelsweeper.games/api/puzzle-date.php"}.items()}


class _NotDeployed(Exception):
    pass


def _api(url: str, body: dict = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "shorts-editor"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise _NotDeployed()
        raise RuntimeError(f"schedule lookup failed: HTTP {e.code} from {url.split('/')[2]}")
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise RuntimeError(f"schedule lookup failed: {getattr(e, 'reason', e)}")


def lookup(game: str, read, token: Token = None):
    """The scheduled puzzle this read names, as {"date", "track", "start"/"end" | "key"}, or None."""
    try:
        if game == "twixtle":
            a, b = _word(read.start_word), _word(read.end_word)
            if not (a and b):
                return None  # the endpoint answers a whole pair or nothing
            from urllib.parse import urlencode
            return _api(API[game] + "?" + urlencode({"start": a, "end": b})).get("match")
        if sum(len(r) - r.count("?") for r in read.grid_rows) < 8:
            return None
        return _api(API[game], {"rows": read.grid_rows}).get("match")
    except _NotDeployed:
        # TRANSITIONAL: until puzzle-date.php is deployed on both sites, fall back to
        # reading the schedule over ssh (Jake's Mac only). Delete this branch, and
        # everything under "the ssh fallback" below, once both endpoints answer.
        rows = fetch_schedule(game, token)
        return (match_twixtle(read.start_word, read.end_word, rows) if game == "twixtle"
                else match_vowelsweeper(read.grid_rows, rows))


SSH_HOST = os.environ.get("SHORTS_SSH_HOST", "unseen-server")
WINDOW = "date >= CURDATE() - INTERVAL 120 DAY"  # recent past plus the whole forward queue

SCHEDULES = {
    "twixtle": dict(
        site="/srv/www/sites/twixt.games", user="twixt", db="twixt_games",
        sql=f"SELECT date, track, start_word, end_word FROM puzzles WHERE {WINDOW} ORDER BY date, track",
        cols=("date", "track", "start", "end")),
    "vowelsweeper": dict(
        site="/srv/www/sites/vowelsweeper.games", user="vowelsweeper", db="vowelsweeper",
        sql=f"SELECT date, track, puzzle_key, JSON_COMPACT(grid) FROM puzzles "
            f"WHERE date IS NOT NULL AND openers IS NOT NULL AND {WINDOW} ORDER BY date, track",
        cols=("date", "track", "key", "grid")),
}


def _word(s: str) -> str:
    return re.sub(r"[^a-z]", "", (s or "").lower())


def title_for(game: str, row: dict) -> str:
    puzzle = f"{row['start']}-{row['end']}" if game == "twixtle" else row["key"]
    return f"{row['date']} {puzzle.lower()} {DIFFICULTY[row['track']]}"


# ---- the ssh fallback (transitional, see lookup) ---------------------------------
def fetch_schedule(game: str, token: Token = None) -> list:
    s = SCHEDULES[game]
    remote = (f"cd {s['site']} && MYSQL_PWD=\"$(grep '^DB_PASS=' .env.server | cut -d= -f2-)\" "
              f"mysql -u {s['user']} -D {s['db']} -B -N -e \"{s['sql']}\"")
    r = (token or Token()).run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", SSH_HOST, remote])
    if r.returncode:
        raise RuntimeError("schedule lookup failed: " + (r.stderr.strip().splitlines() or ["ssh error"])[-1])
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != len(s["cols"]):
            continue
        row = dict(zip(s["cols"], parts))
        if "grid" in row:
            row["grid"] = ["".join(cells) for cells in json.loads(row["grid"])]
        rows.append(row)
    return rows


def match_twixtle(start: str, end: str, rows: list):
    """The scheduled puzzle with this start/end pair. A pair is unique in either
    direction; with one word unreadable, the other must be unique on its own."""
    a, b = _word(start), _word(end)
    if a and b:
        hits = [r for r in rows if {r["start"].lower(), r["end"].lower()} == {a, b}]
    else:
        hits = [r for r in rows if (a and r["start"].lower() == a) or (b and r["end"].lower() == b)]
    return hits[0] if len(hits) == 1 else None


def match_vowelsweeper(grid_rows: list, rows: list, min_known: int = 6):
    """The scheduled board that agrees with the tiles read off the frame. '?' is a
    face-down tile (a mid-game frame only shows the opened consonants), so match
    on the known cells: enough of them, a clear winner, and one misread forgiven
    only when most of the board was read."""
    read = [re.sub(r"[^A-Z#?]", "?", (r or "").upper()) for r in grid_rows]
    scored = []
    for row in rows:
        g = row["grid"]
        if len(g) != len(read) or any(len(x) != len(y) for x, y in zip(g, read)):
            continue
        known = [(x, y) for gr, rr in zip(g, read) for x, y in zip(gr, rr) if y != "?"]
        wrong = sum(1 for x, y in known if x != y)
        if len(known) >= min_known and wrong <= (1 if len(known) >= 15 else 0):
            scored.append((wrong, row))
    scored.sort(key=lambda t: t[0])
    if not scored or (len(scored) > 1 and scored[0][0] == scored[1][0]):
        return None
    return scored[0][1]


# ---- the frame ------------------------------------------------------------------
def grab_frame(src: Path, t: float, out: Path, token: Token = None) -> Path:
    (token or Token()).run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(t, 0):.2f}", "-i", str(src),
                            "-frames:v", "1", "-vf", "scale=720:-2", "-pix_fmt", "yuvj420p", str(out)], check=True)
    return out


def frame_times(duration: float, solve_at) -> list:
    """Just after the solve both games show the whole puzzle (Twixtle: start, end
    and the path; Vowelsweeper: every tile face up). The end of the clip is the
    fallback. With no solve tone it is all there is, so take two looks: the
    capture can already be black in the last second."""
    last = max(duration - 1.0, 0.0)
    times = [min(solve_at + 2.5, last)] if solve_at is not None else [max(duration - 6.0, 0.0)]
    if last - times[0] > 2.0:
        times.append(last)
    return times


def read_frames(frames: list):
    from pydantic import BaseModel, Field
    import anthropic
    from . import editor_llm

    class PuzzleRead(BaseModel):
        game: Literal["twixtle", "vowelsweeper", "unknown"]
        start_word: str = Field(description="Twixtle: the word in the top black bar. Empty if not fully visible or not Twixtle.")
        end_word: str = Field(description="Twixtle: the word in the bottom black bar. Empty if not fully visible or not Twixtle.")
        grid_rows: List[str] = Field(description="Vowelsweeper: one string per grid row, top to bottom, one character per "
                                                 "column: the tile's letter, '?' for a face-down tile, '#' where the grid "
                                                 "has a gap with no tile. Empty list if not Vowelsweeper.")

    editor_llm._ensure_key()
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.standard_b64encode(Path(f).read_bytes()).decode()}}
               for f in frames]
    content.append({"type": "text", "text":
                    "Frames from one screen recording of a daily word puzzle (webcam above, game below), in time order. "
                    "Read the puzzle exactly as shown; do not guess or correct spellings. Twixtle is a ladder of word "
                    "boxes between two black bars. Vowelsweeper is a 5x5 grid of letter tiles. Prefer the frame that "
                    "shows the most of the puzzle."})
    resp = anthropic.Anthropic().messages.parse(
        model=editor_llm.MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": content}], output_format=PuzzleRead)
    if resp.stop_reason == "refusal":
        raise RuntimeError("the frame read was declined")
    return resp.parsed_output


def infer(src: Path, workdir: Path, duration: float, solve_at, game: str, token: Token = None) -> dict:
    """{"title": str|None, "game", "read", "match", "why"}; raises when the lookup or the frame read fails."""
    token = token or Token()
    frames = [grab_frame(src, t, workdir / f"title_frame_{i}.jpg", token) for i, t in enumerate(frame_times(duration, solve_at))]
    read = read_frames(frames)
    token.check()
    game = read.game if read.game in SCHEDULES else game
    out = {"title": None, "game": game, "read": read.model_dump(), "match": None, "why": None}
    if game not in SCHEDULES:
        out["why"] = "could not tell which game this is"
        return out
    row = lookup(game, read, token)
    token.check()
    if not row:
        seen = f"{read.start_word or '?'} to {read.end_word or '?'}" if game == "twixtle" else " / ".join(read.grid_rows) or "no grid"
        out["why"] = f"read {seen}, which is not in the {game} schedule"
        return out
    out["match"] = row
    out["title"] = title_for(game, row)
    return out


if __name__ == "__main__":
    tw = [{"date": "2026-09-18", "track": "daily", "start": "heirs", "end": "fowl"},
          {"date": "2026-09-19", "track": "bonus", "start": "pros", "end": "towed"},
          {"date": "2026-09-20", "track": "daily", "start": "pros", "end": "cons"}]
    assert title_for("twixtle", match_twixtle("PROS", "TOWED", tw)) == "2026-09-19 pros-towed hard"
    assert match_twixtle("towed", "pros", tw)["date"] == "2026-09-19"      # read upside down
    assert match_twixtle("", "FOWL", tw)["date"] == "2026-09-18"           # start scrolled off
    assert match_twixtle("PROS", "", tw) is None                           # ambiguous alone
    assert match_twixtle("PROSE", "TOAD", tw) is None                      # what Whisper heard
    vs = [{"date": "2026-09-15", "track": "daily", "key": "prove", "grid": ["U#WIT", "PROVE", "PERIL", "EASEL", "DRESS"]},
          {"date": "2026-09-16", "track": "daily", "key": "mists", "grid": ["MISTS", "IMPEL", "SAUNA", "EGRET", "RENTS"]}]
    assert title_for("vowelsweeper", match_vowelsweeper(["U#WIT", "PROVE", "PERIL", "EASEL", "DRESS"], vs)) == "2026-09-15 prove daily"
    assert match_vowelsweeper(["?#??T", "?R?V?", "P?R??", "??S??", "?????"], vs)["key"] == "prove"   # mid-game
    assert match_vowelsweeper(["U#WLT", "PROVE", "PERIL", "EASEL", "DRESS"], vs)["key"] == "prove"   # one misread
    assert match_vowelsweeper(["?#??T", "?R?V?", "B?R??", "??S??", "?????"], vs) is None             # a misread on few tiles
    assert match_vowelsweeper(["?????", "?????", "??S??", "?????", "?????"], vs) is None             # too little
    assert frame_times(141.1, 138.4) == [140.1] and frame_times(188.5, 173.8) == [176.3, 187.5] and frame_times(95.0, None) == [89.0, 94.0]
    print("ok")
