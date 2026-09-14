"""Approve: archive the final to Dropbox, upload to S3 for the phone, register
with the prod review page. Config from .env (see .env.example)."""
import json
import os
import shutil
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def env():
    cfg = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    cfg.update({k: v for k, v in os.environ.items() if k in (
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "S3_BUCKET", "S3_REGION",
        "REVIEW_BASE", "REVIEW_SECRET", "ARCHIVE_DIR", "ANTHROPIC_API_KEY")})
    return cfg


def final_name(job) -> str:
    game = (job.meta.get("game") or "clip")
    solve = json.loads((job.dir / "solve.json").read_text()) if (job.dir / "solve.json").exists() else {}
    if game == "auto" and solve.get("game"):
        game = solve["game"]
    date = job.id[:10]
    return f"{date}-{game}.mp4"


def archive(job, cfg) -> Path:
    base = Path(cfg.get("ARCHIVE_DIR") or (Path.home() / "Library/CloudStorage/Dropbox/shorts"))
    dst_dir = base / job.id[:10]
    dst_dir.mkdir(parents=True, exist_ok=True)
    name = final_name(job)
    shutil.copy2(job.dir / "out.mp4", dst_dir / name)
    shutil.copy2(job.dir / "sidecar.json", dst_dir / (name[:-4] + ".json"))
    return dst_dir / name


def upload_s3(job, cfg) -> dict:
    if not (cfg.get("AWS_ACCESS_KEY_ID") and cfg.get("S3_BUCKET")):
        return {"skipped": "S3 not configured"}
    import boto3
    s3 = boto3.client("s3", region_name=cfg.get("S3_REGION", "eu-west-1"),
                      aws_access_key_id=cfg["AWS_ACCESS_KEY_ID"],
                      aws_secret_access_key=cfg["AWS_SECRET_ACCESS_KEY"])
    name = final_name(job)
    key = f"shorts/{job.id}/{name}"
    s3.upload_file(str(job.dir / "out.mp4"), cfg["S3_BUCKET"], key,
                   ExtraArgs={"ContentType": "video/mp4"})
    s3.upload_file(str(job.dir / "sidecar.json"), cfg["S3_BUCKET"], key[:-4] + ".json",
                   ExtraArgs={"ContentType": "application/json"})
    url = s3.generate_presigned_url("get_object", Params={"Bucket": cfg["S3_BUCKET"], "Key": key},
                                    ExpiresIn=7 * 24 * 3600)
    return {"bucket": cfg["S3_BUCKET"], "key": key, "url": url}


def register(job, cfg, s3info) -> dict:
    if not (cfg.get("REVIEW_BASE") and cfg.get("REVIEW_SECRET")):
        return {"skipped": "review endpoint not configured"}
    body = json.dumps({"id": job.id, "name": final_name(job), "s3": s3info,
                       "sidecar": json.loads((job.dir / "sidecar.json").read_text())}).encode()
    req = urllib.request.Request(cfg["REVIEW_BASE"].rstrip("/") + "/api/shorts-register.php", data=body,
                                 headers={"Content-Type": "application/json",
                                          "X-Shorts-Secret": cfg["REVIEW_SECRET"]})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"{}")


def approve(job):
    cfg = env()
    try:
        job.set(stage="approving", msg="archiving to Dropbox")
        path = archive(job, cfg)
        job.set(msg=f"archived {path}")
        job.set(msg="uploading to S3")
        s3info = upload_s3(job, cfg)
        job.set(msg=f"s3: {s3info.get('key') or s3info.get('skipped')}")
        reg = register(job, cfg, s3info)
        job.set(msg=f"registered: {reg}")
        job.set(stage="approved", progress=100, s3=s3info, archived=str(path))
    except Exception as e:
        job.set(stage="failed", error=str(e), msg="approve failed: " + str(e))
        raise
