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
            if not text:
                continue
            words.append({
                "w": text,
                "s": round(float(w["start"]), 3),
                "e": round(float(w["end"]), 3),
                "filler": is_filler(text),
            })
    words.sort(key=lambda x: x["s"])
    return {"words": words, "text": result.get("text", "").strip(), "model": model}


if __name__ == "__main__":
    import sys
    src = Path(sys.argv[1])
    with tempfile.TemporaryDirectory() as td:
        wav = extract_audio(src, Path(td) / "a.wav")
        t = transcribe(wav)
    print(json.dumps(t, indent=1))
