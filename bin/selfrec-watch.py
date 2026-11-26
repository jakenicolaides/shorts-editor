#!/usr/bin/env python3
"""Edit players' own recordings (the game's record mode, a prototype) on THIS Mac.

While the prototype is being iterated on, the posting app is set to SELFREC_EDITOR=mac:
instead of starting the Lambda function it leaves takes/<id>/request.json beside a take.
This watches for those notes, edits each take with the editor code as it stands here (so a
change is tried in seconds, with no image rebuild), and puts the edit back exactly where the
function would have: takes/<id>/edit/out.mp4 and the job's small files, result.json last.
The phone cannot tell the difference.

  .venv/bin/python bin/selfrec-watch.py        (needs `aws login --region eu-west-1`)

Only takes named selfrec-*: presenters' takes always go to the function.
"""
import json
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

import boto3  # noqa: E402

os.environ["REPLAY_PROFILE"] = "player"   # a player's own video: the board at its own width (replay.py)

from shorts_editor import worker  # noqa: E402
from shorts_editor.lambda_handler import BUCKET, KEEP  # noqa: E402

PREFIX = "takes/selfrec-"
WORK = ROOT / "work-selfrec"


def pending(s3):
    """Take ids with a request note and no verdict yet."""
    ids, done = {}, set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=PREFIX):
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) == 3 and parts[2] == "request.json":
                ids[parts[1]] = o["Key"]
            elif len(parts) == 4 and parts[2] == "edit" and parts[3] == "result.json":
                done.add(parts[1])
    return [(i, k) for i, k in ids.items() if i not in done]


def edit(s3, take: str, req_key: str):
    ev = json.loads(s3.get_object(Bucket=BUCKET, Key=req_key)["Body"].read())
    base = f"takes/{take}/"
    tmp = WORK / take
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    found = {}
    for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
        name = o["Key"][len(base):]
        if name in ("game.json", "cam.mp4", "cam.webm"):
            s3.download_file(BUCKET, o["Key"], str(tmp / name))
            found["game" if name == "game.json" else "cam"] = tmp / name
    t0 = time.time()
    res = {"stage": "failed", "error": None, "run": ev.get("run")}
    try:
        if set(found) != {"game", "cam"}:
            raise FileNotFoundError("the take is missing a track")
        r = worker.run(found["game"], found["cam"], ev.get("game") or "twixtle", ev.get("date"),
                       ev.get("track") or "daily", 0.0, ev.get("title"))
        job = Path(r["dir"])
        for name in KEEP:
            f = job / name
            if f.exists():
                s3.upload_file(str(f), BUCKET, f"{base}edit/{name}",
                               ExtraArgs={"ContentType": "video/mp4" if name.endswith(".mp4") else "application/json"})
        res.update({"stage": r["stage"], "error": r["error"], "final_duration": r["final_duration"],
                    "flags": r["flags"], "title": r["title"]})
        print(f"  edited in {time.time() - t0:.0f}s: {r['stage']} {r['final_duration']}s {r.get('replay', {}).get('size')}", flush=True)
    except Exception as e:
        res["error"] = str(e)[:300]
        traceback.print_exc()
    res["seconds"] = round(time.time() - t0, 1)
    res["cpu"] = "Jake's Mac"
    s3.put_object(Bucket=BUCKET, Key=f"{base}edit/result.json", Body=json.dumps(res).encode(), ContentType="application/json")
    shutil.rmtree(tmp, ignore_errors=True)


def main():
    s3 = boto3.client("s3", region_name="eu-west-1")
    print("watching for players' takes; Ctrl-C to stop", flush=True)
    while True:
        try:
            for take, key in pending(s3):
                print(time.strftime("%H:%M:%S"), "editing", take, flush=True)
                edit(s3, take, key)
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("watch error:", e, flush=True)
        time.sleep(4)


if __name__ == "__main__":
    main()
