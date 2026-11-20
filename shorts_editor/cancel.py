"""Cancelling a running job. The long stages are all child processes (ffmpeg, and
Whisper in its own interpreter), so cancelling is killing every child still running
and raising Cancelled at the next check. One Token per background action; a recorder
take runs two children at once (the picture being joined while Whisper listens)."""
import subprocess
import threading


class Cancelled(Exception):
    pass


class Token:
    def __init__(self):
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._procs = []

    def cancel(self):
        self._ev.set()
        with self._lock:
            for p in self._procs:
                if p.poll() is None:
                    p.kill()

    def check(self):
        if self._ev.is_set():
            raise Cancelled()

    @property
    def cancelled(self) -> bool:
        return self._ev.is_set()

    def popen(self, cmd, **kw):
        self.check()
        with self._lock:
            p = subprocess.Popen(cmd, **kw)
            self._procs = [q for q in self._procs if q.poll() is None] + [p]
        if self._ev.is_set():  # cancel() landed between the check and the Popen
            p.kill()
        return p

    def run(self, cmd, check=False, **kw):
        """subprocess.run(capture_output=True, text=True), killable."""
        p = self.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)
        out, err = p.communicate()
        self.check()
        if check and p.returncode:
            raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
        return subprocess.CompletedProcess(cmd, p.returncode, out, err)
