"""One recorder take in, one finished edit out, with no page and no person.

This is what the server runs for an ambassador's take: the two tracks the recorder
uploaded go in, and the job folder it leaves behind holds the edit (out.mp4) and
everything a later note needs (the transcript, the cut, the settings). It is the
same Job the Mac editor runs, started from a command instead of the page.

  python -m shorts_editor.worker <game track> <webcam track> --game twixtle
         [--date 2026-11-19] [--track daily] [--offset-ms 5] [--title "..."]

The game track may be a LOG of the game's screen instead of a film of it (.json,
gzipped or not: the recorder's ?game=log, on trial from 2026-11-26). It is redrawn
into a film first (replay.py) and the edit then runs exactly as it always has.

Prints one line of JSON: the job id, its folder, how it ended and how long each
step took. Exits 1 if the edit failed.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import pipeline


def run(game_track: Path, cam_track: Path, game: str, date: str = None, track: str = "daily",
        offset_ms: float = 0.0, title: str = None, cuts: list = None, seal: str = None, on_story=None) -> dict:
    replayed = None
    # create_from_tracks MOVES its inputs into the job; the caller keeps its own copies
    with tempfile.TemporaryDirectory() as td:
        if game_track.suffix.lower() == ".json":
            from . import replay
            g = Path(td) / "game.mp4"
            replayed = replay.render(game_track, g, _seconds(cam_track))
            offset_ms = 0.0   # the log is laid on the camera's own clock
        else:
            g = Path(shutil.copy2(game_track, Path(td) / ("game" + game_track.suffix.lower())))
        c = Path(shutil.copy2(cam_track, Path(td) / ("cam" + cam_track.suffix.lower())))
        puzzle = {"date": date, "track": track} if date else {}
        job = pipeline.Job.create_from_tracks(g, c, game, puzzle, offset_ms, title,
                                              solve_at=(replayed or {}).get("solved_at"))
    # A PLAYER'S OWN TAKE (REPLAY_PROFILE=player) is kept WHOLE (Jake, 2026-11-27: their video should be
    # what they recorded, pauses and all), less only `cuts`, the frozen gaps where the game was reloaded
    # (camjoin.py). No speech model and no AI, so it is quick too (pipeline.py, _quick).
    if os.environ.get("REPLAY_PROFILE") == "player":
        job._write_meta({**job.meta, "quick": True, "whole": True, "defer_render": True, "cuts": cuts or [],
                         "first_move_at": (replayed or {}).get("first_move_at")})
    t0 = time.time()
    job.run()
    story = None
    if os.environ.get("REPLAY_PROFILE") == "player" and job.status().get("stage") == "cut":
        # the 30 second story and the short-form video, both straight from the take's tracks, and the end card on
        # both (story.py). If that fails: the take rendered whole as it used to be, and the two made from that; and
        # if that fails too, the whole one alone, so a player gets a video rather than nothing.
        from . import story as story_mod
        try:
            story = story_mod.make(job.dir, replayed, date, tracks=job.tracks(), seal=seal, on_story=on_story)
            job.set(stage="review", progress=100, msg=f"made the story and the short-form video: {story}")
        except Exception as e:
            first = str(e)[:300]
            job.rerender()
            try:
                story = {**story_mod.make(job.dir, replayed, date, seal=seal), "tracks_failed": first}
            except Exception as e2:
                story = {"error": str(e2)[:300], "tracks_failed": first}
    st = job.status()
    cut = pipeline._read(job.dir / "cut.json") or {}
    return {
        "job": job.id, "dir": str(job.dir), "stage": st.get("stage"), "error": st.get("error"),
        "seconds": round(time.time() - t0, 1), "final_duration": cut.get("final_duration"),
        "flags": cut.get("flags"), "title": job.meta.get("title"), "log": st.get("log", []),
        **({"replay": replayed} if replayed else {}), **({"story": story} if story else {}),
    }


def _seconds(path: Path) -> float:
    """How long a track runs, by its container."""
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def main(argv):
    if len(argv) < 2 or "--game" not in argv:
        sys.exit(__doc__)
    opt = lambda k, d=None: argv[argv.index(k) + 1] if k in argv else d
    res = run(Path(argv[0]), Path(argv[1]), opt("--game"), opt("--date"), opt("--track", "daily"),
              float(opt("--offset-ms", 0) or 0), opt("--title"))
    print(json.dumps(res))
    sys.exit(0 if res["stage"] == "review" else 1)


if __name__ == "__main__":
    main(sys.argv[1:])
