"""A player's own take (Twixtle's record mode), edited: the one copy of how, run by the Lambda function for
every player and by bin/selfrec-watch.py on Jake's Mac when the posting app says SELFREC_EDITOR=mac.

  takes/<id>/game.json            the game's log
  takes/<id>/cam.<ext>, cam-N.<ext>   the camera, one file per stretch between reloads (camjoin.py)
  -> takes/<id>/edit/out.mp4, out-story.mp4 and the job's small files, result.json LAST

The phone's Cancel deletes the take; an edit that finds the log gone keeps nothing and writes no verdict.
Nothing of the take is kept anywhere else, unless `keep` (the Mac's debugging copy) is given.
"""
import json
import os
import re
import shutil
import threading
import time
import traceback
from pathlib import Path

from . import camjoin, worker
from .lambda_handler import BUCKET, KEEP


def _gone(s3, base: str) -> bool:
    """The take was deleted (the player cancelled, or the video already reached their phone)."""
    try:
        s3.head_object(Bucket=BUCKET, Key=f"{base}game.json")
        return False
    except Exception:
        return True


def edit(s3, take: str, ev: dict, work: Path, keep: Path = None, cpu: str = None) -> dict:
    """Edit one take and put the result where the phone looks. Returns the verdict (or {'cancelled': True})."""
    base = f"takes/{take}/"
    tmp = Path(work) / take
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    os.environ["REPLAY_PROFILE"] = "player"   # a player's own video (replay.py, worker.py): the caller restores it
    found, cams = {}, {}
    for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
        name = o["Key"][len(base):]
        m = re.fullmatch(r"cam(?:-(\d+))?\.(mp4|webm)", name)
        if name == "game.json" or m:
            s3.download_file(BUCKET, o["Key"], str(tmp / name))
            if m:
                cams[int(m.group(1) or 0)] = tmp / name
            else:
                found["game"] = tmp / name
    if 0 in cams:
        found["cam"] = cams[0]
    t0 = time.time()
    res = {"stage": "failed", "error": None, "run": ev.get("run")}
    # The edit's progress, sent on to storage every two seconds for the phone's bar (progress.py).
    prog = tmp / "progress.json"
    os.environ["PROGRESS_FILE"] = str(prog)
    stop = threading.Event()

    def send_progress():
        last = None
        while not stop.wait(2):
            try:
                body = prog.read_bytes()
                if body != last:
                    s3.put_object(Bucket=BUCKET, Key=f"{base}edit/progress.json", Body=body, ContentType="application/json")
                    last = body
            except Exception:
                pass
    threading.Thread(target=send_progress, daemon=True).start()
    job = None
    try:
        if set(found) != {"game", "cam"}:
            raise FileNotFoundError("the take is missing a track")
        segs = camjoin.segments(found["game"])   # the game was reloaded while recording: one camera file per stretch
        gaps = []
        if segs:
            missing = [s["seg"] for s in segs if s["seg"] not in cams]
            if missing:
                raise FileNotFoundError(f"the take is missing camera part {missing[0]}")
            found["cam"], gaps = camjoin.join([cams[s["seg"]] for s in segs], [s["start_ms"] for s in segs], tmp / "cam-joined.mp4")
        r = worker.run(found["game"], found["cam"], ev.get("game") or "twixtle", ev.get("date"),
                       ev.get("track") or "daily", 0.0, ev.get("title"), cuts=gaps, seal=ev.get("seal") or None)
        job = Path(r["dir"])
        if keep is not None:   # the Mac only, while it is tried: the take's files and the cut, to look into later
            shutil.rmtree(keep, ignore_errors=True)
            shutil.copytree(tmp, keep, ignore=shutil.ignore_patterns("progress.json"))
            for name in ("cut.json", "params.json", "meta.json", "transcript.json", "solve.json", "story.json"):
                if (job / name).exists():
                    shutil.copy(job / name, keep / name)
        if not _gone(s3, base):
            for name in KEEP:
                f = job / name
                if f.exists():
                    s3.upload_file(str(f), BUCKET, f"{base}edit/{name}",
                                   ExtraArgs={"ContentType": "video/mp4" if name.endswith(".mp4") else "application/json"})
        res.update({"stage": r["stage"], "error": r["error"], "final_duration": r["final_duration"],
                    "flags": r["flags"], "title": r["title"], "story": r.get("story")})
    except Exception as e:
        res["error"] = str(e)[:300]
        traceback.print_exc()
    finally:
        stop.set()
        os.environ.pop("PROGRESS_FILE", None)
        if job is not None:
            shutil.rmtree(job, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)
    if _gone(s3, base):   # they gave up waiting: nothing of the take may be left behind
        for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
            s3.delete_object(Bucket=BUCKET, Key=o["Key"])
        return {"cancelled": True}
    res["seconds"] = round(time.time() - t0, 1)
    if cpu:
        res["cpu"] = cpu
    s3.put_object(Bucket=BUCKET, Key=f"{base}edit/result.json", Body=json.dumps(res).encode(), ContentType="application/json")
    return res
