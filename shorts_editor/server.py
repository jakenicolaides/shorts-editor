"""Local drop UI on http://localhost:8790

  GET  /                      the page
  GET  /jobs                  job list (json)
  PUT  /upload?name=           raw file body -> starts a job. That is the whole intake: the game and
                              the name are worked out from the clip, speed is 1.0 until changed on the
                              job (&speed= &game= &title= still override, for scripts)
  GET  /jobs/<id>             status + sidecar (json)
  GET  /jobs/<id>/video       current render (range requests supported)
  POST /jobs/<id>/note        {"note": "..."}  -> editor loop, re-render
  POST /jobs/<id>/title       {"title": "..."} -> rename (the Dropbox file name)
  POST /jobs/<id>/speed       {"speed": 1.2}   -> re-cut (the 1:30 floor is post-speed) + re-render
  POST /jobs/<id>/approve     save the final to Dropbox and, when connected, schedule it in the posting app (publish.py)
  POST /jobs/<id>/rerun       re-cut + re-render with current params (or pick a stopped first run back up)
  POST /jobs/<id>/cancel      stop whatever is running on the job
  GET  /prepare?games=a,b&days=N   how many days each game has queued (via the posting app)
  POST /prepare               {"days", "games", "profile"} -> open one Chrome tab per scheduled puzzle
  DELETE /jobs/<id>           cancel if running, then remove work/<id>/ (the Dropbox copy stays)
"""
import json
import os
import re
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from . import pipeline, poster

ROOT = Path(__file__).resolve().parent.parent
PAGE_FILE = ROOT / "shorts_editor" / "page.html"  # read per request so edits need no restart
PORT = int(os.environ.get("SHORTS_PORT", "8790"))

_locks = {}
_heavy = threading.Lock()   # one job works at a time: Whisper and ffmpeg each want the whole machine
_page = {"last_ping": 0.0, "seen": False, "bye_at": 0.0}


def _lock(job_id):
    return _locks.setdefault(job_id, threading.Lock())


def _run_bg(job, fn):
    def go():
        with _lock(job.id):
            if not job.exists():  # deleted before it started
                return
            job.begin()
            if _heavy.locked():
                job.set(stage="queued", msg="waiting: another job is running")
            while not _heavy.acquire(timeout=0.5):   # a cancel or delete while waiting must not wait for the other job
                if job.token.cancelled or not job.exists():
                    if job.exists():
                        job._cancelled()
                    return
            try:
                if job.exists():
                    fn()
            finally:
                _heavy.release()
    threading.Thread(target=go, daemon=True).start()


def _name_ok(title):
    return len(title) > 11  # more than the date


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        line = fmt % args
        if "/ping" in line or ("/jobs" in line and "video" not in line):
            return
        super().log_message(fmt, *args)

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            body = PAGE_FILE.read_text().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/ping":
            import time
            _page["last_ping"] = time.time(); _page["seen"] = True
            return self._json({"ok": True})
        if self.path == "/jobs":
            return self._json(pipeline.list_jobs())
        if self.path.startswith("/prepare"):
            from urllib.parse import urlparse, parse_qs
            from . import prepare
            q = parse_qs(urlparse(self.path).query)
            if not poster.enabled():
                return self._json({"error": "not connected to the posting app (POSTER_URL in .env)"}, 400)
            games = [g for g in (q.get("games", ["twixtle,vowelsweeper"])[0]).split(",") if g]
            try:
                r = poster.record_links(int(q.get("days", ["14"])[0]), games, probe_only=True)
            except Exception as e:
                r = {"error": str(e)}
            r["profiles"] = prepare.chrome_profiles()   # the profile list is this Mac's, whatever the posting app said
            return self._json(r, 502 if "error" in r else 200)
        m = re.match(r"^/jobs/([\w-]+)(/video)?(\?.*)?$", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        job = pipeline.Job(m.group(1))
        if not job.exists():
            return self._json({"error": "no such job"}, 404)
        if m.group(2):
            return self._video(job.dir / "out.mp4")
        side = pipeline._read(job.dir / "sidecar.json", {})
        return self._json({"id": job.id, "status": job.status(), "meta": job.meta, "sidecar": side,
                           "history": pipeline._read(job.dir / "history.json", []), "poster": poster.enabled()})

    def _video(self, path: Path):
        if not path.exists():
            return self._json({"error": "no video yet"}, 404)
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                if m.group(1):
                    start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
                elif start:
                    end = size - 1
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 20, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)

    def do_POST(self):
        if self.path == "/bye":
            import time
            _page["bye_at"] = time.time()
            return self._json({"ok": True})
        if self.path == "/prepare":
            from . import prepare
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            if not poster.enabled():
                return self._json({"error": "not connected to the posting app (POSTER_URL in .env)"}, 400)
            games = [g for g in (body.get("games") or []) if g in ("twixtle", "vowelsweeper")]
            days = max(1, min(31, int(body.get("days") or 1)))
            if not games:
                return self._json({"error": "tick at least one game"}, 400)
            try:
                r = poster.record_links(days, games)
            except Exception as e:
                return self._json({"error": str(e)}, 502)
            if not body.get("dry"):   # dry: the test suite, which wants the list without a browser opening
                prepare.open_in_chrome([l["url"] for l in r["links"]], body.get("profile") or None)
            return self._json({"opened": [l["label"] for l in r["links"]], "depth": r["depth"], "starts": r.get("starts", {})})
        m = re.match(r"^/jobs/([\w-]+)/(note|title|speed|approve|rerun|cancel)$", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        job = pipeline.Job(m.group(1))
        if not job.exists():
            return self._json({"error": "no such job"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        action = m.group(2)
        if action == "cancel":
            if not _lock(job.id).locked():
                return self._json({"error": "nothing is running on this job"}, 409)
            job.cancel()
            return self._json({"ok": True})
        if _lock(job.id).locked():
            return self._json({"error": "job is busy"}, 409)
        if action == "title":
            title = (body.get("title") or "").strip()
            if not _name_ok(title):
                return self._json({"error": "a name is date, puzzle, difficulty, e.g. 2026-09-14 aye-hassle daily"}, 400)
            job.rename(title)
        elif action == "speed":
            try:
                speed = round(float(body.get("speed")), 3)
            except (TypeError, ValueError):
                speed = 0
            if not 0.5 <= speed <= 2.0:
                return self._json({"error": "speed is between 0.5 and 2"}, 400)
            if not (job.dir / "cut.json").exists():
                return self._json({"error": "nothing to re-render yet: try again first"}, 409)
            _run_bg(job, lambda: job.set_speed(speed))
        elif action == "note":
            note = (body.get("note") or "").strip()
            if not note:
                return self._json({"error": "empty note"}, 400)
            _run_bg(job, lambda: job.apply_note(note))
        elif action == "rerun":
            _run_bg(job, job.rerun)
        elif action == "approve":
            if not _name_ok(job.meta.get("title") or ""):
                return self._json({"error": "name the video first: it becomes the file name in Dropbox"}, 400)
            from . import publish
            _run_bg(job, lambda: publish.approve(job))
        return self._json({"ok": True})

    def do_DELETE(self):
        m = re.match(r"^/jobs/([\w-]+)$", self.path)
        job = pipeline.Job(m.group(1)) if m else None
        if not job or not job.exists():
            return self._json({"error": "no such job"}, 404)
        job.cancel()
        lock = _lock(job.id)
        if not lock.acquire(timeout=20):
            return self._json({"error": "still stopping, try again in a moment"}, 409)
        try:
            job.delete()
        finally:
            lock.release()
        return self._json({"ok": True})

    def do_PUT(self):
        from urllib.parse import urlparse, parse_qs, unquote
        u = urlparse(self.path)
        if u.path != "/upload":
            return self._json({"error": "not found"}, 404)
        q = parse_qs(u.query)
        name = Path(unquote(q.get("name", ["clip.mp4"])[0])).name
        speed = float(q.get("speed", ["1.0"])[0])
        game = q.get("game", ["auto"])[0]
        title = unquote(q.get("title", [""])[0]).strip()
        if title and not _name_ok(title):
            return self._json({"error": "a name is the date plus the puzzle (or leave it to be worked out from the clip)"}, 400)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return self._json({"error": "empty body"}, 400)
        tmp = pipeline.WORK / "_upload"
        tmp.mkdir(parents=True, exist_ok=True)
        dst = tmp / name
        with open(dst, "wb") as out:
            remaining = n
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        job = pipeline.Job.create(dst, speed, game, name=name, title=title)
        dst.unlink(missing_ok=True)
        _run_bg(job, job.run)
        return self._json({"id": job.id})


BUSY_STAGES = {"queued", "audio", "transcribe", "solve", "title", "cut", "loudness", "render", "editing", "saving", "scheduling"}


def _sweep_orphans():
    """No work survives a restart: a job left mid-stage has nothing running it."""
    for j in pipeline.list_jobs():
        if j["status"].get("stage") in BUSY_STAGES:
            job = pipeline.Job(j["id"])
            has_video = (job.dir / "out.mp4").exists()
            job.set(stage="review" if has_video else "failed",
                    error=None if has_video else "interrupted before the first render; drop the clip again",
                    msg="server restarted mid-job" + (": showing the last render" if has_video else ""))


def _busy():
    return any(l.locked() for l in _locks.values())


def _close_own_terminal():
    """Close the Terminal window this server was launched in (by its tty)."""
    import subprocess
    try:
        tty = os.ttyname(0)
    except OSError:
        return
    script = f'tell application "Terminal" to close (first window whose tty of selected tab is "{tty}") saving no'
    subprocess.Popen(["sh", "-c", f"sleep 1; osascript -e '{script}' >/dev/null 2>&1"],
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _watchdog():
    """Quit when the page has gone: no ping for 15s (or a bye with no ping after
    it for 4s, so a reload does not count). Never while a job is running.
"""
    import time
    while True:
        time.sleep(2)
        if not _page["seen"] or _busy():
            continue
        now = time.time()
        gone = (now - _page["last_ping"] > 15) or (_page["bye_at"] > _page["last_ping"] and now - _page["bye_at"] > 4)
        if gone:
            print("page closed, shutting down")
            _close_own_terminal()
            os._exit(0)


def main():
    pipeline.WORK.mkdir(exist_ok=True)
    _sweep_orphans()
    threading.Thread(target=_watchdog, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"shorts-editor on http://localhost:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
