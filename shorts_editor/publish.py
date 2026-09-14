"""Save to Dropbox: the approved final goes to the game's folder under the name
typed at upload, with its sidecar JSON beside it. The phone picks it up from
Dropbox. Config from .env (see .env.example)."""
import json
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GAME_FOLDERS = {"twixtle": "twixtle/Dailies", "vowelsweeper": "vowelsweeper"}


def env():
    cfg = {}
    p = ROOT / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip()
    for k in ("ARCHIVE_DIR", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            cfg[k] = os.environ[k]
    if cfg.get("ANTHROPIC_API_KEY"):
        os.environ.setdefault("ANTHROPIC_API_KEY", cfg["ANTHROPIC_API_KEY"])
    return cfg


def job_game(job) -> str:
    game = job.meta.get("game") or "auto"
    if game == "auto":
        solve = json.loads((job.dir / "solve.json").read_text()) if (job.dir / "solve.json").exists() else {}
        game = solve.get("game") or "clip"
    return game


def final_name(job) -> str:
    """The name typed at upload, else <date>-<game>."""
    title = (job.meta.get("title") or "").strip()
    safe = "".join(c for c in title if c not in '/\\:*?"<>|').strip()
    return (safe or f"{job.id[:10]}-{job_game(job)}") + ".mp4"


def archive(job, cfg) -> Path:
    base = Path(cfg.get("ARCHIVE_DIR") or (Path.home() / "Library/CloudStorage/Dropbox"))
    dst_dir = base / GAME_FOLDERS.get(job_game(job), "shorts")
    try:
        dst_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        # macOS refuses the Dropbox folder to some processes; keep the final locally
        dst_dir = ROOT / "work" / "_archive" / GAME_FOLDERS.get(job_game(job), "shorts")
        dst_dir.mkdir(parents=True, exist_ok=True)
        job.set(msg=f"Dropbox folder not writable from this process, saved under {dst_dir} instead")
    name = final_name(job)
    shutil.copy2(job.dir / "out.mp4", dst_dir / name)
    shutil.copy2(job.dir / "sidecar.json", dst_dir / (name[:-4] + ".json"))
    return dst_dir / name


def approve(job):
    try:
        job.set(stage="saving", msg="saving to Dropbox")
        path = archive(job, env())
        meta = job.meta
        meta["saved_to"] = str(path)
        job._write_meta(meta)
        job.set(stage="approved", progress=100, msg=f"saved to {path}")
        return path
    except Exception as e:
        job.set(stage="failed", error=str(e), msg="save failed: " + str(e))
        raise
