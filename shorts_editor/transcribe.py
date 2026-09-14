"""Whisper transcription with word timestamps (mlx-whisper on Apple Silicon).

Output shape (transcript.json):
  {"words": [{"w": "hello", "s": 1.23, "e": 1.61, "filler": false}, ...],
   "text": "...", "model": "..."}
"""
import re
import json
import subprocess
import tempfile
from pathlib import Path

MODEL = "mlx-community/whisper-large-v3-turbo"

# What counts as a filler: the word on its own, case-insensitive, punctuation stripped.
FILLER_RE = re.compile(r"^(u+m+|u+h+|e+r+m*|e+h+|a+h+|h+m+|m+m+|o+h+)$")


def is_filler(word: str) -> bool:
    w = re.sub(r"[^a-z]", "", word.lower())
    return bool(w) and bool(FILLER_RE.match(w))


def extract_audio(src: Path, dst: Path, rate: int = 16000) -> Path:
    """Mono 16k wav for whisper + tone matching."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(rate),
         "-c:a", "pcm_s16le", str(dst)],
        check=True,
    )
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


def transcribe(audio_wav: Path, model: str = MODEL) -> dict:
    import mlx_whisper

    result = mlx_whisper.transcribe(
        str(audio_wav),
        path_or_hf_repo=model,
        word_timestamps=True,
        language="en",
        condition_on_previous_text=False,
    )
    words = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []):
            text = w["word"].strip()
            if not re.search(r"[A-Za-z0-9]", text):
                continue  # Whisper emits lone punctuation over silence
            words.append({
                "w": text,
                "s": round(float(w["start"]), 3),
                "e": round(float(w["end"]), 3),
                "filler": is_filler(text),
                "p": round(float(w.get("probability", 1.0)), 2),
            })
    words.sort(key=lambda x: x["s"])
    n = len(words)
    words = _energy_gate(words, audio_wav)
    return {"words": words, "text": result.get("text", "").strip(), "model": model,
            "dropped_silent": n - len(words)}


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as td:
        wav = extract_audio(src, Path(td) / "a.wav")
        t = transcribe(wav)
    print(json.dumps(t, indent=1))


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
