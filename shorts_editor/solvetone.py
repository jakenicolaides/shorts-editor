"""Find the game's solved-puzzle tone in a recording by spectrogram cross-correlation.

Templates live in templates/<game>.wav (mono 16k), clipped from a real recording
with make_template(). detect() returns (time_seconds, score) for the best match,
or (None, score) when nothing clears the threshold.
"""
import numpy as np
from pathlib import Path
from scipy.io import wavfile
from scipy.signal import stft, fftconvolve

RATE = 16000
NPERSEG = 1024
HOP = 256
THRESHOLD = 0.55

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


def load_wav(path: Path) -> np.ndarray:
    rate, data = wavfile.read(str(path))
    if rate != RATE:
        raise ValueError(f"{path}: expected {RATE} Hz, got {rate}")
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data.astype(np.float32) / 32768.0


def _spec(x: np.ndarray) -> np.ndarray:
    _, _, z = stft(x, fs=RATE, nperseg=NPERSEG, noverlap=NPERSEG - HOP, padded=False)
    s = np.log1p(np.abs(z) * 100.0)
    # per-bin mean removal so broadband speech energy does not dominate
    return s - s.mean(axis=1, keepdims=True)


def _ncc(spec: np.ndarray, tmpl: np.ndarray) -> np.ndarray:
    """Normalised cross-correlation of a 2D template over time. Returns 1D scores
    indexed by the frame where the template would START."""
    F, T = tmpl.shape
    if spec.shape[1] < T:
        return np.zeros(1)
    tn = tmpl - tmpl.mean()
    tn /= (np.linalg.norm(tn) + 1e-9)
    num = np.zeros(spec.shape[1] - T + 1)
    for f in range(F):
        num += fftconvolve(spec[f], tn[f][::-1], mode="valid")
    win = np.ones(T)
    energy = np.zeros_like(num)
    for f in range(F):
        energy += fftconvolve(spec[f] ** 2, win, mode="valid")
    # subtract the local mean contribution (true NCC)
    local_sum = np.zeros_like(num)
    for f in range(F):
        local_sum += fftconvolve(spec[f], win, mode="valid")
    n = F * T
    var = energy - (local_sum ** 2) / n
    return num / (np.sqrt(np.maximum(var, 1e-9)))


def detect(audio_wav: Path, game: str, threshold: float = THRESHOLD):
    tmpl_path = TEMPLATE_DIR / f"{game}.wav"
    if not tmpl_path.exists():
        return None, 0.0
    x = load_wav(audio_wav)
    t = load_wav(tmpl_path)
    scores = _ncc(_spec(x), _spec(t))
    i = int(np.argmax(scores))
    best = float(scores[i])
    when = i * HOP / RATE
    return (when if best >= threshold else None), round(best, 3)


def detect_any(audio_wav: Path, threshold: float = THRESHOLD):
    """Try every template; return (game, time, score) for the best."""
    best = (None, None, 0.0)
    for tmpl in sorted(TEMPLATE_DIR.glob("*.wav")):
        when, score = detect(audio_wav, tmpl.stem, threshold)
        if score > best[2]:
            best = (tmpl.stem, when, score)
    return best


def make_template(audio_wav: Path, start: float, end: float, game: str) -> Path:
    x = load_wav(audio_wav)
    seg = x[int(start * RATE):int(end * RATE)]
    TEMPLATE_DIR.mkdir(exist_ok=True)
    out = TEMPLATE_DIR / f"{game}.wav"
    wavfile.write(str(out), RATE, (seg * 32767).astype(np.int16))
    return out


if __name__ == "__main__":
    import sys
    print(detect_any(Path(sys.argv[1])))
