"""One recorder take in, one finished edit out, with no page and no person.

This is what the server runs for an ambassador's take: the two tracks the recorder
uploaded go in, and the job folder it leaves behind holds the edit (out.mp4) and
everything a later note needs (the transcript, the cut, the settings). It is the
same Job the Mac editor runs, started from a command instead of the page.

  python -m shorts_editor.worker <game track> <webcam track> --game twixtle
         [--date 2026-11-19] [--track daily] [--offset-ms 5] [--title "..."]

Prints one line of JSON: the job id, its folder, how it ended and how long each
step took. Exits 1 if the edit failed.
"""
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import pipeline


def run(game_track: Path, cam_track: Path, game: str, date: str = None, track: str = "daily",
        offset_ms: float = 0.0, title: str = None) -> dict:
    # create_from_tracks MOVES its inputs into the job; the caller keeps its own copies
    with tempfile.TemporaryDirectory() as td:
        g = Path(shutil.copy2(game_track, Path(td) / ("game" + game_track.suffix.lower())))
        c = Path(shutil.copy2(cam_track, Path(td) / ("cam" + cam_track.suffix.lower())))
        puzzle = {"date": date, "track": track} if date else {}
        job = pipeline.Job.create_from_tracks(g, c, game, puzzle, offset_ms, title)
    t0 = time.time()
    job.run()
    st = job.status()
    cut = pipeline._read(job.dir / "cut.json") or {}
    return {
        "job": job.id, "dir": str(job.dir), "stage": st.get("stage"), "error": st.get("error"),
        "seconds": round(time.time() - t0, 1), "final_duration": cut.get("final_duration"),
        "flags": cut.get("flags"), "title": job.meta.get("title"), "log": st.get("log", []),
    }


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
