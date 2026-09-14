"""Review on the phone, entirely on S3.

send_for_review(job):
    uploads shorts/<id>/v<N>.mp4, state.json and review.html to the bucket and
    returns a 7-day presigned link to review.html. The page plays the video,
    has Approve and a notes box, and writes shorts/<id>/decision.json back
    through a presigned PUT baked into the page (same origin, so no CORS).

poll_decisions():
    called by the server loop; for every job awaiting review, fetch
    decision.json. "approve" -> archive to Dropbox, mark approved.
    "notes" -> editor loop, re-render, re-upload as v<N+1>.

Config from .env (see .env.example).
"""
import json
import os
import shutil
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REVIEW_TTL = 7 * 24 * 3600
PAGE = (ROOT / "shorts_editor" / "review.html").read_text()


def env():
    cfg = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET", "S3_REGION",
              "ARCHIVE_DIR", "ANTHROPIC_API_KEY", "IMESSAGE_TO"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if cfg.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", cfg["ANTHROPIC_API_KEY"])
    return cfg


def _s3(cfg):
    import boto3
    from botocore.config import Config
    region = cfg.get("S3_REGION", "eu-west-1")
    # regional endpoint: presigned URLs on the global host get a 307 the signature can't follow
    return boto3.client("s3", region_name=region,
                        endpoint_url=f"https://s3.{region}.amazonaws.com",
                        aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
                        aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"],
                        config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))


def final_name(job) -> str:
    game = job.meta.get("game") or "clip"
    solve = json.loads((job.dir / "solve.json").read_text()) if (job.dir / "solve.json").exists() else {}
    if game == "auto" and solve.get("game"):
        game = solve["game"]
    return f"{job.id[:10]}-{game}.mp4"


def _presign(s3, cfg, method, key, extra=None):
    params = {"Bucket": cfg["S3_BUCKET"], "Key": key}
    params.update(extra or {})
    return s3.generate_presigned_url("put_object" if method == "PUT" else "get_object",
                                     Params=params, ExpiresIn=REVIEW_TTL, HttpMethod=method)


def send_for_review(job):
    cfg = env()
    if not (cfg.get("AWS_ACCESS_KEY_ID") and cfg.get("S3_BUCKET")):
        raise RuntimeError("S3 not configured in .env")
    s3 = _s3(cfg)
    bucket = cfg["S3_BUCKET"]
    version = int(job.meta.get("version", 1))
    prefix = f"shorts/{job.id}"
    vkey = f"{prefix}/v{version}.mp4"
    job.set(stage="uploading", progress=0, msg=f"uploading v{version} to s3")

    size = (job.dir / "out.mp4").stat().st_size

    def cb(n, done=[0]):
        done[0] += n
        job.set(progress=int(90 * done[0] / size))

    s3.upload_file(str(job.dir / "out.mp4"), bucket, vkey,
                   ExtraArgs={"ContentType": "video/mp4"}, Callback=cb)
    side = job.write_sidecar()

    # state.json: what the page shows. decision.json: what the page writes.
    state = {
        "id": job.id, "version": version, "name": final_name(job),
        "video_url": _presign(s3, cfg, "GET", vkey),
        "download_url": _presign(s3, cfg, "GET", vkey, {
            "ResponseContentDisposition": f'attachment; filename="{final_name(job)}"'}),
        "final_duration": side["cut"]["final_duration"], "flags": side.get("flags", []),
        "solve": side.get("solve"), "loudness": side.get("loudness_out"),
        "speed": side["params"].get("speed"), "history": side.get("history", []),
        "status": "review", "updated": time.time(),
    }
    s3.put_object(Bucket=bucket, Key=f"{prefix}/state.json", Body=json.dumps(state).encode(),
                  ContentType="application/json", CacheControl="no-store")
    # a fresh decision file so an old decision is never re-read
    s3.put_object(Bucket=bucket, Key=f"{prefix}/decision.json", Body=b'{"action": null}',
                  ContentType="application/json", CacheControl="no-store")

    page = (PAGE.replace("__STATE_URL__", _presign(s3, cfg, "GET", f"{prefix}/state.json"))
                .replace("__DECISION_PUT_URL__", _presign(s3, cfg, "PUT", f"{prefix}/decision.json",
                                                          {"ContentType": "application/json"}))
                .replace("__JOB_ID__", job.id))
    s3.put_object(Bucket=bucket, Key=f"{prefix}/review.html", Body=page.encode(),
                  ContentType="text/html; charset=utf-8", CacheControl="no-store")
    link = _presign(s3, cfg, "GET", f"{prefix}/review.html")

    meta = job.meta
    meta.update(review_link=link, review_version=version, review_sent=time.time(), decision_seen=None)
    job._write_meta(meta)
    job.set(stage="awaiting_review", progress=100, msg=f"review link ready (v{version})", review_link=link)
    _notify(cfg, job, link)
    return link


def _notify(cfg, job, link):
    """Optional: iMessage the link to yourself (IMESSAGE_TO in .env)."""
    to = cfg.get("IMESSAGE_TO")
    if not to:
        return
    import subprocess
    msg = f"Short ready for review ({final_name(job)}, v{job.meta.get('version')}): {link}"
    try:
        subprocess.run(["osascript", "-e",
                        f'tell application "Messages" to send {json.dumps(msg)} to participant {json.dumps(to)} of account 1'],
                       check=True, capture_output=True, timeout=20)
        job.set(msg=f"iMessaged link to {to}")
    except Exception as e:  # never fail the upload over a notification
        job.set(msg=f"iMessage failed: {e}")


def archive(job, cfg) -> Path:
    base = Path(cfg.get("ARCHIVE_DIR") or (Path.home() / "Library/CloudStorage/Dropbox/shorts"))
    dst_dir = base / job.id[:10]
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # macOS refuses the Dropbox folder to some processes; keep the final locally
        dst_dir = ROOT / "work" / "_archive" / job.id[:10]
        dst_dir.mkdir(parents=True, exist_ok=True)
        job.set(msg=f"Dropbox folder not writable from this process, archived under {dst_dir} instead")
    name = final_name(job)
    shutil.copy2(job.dir / "out.mp4", dst_dir / name)
    shutil.copy2(job.dir / "sidecar.json", dst_dir / (name[:-4] + ".json"))
    return dst_dir / name


def read_decision(job, cfg=None):
    cfg = cfg or env()
    s3 = _s3(cfg)
    try:
        body = s3.get_object(Bucket=cfg["S3_BUCKET"], Key=f"shorts/{job.id}/decision.json")["Body"].read()
        return json.loads(body or b"{}")
    except Exception:
        return {}


def poll_once(list_jobs, Job, run_bg):
    """One pass over jobs awaiting review. run_bg(job_id, fn) schedules work."""
    cfg = env()
    if not cfg.get("AWS_ACCESS_KEY_ID"):
        return
    for j in list_jobs():
        st = j["status"].get("stage")
        if st != "awaiting_review":
            continue
        job = Job(j["id"])
        d = read_decision(job, cfg)
        action = d.get("action")
        if not action or d.get("at") == job.meta.get("decision_seen"):
            continue
        if int(d.get("version", 0)) != int(job.meta.get("review_version", 0)):
            continue  # a decision about an older render
        meta = job.meta
        meta["decision_seen"] = d.get("at")
        job._write_meta(meta)
        if action == "approve":
            def do_approve(job=job):
                path = archive(job, env())
                job.set(stage="approved", msg=f"approved on the phone, archived to {path}")
            run_bg(job.id, do_approve)
        elif action == "notes":
            note = (d.get("note") or "").strip()
            if note:
                def do_note(job=job, note=note):
                    job.apply_note(note)
                    if job.status().get("stage") == "review":
                        send_for_review(job)
                run_bg(job.id, do_note)
