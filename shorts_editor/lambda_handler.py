"""The editor as an AWS Lambda function: a take's two tracks come from S3, the
finished edit goes back to S3.

The event names a take and what the recorder knew about it:

  {"take": "abc123", "game": "twixtle", "date": "2026-11-19", "track": "daily",
   "offset_ms": 5, "title": "2026-11-19 over-wedding daily"}

It reads   s3://<bucket>/takes/<take>/game.<ext>  and  cam.<ext>
and writes s3://<bucket>/takes/<take>/edit/out.mp4  plus the job's small files
(transcript, cut, settings, status), which are what a later note needs to change
the edit without transcribing again.

With "note" in the event it does not edit from scratch: it re-opens the edit this
take already has (the small files it left under edit/), asks the model what the
note means for the cut, and renders again. Nothing is transcribed twice. The key
for the model is the function's own (ANTHROPIC_API_KEY in its settings): the page
a presenter uses never holds it.

"run" is whatever the caller sent, handed back in result.json, so the caller can
tell this run's verdict from the last one's (a take that is changed has an old
result.json sitting there already).

LAST of all it writes edit/result.json: how it ended, in a few fields. That file
is the signal. Whoever started the edit did not wait for it (the function runs for
minutes), so they watch for result.json; it is written when the edit fails too,
with the reason, so a failure is told apart from an edit still running.

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
        "history.json", "traceback.txt", "out-story.mp4", "story.json")   # the last two: a player's take only (story.py)


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
    """Run the edit, and say how it ended in result.json whichever way that was."""
    import boto3
    take = str((event or {}).get("take") or "")
    run = str((event or {}).get("run") or "")[:32]
    if take.startswith("selfrec-") and not (event or {}).get("note"):
        return _selfrec(event, take, run)
    try:
        res = _note(event) if (event or {}).get("note") else _edit(event)
    except Exception as e:
        res = {"stage": "failed", "error": str(e)[:300], "run": run}
        if re.fullmatch(r"[A-Za-z0-9_-]{6,64}", take):
            _result(boto3.client("s3"), take, res)
        raise
    res["run"] = run
    _result(boto3.client("s3"), take, res)
    return res


SELFREC_PARALLEL = "3"   # browsers redrawing a player's game at once (replay.py): a player's edit loads no speech model,
                         # so the 10 GB has room for three; a presenter's does, and stays at one


def _selfrec(event, take, run):
    """A player's own take (Twixtle's record mode): shorts_editor/selfrec.py, which writes its own result.json."""
    import boto3
    from . import selfrec
    if not re.fullmatch(r"selfrec-[a-f0-9]{24}", take):
        raise ValueError("not a player's take")
    saved = {k: os.environ.get(k) for k in ("REPLAY_PROFILE", "REPLAY_PARALLEL")}
    os.environ["REPLAY_PARALLEL"] = os.environ.get("SELFREC_PARALLEL") or SELFREC_PARALLEL   # the function's own setting wins, to tune without a build
    try:
        shutil.rmtree(os.environ.get("SHORTS_WORK", "/tmp/work"), ignore_errors=True)   # a warm container keeps the last job
        return selfrec.edit(boto3.client("s3"), take, {**event, "run": run}, Path("/tmp/selfrec"), cpu=_cpu())
    finally:   # a warm container runs a presenter's take next: it must not inherit a player's settings
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _cpu() -> str:
    """Which processor this run landed on. Lambda's machines are not all alike: timed
    2026-11-20, the same take took 224s on two fresh ones and 334s on a third, with
    nothing else different. Recorded with every result so a slow edit can be told from
    a slow machine."""
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        return "unknown"
    m = re.search(r"model name\s*:\s*(.+)", text)
    if m:
        return m.group(1).strip()
    part = re.search(r"CPU part\s*:\s*(0x[0-9a-f]+)", text)
    names = {"0xd0c": "Graviton2 (Neoverse N1)", "0xd40": "Graviton3 (Neoverse V1)", "0xd4f": "Graviton4 (Neoverse V2)"}
    return names.get(part.group(1), "arm " + part.group(1)) if part else "unknown"


def _result(s3, take: str, res: dict):
    res.setdefault("cpu", _cpu())
    body = {k: res.get(k) for k in ("stage", "error", "final_duration", "flags", "title", "seconds", "cpu", "run", "reply", "changed")}
    s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}{take}/edit/result.json", Body=json.dumps(body).encode(),
                  ContentType="application/json")


def _checked(event):
    take = str(event.get("take") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{6,64}", take):   # it becomes part of a key: nothing that can climb out of takes/
        raise ValueError("take must be 6 to 64 letters, digits, - or _")
    game = str(event.get("game") or "")
    if game not in ("twixtle", "vowelsweeper"):
        raise ValueError("game must be twixtle or vowelsweeper")
    return take, game


def _upload(s3, job_dir: Path, base: str):
    for name in KEEP:
        f = job_dir / name
        if f.exists():
            s3.upload_file(str(f), BUCKET, f"{base}edit/{name}",
                           ExtraArgs={"ContentType": "video/mp4" if name.endswith(".mp4") else
                                      "application/json" if name.endswith(".json") else "text/plain"})


def change(job_id: str, note: str) -> dict:
    """Apply a note to a job folder that holds a take's tracks and the small files of
    its edit, but neither its sound nor its picture: those are made again here, from
    the same tracks by the same code, which is quicker than fetching them."""
    from . import pipeline, transcribe
    job = pipeline.Job(job_id)
    job._mix()
    transcribe.extract_audio(job.dir / "mix.wav", job.dir / "audio.wav")   # the cut snaps its ends to this
    t0 = time.time()
    try:
        reply = job.apply_note(note)
    except Exception:
        reply = None   # apply_note has already written the failure into the job's status
    st = job.status()
    cut = pipeline._read(job.dir / "cut.json") or {}
    return {"job": job.id, "stage": st.get("stage"), "error": st.get("error"), "seconds": round(time.time() - t0, 1),
            "final_duration": cut.get("final_duration"), "flags": cut.get("flags"), "title": job.meta.get("title"),
            "reply": (reply or "")[:700],
            "changed": (job.dir / "out.mp4").exists(),   # the folder starts without a render: one here was made by this note
            "log": st.get("log", [])}


def _note(event):
    """Change an edit this take already has, as a note asks."""
    import boto3
    take, _game = _checked(event)
    note = " ".join(str(event.get("note") or "").split())[:600]
    if len(note) < 3:
        raise ValueError("the note is empty")
    s3 = boto3.client("s3")
    base = f"{PREFIX}{take}/"
    work = Path(os.environ.get("SHORTS_WORK", "/tmp/work"))
    shutil.rmtree(work, ignore_errors=True)   # a warm container still has the last take's files
    job_dir = work / take
    job_dir.mkdir(parents=True)

    t0 = time.time()
    tracks = set()
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
        key = obj["Key"]
        m = re.fullmatch(re.escape(base) + r"(game|cam)\.(mp4|webm|mov|mkv)", key)
        if m:
            s3.download_file(BUCKET, key, str(job_dir / f"track_{m.group(1)}.{m.group(2)}"))
            tracks.add(m.group(1))
        elif key.startswith(base + "edit/") and key.endswith(".json") and key.rsplit("/", 1)[1] in KEEP:
            s3.download_file(BUCKET, key, str(job_dir / key.rsplit("/", 1)[1]))
    if tracks != {"game", "cam"}:
        raise FileNotFoundError(f"takes/{take}/ has lost a track: record it again")
    for need in ("transcript.json", "cut.json", "params.json", "probe.json", "meta.json"):
        if not (job_dir / need).exists():
            raise FileNotFoundError("this take has no edit to change yet")
    fetched = time.time() - t0

    res = change(take, note)
    res["seconds_fetch"] = round(fetched, 1)
    job_dir = work / take
    _upload(s3, job_dir, base)
    shutil.rmtree(work, ignore_errors=True)
    return res


def _edit(event):
    import boto3
    take, game = _checked(event)

    s3 = boto3.client("s3")
    base = f"{PREFIX}{take}/"
    tmp = Path("/tmp") / take
    shutil.rmtree(tmp, ignore_errors=True)   # a warm container still has the last take's files
    shutil.rmtree(os.environ.get("SHORTS_WORK", "/tmp/work"), ignore_errors=True)
    tmp.mkdir(parents=True)

    t0 = time.time()
    found = {}
    for obj in s3.list_objects_v2(Bucket=BUCKET, Prefix=base).get("Contents", []):
        # game.json is a LOG of the game's screen (the recorder's ?game=log), redrawn by worker.run
        m = re.fullmatch(re.escape(base) + r"(game|cam)\.(mp4|webm|mov|mkv|json)", obj["Key"])
        if m:
            dst = tmp / f"{m.group(1)}.{m.group(2)}"
            s3.download_file(BUCKET, obj["Key"], str(dst))
            if m.group(1) not in found or m.group(2) == "json":   # a log wins over a film made from it earlier
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
    _upload(s3, job_dir, base)
    # A redrawn game is kept beside the log, as game.mp4, so a note re-opens the same picture
    # instead of drawing it again (_note reads only films).
    if found["game"].suffix == ".json":
        for f in job_dir.glob("track_game.*"):
            s3.upload_file(str(f), BUCKET, f"{base}game{f.suffix}", ExtraArgs={"ContentType": "video/mp4"})
            break
    res["seconds_fetch"], res["seconds_store"] = round(fetched, 1), round(time.time() - t1, 1)
    res["seconds_model"] = model_seconds
    res["edit"] = f"s3://{BUCKET}/{base}edit/out.mp4" if (job_dir / "out.mp4").exists() else None
    res.pop("dir", None)
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.rmtree(job_dir, ignore_errors=True)
    return res
