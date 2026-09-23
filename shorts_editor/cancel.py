"""Cancelling a running job. The long stages are all child processes (ffmpeg, and
Whisper in its own interpreter), so cancelling is killing the current child and
raising Cancelled at the next check. One Token per background action."""
import subprocess
import threading


class Cancelled(Exception):
    pass


class Token:
    def __init__(self):
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._proc = None

    def cancel(self):
        self._ev.set()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.kill()

    def check(self):
        if self._ev.is_set():
            raise Cancelled()

    def popen(self, cmd, **kw):
        self.check()
        with self._lock:
            p = subprocess.Popen(cmd, **kw)
            self._proc = p
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
