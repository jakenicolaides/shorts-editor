"""Local drop UI on http://localhost:8790

  GET  /                      the page
  GET  /jobs                  job list (json)
  PUT  /upload?name=&speed=&game=&title=   raw file body -> starts a job
  GET  /jobs/<id>             status + sidecar (json)
  GET  /jobs/<id>/video       current render (range requests supported)
  POST /jobs/<id>/note        {"note": "..."}  -> editor loop, re-render
  POST /jobs/<id>/review      upload to S3 + make the phone review page (publish.py)
  POST /jobs/<id>/rerun       re-cut + re-render with current params
"""
import json
import os
import re
import threading
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from . import pipeline

ROOT = Path(__file__).resolve().parent.parent
PAGE_FILE = ROOT / "shorts_editor" / "page.html"  # read per request so edits need no restart
PORT = int(os.environ.get("SHORTS_PORT", "8790"))

_locks = {}


def _lock(job_id):
    return _locks.setdefault(job_id, threading.Lock())


def _run_bg(job_id, fn):
    def go():
        with _lock(job_id):
            fn()
    threading.Thread(target=go, daemon=True).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter
        if "/jobs/" not in fmt % args or "video" not in fmt % args:
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
        if self.path == "/jobs":
            return self._json(pipeline.list_jobs())
        m = re.match(r"^/jobs/([\w-]+)(/video)?(\?.*)?$", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        job = pipeline.Job(m.group(1))
        if m.group(2):
            return self._video(job.dir / "out.mp4")
        side = pipeline._read(job.dir / "sidecar.json", {})
        return self._json({"id": job.id, "status": job.status(), "meta": job.meta, "sidecar": side,
                           "history": pipeline._read(job.dir / "history.json", [])})

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
        m = re.match(r"^/jobs/([\w-]+)/(note|review|rerun)$", self.path)
        if not m:
            return self._json({"error": "not found"}, 404)
        job = pipeline.Job(m.group(1))
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        action = m.group(2)
        if _lock(job.id).locked():
            return self._json({"error": "job is busy"}, 409)
        if action == "note":
            note = (body.get("note") or "").strip()
            if not note:
                return self._json({"error": "empty note"}, 400)
            _run_bg(job.id, lambda: job.apply_note(note))
        elif action == "rerun":
            _run_bg(job.id, lambda: (job.recut(), job.rerender()))
        elif action == "review":
            from . import publish
            _run_bg(job.id, lambda: publish.send_for_review(job))
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
        if len(title) <= 11:
            return self._json({"error": "a name is required (date plus the puzzle)"}, 400)
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
        _run_bg(job.id, job.run)
        return self._json({"id": job.id})


def _poller():
    """Every 20s, look at S3 for decisions made on the phone."""
    import time
    from . import publish
    while True:
        try:
            publish.poll_once(pipeline.list_jobs, pipeline.Job, _run_bg)
        except Exception as e:
            print("poll error:", e)
        time.sleep(20)


BUSY_STAGES = {"queued", "audio", "transcribe", "solve", "cut", "loudness", "render", "editing", "uploading", "approving"}


def _sweep_orphans():
    """No work survives a restart: a job left mid-stage has nothing running it."""
    for j in pipeline.list_jobs():
        if j["status"].get("stage") in BUSY_STAGES:
            job = pipeline.Job(j["id"])
            has_video = (job.dir / "out.mp4").exists()
            job.set(stage="review" if has_video else "failed",
                    error=None if has_video else "interrupted before the first render; drop the clip again",
                    msg="server restarted mid-job" + (": showing the last render" if has_video else ""))


def main():
    pipeline.WORK.mkdir(exist_ok=True)
    _sweep_orphans()
    threading.Thread(target=_poller, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"shorts-editor on http://localhost:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
