"""ffmpeg rendering: apply a keep list, speed change, loudness normalisation,
and encode 1080x1920 H.264 (matching the Resolve TikTok 1080p preset in spirit:
high profile, yuv420p, AAC 48k, faststart)."""
import json
import re
import subprocess
from pathlib import Path

TARGET_I = -14.0
TARGET_TP = -1.0
TARGET_LRA = 11.0


def _atempo_chain(speed: float) -> str:
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    while s < 0.5:
        parts.append("atempo=0.5")
        s /= 0.5
    parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def _graph(keep, speed, loud=None, video=True):
    n = len(keep)
    parts = []
    for i, (s, e) in enumerate(keep):
        if video:
            parts.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    if video:
        ins = "".join(f"[v{i}][a{i}]" for i in range(n))
        parts.append(f"{ins}concat=n={n}:v=1:a=1[vc][ac]")
        parts.append(
            f"[vc]setpts=PTS/{speed:.5f},scale=1080:1920:force_original_aspect_ratio=decrease,"
            f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2,format=yuv420p[vout]")
    else:
        ins = "".join(f"[a{i}]" for i in range(n))
        parts.append(f"{ins}concat=n={n}:v=0:a=1[ac]")
    ln = f"loudnorm=I={TARGET_I}:TP={TARGET_TP}:LRA={TARGET_LRA}"
    if loud:
        ln += (f":measured_I={loud['input_i']}:measured_TP={loud['input_tp']}"
               f":measured_LRA={loud['input_lra']}:measured_thresh={loud['input_thresh']}"
               f":offset={loud['target_offset']}:linear=true:print_format=summary")
    else:
        ln += ":print_format=json"
    parts.append(f"[ac]{_atempo_chain(speed)},{ln}[aout]")
    return ";".join(parts)


def measure_loudness(src: Path, keep, speed) -> dict:
    """First loudnorm pass on the cut + sped audio only."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
           "-filter_complex", _graph(keep, speed, None, video=False),
           "-map", "[aout]", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm measurement failed:\n" + r.stderr[-2000:])
    return json.loads(m.group(0))


def render(src: Path, keep, speed, out: Path, loud: dict, crf: int = 18, on_progress=None) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1", "-i", str(src),
           "-filter_complex", _graph(keep, speed, loud, video=True),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-profile:v", "high",
           "-level", "4.2", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
           str(out)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in p.stdout:
        if on_progress and line.startswith("out_time_us="):
            try:
                on_progress(int(line.split("=")[1]) / 1e6)
            except ValueError:
                pass
    err = p.stderr.read()
    if p.wait() != 0:
        raise RuntimeError("render failed:\n" + err[-3000:])
    return out


def probe(src: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                        "format=duration:stream=codec_type,width,height,r_frame_rate",
                        "-of", "json", str(src)], capture_output=True, text=True, check=True)
    j = json.loads(r.stdout)
    info = {"duration": float(j["format"]["duration"])}
    for s in j.get("streams", []):
        if s.get("codec_type") == "video":
            info.update(width=s.get("width"), height=s.get("height"), fps=s.get("r_frame_rate"))
    return info


def measure_output_loudness(path: Path) -> dict:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                        "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                       capture_output=True, text=True)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    return json.loads(m.group(0)) if m else {}
