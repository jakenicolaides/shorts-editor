"""ffmpeg rendering: apply a keep list, speed change, loudness normalisation,
and encode 1080x1920 H.264 (matching the Resolve TikTok 1080p preset in spirit:
high profile, yuv420p, AAC 48k, faststart)."""
import json
import re
import subprocess
from pathlib import Path

from .cancel import Token

# Chrome refuses to share a file over 50 MiB from a web page (blink kMaxSharedFileBytes), and the
# phone app posts by sharing. Every render is capped under it: a video that cannot be shared is not
# a video. The cap is on the file, so long takes get a lower bitrate rather than a refusal.
SHARE_LIMIT = 50 * 1024 * 1024
SIZE_TARGET = 44 * 1024 * 1024   # headroom for the container and the VBV overshooting a little
AUDIO_KBPS = 192
# Instagram takes a reel of up to three minutes; six of September's videos ran longer, up to 5:11
# (found 2026-10-01). The cut aims 2s under it (cutlist.Params.max_duration); approve holds this line.
LENGTH_LIMIT = 180.0
NOT_INSTAGRAM = ("spelling-garden",)   # never posted there (2026-10-14), so no ceiling, only the 1:30 floor


def length_limit(game: str):
    """The longest this game's render may run, in seconds; None for no limit."""
    return None if game in NOT_INSTAGRAM else LENGTH_LIMIT

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


def _is_tracks(src) -> bool:
    return hasattr(src, "inputs")   # compose.Tracks: a recorder take, two pictures and one sound


def _graph(keep, speed, loud=None, video=True, tracks=False):
    """tracks: the source is a recorder take (compose.Tracks) rather than one clip. Its
    picture is stacked here, once, and each kept stretch is cut from that; its sound is
    input 2, or input 0 when only the sound was opened (video=False)."""
    n = len(keep)
    parts = []
    asrc = "[2:a]" if (tracks and video) else "[0:a]"
    if tracks and video:
        from .compose import stack_filters
        parts += stack_filters("st")
        parts.append("[st]split=" + str(n) + "".join(f"[s{i}]" for i in range(n)))
    for i, (s, e) in enumerate(keep):
        if video:
            vsrc = f"[s{i}]" if tracks else "[0:v]"
            parts.append(f"{vsrc}trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"{asrc}atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
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


def measure_loudness(src: Path, keep, speed, token: Token = None) -> dict:
    """First loudnorm pass on the cut + sped audio only."""
    cmd = ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src.sound if _is_tracks(src) else src),
           "-filter_complex", _graph(keep, speed, None, video=False),
           "-map", "[aout]", "-f", "null", "-"]
    r = (token or Token()).run(cmd)
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    if not m:
        raise RuntimeError("loudnorm measurement failed:\n" + r.stderr[-2000:])
    return json.loads(m.group(0))


def video_maxrate_kbps(duration_s: float) -> int:
    """The video bitrate that lands the whole file at SIZE_TARGET for this length."""
    total_kbps = SIZE_TARGET * 8 / max(duration_s, 1.0) / 1000
    return int(max(total_kbps - AUDIO_KBPS, 600))


def render(src: Path, keep, speed, out: Path, loud: dict, crf: int = 18, on_progress=None, token: Token = None) -> Path:
    duration = sum(e - s_ for s_, e in keep) / max(speed, 0.01)
    maxrate = video_maxrate_kbps(duration)
    tracks = _is_tracks(src)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-progress", "pipe:1",
           *(src.inputs() if tracks else ["-i", str(src)]),
           "-filter_complex", _graph(keep, speed, loud, video=True, tracks=tracks),
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", str(crf), "-profile:v", "high",
           "-level", "4.2", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           # CRF quality up to a ceiling: the VBV cap is what keeps the file under SHARE_LIMIT.
           "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k",
           # A keyframe every two seconds (Instagram's own encodes do the same); x264's default is one per
           # 250 frames, 4.2s at 60fps. Every second was tried (2026-09-21) and doubled the file size.
           "-force_key_frames", "expr:gte(t,n_forced*2)",
           "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k", "-ar", "48000",
           str(out)]
    token = token or Token()
    # What ffmpeg says goes to a FILE, never a pipe. Progress is read from stdout to its end before
    # anything else is, so a pipe for stderr fills (64 KB) the moment ffmpeg has a lot to warn about,
    # ffmpeg stops to wait for a reader, and the render hangs for good: a take did exactly that on
    # Lambda and ran into the 15 minute limit with nothing written (2026-11-25).
    import tempfile
    with tempfile.TemporaryFile("w+", errors="replace") as errf:
        p = token.popen(cmd, stdout=subprocess.PIPE, stderr=errf, text=True)
        for line in p.stdout:
            if on_progress and line.startswith("out_time_us="):
                try:
                    on_progress(int(line.split("=")[1]) / 1e6)
                except ValueError:
                    pass
        code = p.wait()
        errf.seek(0)
        err = errf.read()
    token.check()
    if code != 0:
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


def measure_output_loudness(path: Path, token: Token = None) -> dict:
    r = (token or Token()).run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
                                "-af", "loudnorm=print_format=json", "-f", "null", "-"])
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", r.stderr, re.S)
    return json.loads(m.group(0)) if m else {}
