"""Whisper transcription with word timestamps.

One model, three ways to run it: mlx-whisper on Apple Silicon (every Mac the
editor runs on), faster-whisper everywhere else (Linux, Windows), and Groq's hosted
copy of it, which the server that edits ambassadors' takes asks for because its own
processors take two minutes over what Groq does in seconds. Same model
(large-v3-turbo), same output shape, so nothing after this step knows which ran.
SHORTS_WHISPER=mlx|faster|groq forces one, which is how they are compared.

Groq is only ever ASKED FOR, never the default, and never the only way: with no
key, or on any failure, the local runner does the job (slower, never wrong), and
the transcript says which one ran and why.

Output shape (transcript.json):
  {"words": [{"w": "hello", "s": 1.23, "e": 1.61, "filler": false}, ...],
   "text": "...", "model": "..."}
"""
import os
import re
import sys
import json
import platform
import tempfile
from pathlib import Path

from .cancel import Token

MODEL = "mlx-community/whisper-large-v3-turbo"
FASTER_MODEL = "large-v3-turbo"   # the same weights, converted for faster-whisper (CTranslate2)


GROQ_MODEL = "whisper-large-v3-turbo"
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"


def local_backend() -> str:
    return "mlx" if (sys.platform == "darwin" and platform.machine() == "arm64") else "faster"


def backend() -> str:
    want = os.environ.get("SHORTS_WHISPER", "").strip().lower()
    if want in ("mlx", "faster", "groq"):
        return want
    return local_backend()

# What counts as a filler: the word on its own, case-insensitive, punctuation stripped.
FILLER_RE = re.compile(r"^(u+m+|u+h+|e+r+m*|e+h+|a+h+|h+m+|m+m+|o+h+)$")


def is_filler(word: str) -> bool:
    w = re.sub(r"[^a-z]", "", word.lower())
    return bool(w) and bool(FILLER_RE.match(w))


def extract_audio(src: Path, dst: Path, rate: int = 16000, token: Token = None) -> Path:
    """Mono 16k wav for whisper + tone matching. Written beside dst and moved into
    place, so a cancelled extract never leaves a short wav that looks finished."""
    tmp = dst.with_name(dst.stem + ".tmp.wav")
    try:
        (token or Token()).run(
            ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
             "-c:a", "pcm_s16le", str(tmp)],
            check=True,
        )
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def _energy_gate(words, audio_wav: Path, margin_db: float = 20.0):
    """Drop words whose span holds no real audio energy: Whisper invents phrases
    like "Thank you." inside long silences, and they would be kept as content."""
    import numpy as np
    from scipy.io import wavfile
    rate, x = wavfile.read(str(audio_wav))
    x = x.astype(np.float32) / 32768.0
    def rms_db(s, e):
        a, b = int(s * rate), max(int(e * rate), int(s * rate) + 1)
        seg = x[a:b]
        return float(20 * np.log10(np.sqrt((seg ** 2).mean() + 1e-12))) if len(seg) else -120.0
    for w in words:
        w["db"] = round(rms_db(w["s"], w["e"]), 1)
    if not words:
        return words
    med = float(np.median([w["db"] for w in words]))
    floor = med - margin_db
    kept = [w for w in words if w["db"] >= floor]
    return kept


def _run_mlx(audio_wav: Path, model: str):
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio_wav),
        path_or_hf_repo=model,
        word_timestamps=True,
        language="en",
        condition_on_previous_text=False,
    )
    raw = [(w["word"], w["start"], w["end"], w.get("probability", 1.0))
           for seg in result.get("segments", []) for w in seg.get("words", [])]
    return raw, result.get("text", "")


def _run_faster(audio_wav: Path, model: str):
    from faster_whisper import WhisperModel

    m = WhisperModel(model, device="cpu",
                     # float32 is the weights as they are; int8 is quicker and close (SHORTS_WHISPER_COMPUTE)
                     compute_type=os.environ.get("SHORTS_WHISPER_COMPUTE", "float32"),
                     cpu_threads=int(os.environ.get("SHORTS_WHISPER_THREADS", "0") or 0) or (os.cpu_count() or 4))
    # the wav is already what Whisper wants (mono, 16k: extract_audio), so it is handed over as
    # samples. Given a path, faster-whisper decodes it with PyAV, whose versions break each other
    import numpy as np
    from scipy.io import wavfile
    rate, x = wavfile.read(str(audio_wav))
    if rate != 16000 or x.ndim != 1:
        raise RuntimeError(f"expected a mono 16 kHz wav, got {rate} Hz, {x.ndim} dimension(s)")
    segments, _ = m.transcribe(
        x.astype(np.float32) / 32768.0,
        word_timestamps=True,
        language="en",
        condition_on_previous_text=False,
        beam_size=1,   # mlx-whisper decodes greedily; faster-whisper's own default is a beam of 5
    )
    raw, text = [], []
    for seg in segments:
        text.append(seg.text)
        raw += [(w.word, w.start, w.end, w.probability) for w in (seg.words or [])]
    return raw, "".join(text)


def _run_groq(audio_wav: Path, model: str):
    """The same model, run by Groq. The sound leaves this machine: that is the cost,
    and it is why this is asked for by a setting and is not what a Mac does."""
    import subprocess
    import urllib.error
    import urllib.request
    import uuid
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        raise RuntimeError("no GROQ_API_KEY")
    # FLAC: the same samples in about a third of the bytes, so less to send
    with tempfile.TemporaryDirectory() as td:
        flac = Path(td) / "audio.flac"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(audio_wav), "-c:a", "flac", str(flac)], check=True)
        body_file = flac.read_bytes()
    fields = [("model", model), ("response_format", "verbose_json"), ("timestamp_granularities[]", "word"),
              ("language", "en"), ("temperature", "0")]
    bound = uuid.uuid4().hex
    parts = b"".join(f'--{bound}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields)
    parts += (f'--{bound}\r\nContent-Disposition: form-data; name="file"; filename="audio.flac"\r\n'
              f'Content-Type: audio/flac\r\n\r\n').encode() + body_file + f"\r\n--{bound}--\r\n".encode()
    req = urllib.request.Request(GROQ_URL, data=parts, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "multipart/form-data; boundary=" + bound,
        "User-Agent": "shorts-editor"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            j = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Groq answered HTTP {e.code}: {e.read()[:200].decode(errors='replace')}")
    words = j.get("words")
    if not isinstance(words, list) or (not words and (j.get("text") or "").strip()):
        raise RuntimeError("Groq sent no word timings")
    # no per-word confidence comes back; 1.0 is "not known", and nothing downstream cuts on it
    return [(w["word"], w["start"], w["end"], 1.0) for w in words], j.get("text", "")


def transcribe(audio_wav: Path, model: str = None) -> dict:
    which = backend()
    fell_back = None
    if which == "groq":
        try:
            raw, full = _run_groq(audio_wav, model or GROQ_MODEL)
            model = "groq:" + (model or GROQ_MODEL)
        except Exception as e:   # no key, no network, a refusal: the local runner is slower and always there
            fell_back = str(e)[:200]
            which, model = local_backend(), None
    if which == "groq":
        pass
    elif which == "mlx":
        model = model or MODEL
        raw, full = _run_mlx(audio_wav, model)
    else:
        model = model or os.environ.get("SHORTS_WHISPER_MODEL") or FASTER_MODEL
        raw, full = _run_faster(audio_wav, model)
        model = "faster-whisper:" + model
    words = []
    for word, start, end, prob in raw:
        text = word.strip()
        if not re.search(r"[A-Za-z0-9]", text):
            continue  # Whisper emits lone punctuation over silence
        words.append({
            "w": text,
            "s": round(float(start), 3),
            "e": round(float(end), 3),
            "filler": is_filler(text),
            "p": round(float(prob), 2),
        })
    words.sort(key=lambda x: x["s"])
    n = len(words)
    words = _energy_gate(words, audio_wav)
    out = {"words": words, "text": full.strip(), "model": model,
           "dropped_silent": n - len(words)}
    if fell_back:
        out["fell_back"] = fell_back   # Groq was asked for and did not answer: why, for whoever wonders at the time it took
    return out


_one_at_a_time = __import__("threading").Lock()   # Whisper: one model load, one GPU; overlapping runs gain nothing


def transcribe_killable(audio_wav: Path, out_json: Path, token: Token, on_wait=None) -> dict:
    """transcribe() in a child interpreter: a thread running Whisper cannot be
    stopped, a process can. Costs a model load per job (a few seconds). Jobs run in
    parallel, but this step is taken in turn: several clips dropped together each
    load the 1.5 GB model and share the one GPU, so overlapping them is no faster and
    can run the machine out of memory. A cancel while waiting for the turn is honoured."""
    tmp = out_json.with_name(out_json.stem + ".tmp.json")
    waited = False
    while not _one_at_a_time.acquire(timeout=0.5):
        token.check()
        if not waited and on_wait:
            on_wait(); waited = True
    try:
        r = token.run([sys.executable, "-m", "shorts_editor.transcribe", str(audio_wav), str(tmp)],
                      cwd=str(Path(__file__).resolve().parent.parent))
    finally:
        _one_at_a_time.release()
    try:
        if r.returncode:
            raise RuntimeError("transcription failed:\n" + r.stderr[-2000:])
        os.replace(tmp, out_json)
    finally:
        tmp.unlink(missing_ok=True)
    return json.loads(out_json.read_text())


def frame_db(audio_wav: Path, frame_ms: int = 10):
    """Per-frame RMS in dB for edge snapping."""
    import numpy as np
    from scipy.io import wavfile
    rate, x = wavfile.read(str(audio_wav))
    x = x.astype(np.float32) / 32768.0
    n = int(rate * frame_ms / 1000)
    m = len(x) // n
    fr = x[:m * n].reshape(m, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1)) + 1e-9)
    return db, frame_ms / 1000.0


def snap_start(db, dt, t, max_back=0.6):
    """Walk back from t while the audio is still speech, so the cut lands on the
    onset rather than on Whisper's (late) word timestamp."""
    import numpy as np
    thr = float(np.percentile(db, 90)) - 20.0  # 20 dB under the speech level
    i = int(t / dt)
    lo = max(0, int((t - max_back) / dt))
    hi = min(len(db) - 1, int((t + max_back) / dt))
    if db[i] > thr:
        while i - 1 >= lo and db[i - 1] > thr:   # Whisper late: walk back to the onset
            i -= 1
    else:
        while i + 1 <= hi and db[i + 1] <= thr:  # Whisper early: walk forward to the onset
            i += 1
        i += 1
    return i * dt


def snap_end(db, dt, t, max_fwd=0.8):
    """Walk forward from t while the audio is still speech (Whisper ends early)."""
    import numpy as np
    thr = float(np.percentile(db, 90)) - 20.0  # 20 dB under the speech level
    i = int(t / dt)
    hi = min(len(db) - 1, int((t + max_fwd) / dt))
    lo = max(0, int((t - max_fwd) / dt))
    if db[i] > thr:
        while i + 1 <= hi and db[i + 1] > thr:   # Whisper early: walk forward to the offset
            i += 1
    else:
        while i - 1 >= lo and db[i - 1] <= thr:  # Whisper late: walk back to the offset
            i -= 1
        i -= 1
    return (i + 1) * dt


if __name__ == "__main__":
    # <clip>            transcribe a clip and print the JSON
    # <wav> <out.json>  what transcribe_killable runs
    from ._env import clean_dyld
    clean_dyld()
    src = Path(sys.argv[1])
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(json.dumps(transcribe(src), indent=1))
    else:
        with tempfile.TemporaryDirectory() as td:
            wav = extract_audio(src, Path(td) / "a.wav")
            t = transcribe(wav)
        print(json.dumps(t, indent=1))
