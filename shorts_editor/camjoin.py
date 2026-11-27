"""Join a player's camera files into one, for a take the game was reloaded during.

The game keeps recording after a reload, but a phone's recorder cannot add to a file it did not
start, so each stretch arrives as its own file (cam.mp4, cam-1.mp4, ...) with the moment it began
on the game log's clock (the log's cam_segments). They are joined on that clock: the last picture of
one stretch is held, in silence, until the next begins, so the camera stays in step with the game's
redraw (which carries on from the same log) and the quick edit cuts the silent gap like any pause.
"""
import gzip
import json
import subprocess
from pathlib import Path

from .render import probe


def segments(log_path: Path) -> list:
    """[{seg, start_ms}] from the game's log, or [] for a take in one piece."""
    raw = Path(log_path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    segs = json.loads(raw).get("cam_segments") or []
    return sorted(segs, key=lambda s: s["seg"]) if len(segs) > 1 else []


def join(files: list, starts_ms: list, out: Path):
    """files[i] began at starts_ms[i]; one file out, the gaps held on the last frame. Returns the file
    and where the gaps fell in it ([[start, end], ...] in seconds), which the edit cuts out."""
    info = [probe(f) for f in files]
    w, h = info[0]["width"], info[0]["height"]
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for f in files:
        cmd += ["-i", str(f)]
    parts, chain, gaps, pos = [], "", [], 0.0
    for i, f in enumerate(files):
        gap = 0.0
        if i + 1 < len(files):
            gap = max(0.0, (starts_ms[i + 1] - starts_ms[i]) / 1000 - info[i]["duration"])
        pos += info[i]["duration"]
        if gap > 0:
            gaps.append([round(pos, 3), round(pos + gap, 3)])
            pos += gap
        v = (f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,"
             f"setsar=1,fps=30")
        a = f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
        if gap > 0:
            v += f",tpad=stop_mode=clone:stop_duration={gap:.3f}"
            a += f",apad=pad_dur={gap:.3f}"
        parts.append(f"{v}[v{i}];{a}[a{i}]")
        chain += f"[v{i}][a{i}]"
    graph = ";".join(parts) + f";{chain}concat=n={len(files)}:v=1:a=1[v][a]"
    cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(out)]
    subprocess.run(cmd, check=True, capture_output=True)
    return out, gaps
