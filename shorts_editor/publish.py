"""Approve: the final goes to the game's Dropbox folder under the job's name, with
its sidecar JSON beside it (the archive), and, when this editor is connected to
the posting app (poster.py), is uploaded and scheduled there for the phone.
Config from .env (see .env.example)."""
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


def archive(job, cfg):
    """Copy the final into the archive folder. None when there is no archive here:
    ARCHIVE_DIR unset and no Dropbox on this Mac (a colleague's machine). The posting
    app holds the copy that matters then, and inventing a Dropbox folder would be worse."""
    default = Path.home() / "Library/CloudStorage/Dropbox"
    if not cfg.get("ARCHIVE_DIR") and not default.exists():
        return None
    base = Path(cfg.get("ARCHIVE_DIR") or default)
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
    """Dropbox first, then the schedule: the archive copy must not depend on a
    server being up. Both halves overwrite, so pressing the button again after a
    failure (or after a re-edit) is always the fix."""
    from . import poster
    from .cancel import Cancelled
    try:
        if poster.enabled():   # a name the schedule cannot use is refused before anything is saved under it
            poster.puzzle_from_name(job.meta.get("title") or "", job_game(job))
            from . import render
            size = (job.dir / "out.mp4").stat().st_size
            if size > render.SHARE_LIMIT:
                raise RuntimeError(f"this render is {size / 1e6:.0f} MB and the phone can only share files under "
                                   f"{render.SHARE_LIMIT / 1e6:.0f} MB: press Re-render (new renders are capped), then Approve")
        job.set(stage="saving", progress=5, msg="saving to Dropbox")
        path = archive(job, env())
        meta = job.meta
        if path:
            meta["saved_to"] = str(path)
            job._write_meta(meta)
            job.set(msg=f"saved to {path}")
        else:
            job.set(msg="no archive folder on this Mac (ARCHIVE_DIR unset, no Dropbox): not archived")
        if poster.enabled():
            job.set(stage="scheduling", progress=10, msg="uploading to the posting app")
            cut = json.loads((job.dir / "cut.json").read_text())
            res = poster.schedule(job.dir / "out.mp4", meta.get("title") or "", job_game(job), cut.get("final_duration", 0),
                                  token=job.token, on_progress=lambda f: job.set(progress=10 + int(85 * f)))
            meta = job.meta
            meta["scheduled"] = {**res, "version": meta.get("version")}
            job._write_meta(meta)
            job.set(msg=("replaced the earlier upload; " if res["replaced"] else "") +
                        ("scheduled: it is already due" if res["due_now"] else f"scheduled for {res['due_at']}"))
        job.set(stage="approved", progress=100)
        return path
    except Cancelled:
        job.set(stage="review", progress=100, msg="upload cancelled: the Dropbox copy is saved, nothing was scheduled")
    except Exception as e:
        job.set(stage="failed", error=str(e), msg="approve failed: " + str(e))
        raise
