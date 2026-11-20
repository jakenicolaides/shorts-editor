"""The editor as an AWS Lambda function: a take's two tracks come from S3, the
finished edit goes back to S3.

The event names a take and what the recorder knew about it:

  {"take": "abc123", "game": "twixtle", "date": "2026-11-19", "track": "daily",
   "offset_ms": 5, "title": "2026-11-19 over-wedding daily"}

It reads   s3://<bucket>/takes/<take>/game.<ext>  and  cam.<ext>
and writes s3://<bucket>/takes/<take>/edit/out.mp4  plus the job's small files
(transcript, cut, settings, status), which are what a later note needs to change
the edit without transcribing again.

The function's role may touch takes/ in that one bucket and nothing else, and it
is the only thing here that holds AWS access: the recorder's page is handed a
link to upload to and a link to watch, never a key.

Only /tmp is writable on Lambda, so the job folder lives there (SHORTS_WORK).

The speech model is fetched from S3 into /tmp the first time a container runs
(models/ in the same bucket, a copy of what the image holds). The image has it
too, but Lambda reads an image a block at a time as it is touched, and 1.6 GB read
that way cost about a minute and a half on every cold start. If the fetch fails,
the image's copy is used: slower, never wrong.
"""
import json
import os
import re
import shutil
import time
from pathlib import Path

BUCKET = os.environ.get("TAKES_BUCKET", "twixtle-shorts")
PREFIX = "takes/"
KEEP = ("out.mp4", "transcript.json", "cut.json", "params.json", "relax.json", "solve.json",
        "meta.json", "status.json", "title.json", "probe.json", "loudness_in.json", "loudness_out.json",
        "history.json", "traceback.txt")


MODEL_PREFIX = "models/faster-whisper-large-v3-turbo/"
MODEL_DIR = Path("/tmp/model")


def _fetch_model(s3) -> float:
    """The speech model into /tmp, once per container. Returns the seconds it took
    (0 when it was already there or could not be fetched)."""
    if (MODEL_DIR / ".complete").exists():
        os.environ["SHORTS_WHISPER_MODEL"] = str(MODEL_DIR)
        return 0.0
    from boto3.s3.transfer import TransferConfig
    t0 = time.time()
    try:
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
        MODEL_DIR.mkdir(parents=True)
        objs = s3.list_objects_v2(Bucket=BUCKET, Prefix=MODEL_PREFIX).get("Contents", [])
        if not any(o["Key"].endswith("model.bin") for o in objs):
            raise FileNotFoundError("no model under " + MODEL_PREFIX)
        cfg = TransferConfig(max_concurrency=24, multipart_chunksize=32 * 1024 * 1024)
        for o in objs:
            name = o["Key"][len(MODEL_PREFIX):]
            if name and "/" not in name:
                s3.download_file(BUCKET, o["Key"], str(MODEL_DIR / name), Config=cfg)
        (MODEL_DIR / ".complete").write_text("")
        os.environ["SHORTS_WHISPER_MODEL"] = str(MODEL_DIR)
        return round(time.time() - t0, 1)
    except Exception as e:
        print("model not fetched from S3, using the image's copy:", e)
        shutil.rmtree(MODEL_DIR, ignore_errors=True)
        os.environ.pop("SHORTS_WHISPER_MODEL", None)
        return 0.0


def handler(event, context=None):
    import boto3
    take = str(event.get("take") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", take):   # it becomes part of a key: nothing that can climb out of takes/
        raise ValueError("take must be 6 to 64 letters, digits, - or _")
    game = str(event.get("game") or "")
    if game not in ("twixtle", "vowelsweeper"):
        raise ValueError("game must be twixtle or vowelsweeper")

    s3 = boto3.client("s3")
    base = f"{PREFIX}{take}/"
    tmp = Path("/tmp") / take
    shutil.rmtree(tmp, ignore_errors=True)   # a warm container still has the last take's files
    shutil.rmtree(os.environ.get("SHORTS_WORK", "/tmp/work"), ignore_errors=True)
    tmp.mkdir(parents=True)

    t0 = time.time()
    found = {}
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
        m = re.fullmatch(re.escape(base) + r"(game|cam)\.(mp4|webm|mov|mkv)", obj["Key"])
        if m:
            dst = tmp / f"{m.group(1)}.{m.group(2)}"
            s3.download_file(BUCKET, obj["Key"], str(dst))
            found[m.group(1)] = dst
    if set(found) != {"game", "cam"}:
        raise FileNotFoundError(f"takes/{take}/ needs a game track and a cam track, found: {sorted(found) or 'neither'}")
    fetched = time.time() - t0
    model_seconds = _fetch_model(s3)

    from . import worker
    res = worker.run(found["game"], found["cam"], game, event.get("date"), event.get("track") or "daily",
                     float(event.get("offset_ms") or 0), event.get("title"))

    t1 = time.time()
    job_dir = Path(res["dir"])
    for name in KEEP:
        f = job_dir / name
        if f.exists():
            s3.upload_file(str(f), BUCKET, f"{base}edit/{name}",
                           ExtraArgs={"ContentType": "video/mp4" if name.endswith(".mp4") else
                                      "application/json" if name.endswith(".json") else "text/plain"})
    res["seconds_fetch"], res["seconds_store"] = round(fetched, 1), round(time.time() - t1, 1)
    res["seconds_model"] = model_seconds
    res["edit"] = f"s3://{BUCKET}/{base}edit/out.mp4" if (job_dir / "out.mp4").exists() else None
    res.pop("dir", None)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(job_dir, ignore_errors=True)
    return res
