"""A player's take, made twice: the whole of it, and a STORY about 30 seconds long (Jake, 2026-11-27).

Players post these to their Instagram stories, where 15 to 30 seconds is the length people watch. Both
come out of the finished whole video (out.mp4), and both end on the same CARD: the Twixtle name, the day
and the time the game showed, over the last frame held still, so every friend who watches knows what it
is and where to find it.

The story is made in two steps, in this order:
  1. CUT THE THINKING. A stretch counts as something happening if the player is talking OR doing
     something on the board (every tap and key the game's log recorded, and the game's own sounds,
     which are in the video's sound). Only stretches where both are quiet go, and even those keep their
     first moment, so the "hmm" before the next move is still there. The voice alone was not enough: a
     player who never speaks would have had the whole solve cut.
  2. SPEED UP WHAT IS LEFT until it fits, up to MAX_SPEED. Voices keep their pitch (atempo: quicker,
     not higher). The last TAIL_S before the solve play at normal speed, so the solve itself is seen.
A take that is short enough already is not sped up at all.
"""
import json
import subprocess
from pathlib import Path

from . import render, transcribe

TARGET_S = 30.0      # the story's whole length, card included
CARD_S = 3.0         # the end card
TAIL_S = 2.0         # up to and just past the solve, at normal speed
AFTER_SOLVE_S = 1.0  # how long the story runs on after the solve before the card
MAX_SPEED = 6.0      # past this even the board is a blur: a very long solve runs a little over instead
LEAD_S = 1.0         # before the first move
MOVE_PAD = (0.5, 0.7)   # around each tap or key, before and after
SPEECH_PAD = 0.2
THINK_KEEP_S = 0.6   # what is left of a long quiet stretch
JOIN_S = 1.2         # quiet shorter than this is kept whole


def _merge(spans):
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return out


def to_out_time(t, keep):
    """A moment on the camera's clock, on the finished video's, which is the camera's less the
    stretches the cut took out (the reload gaps). None when it fell inside one of those."""
    if t is None:
        return None
    pos = 0.0
    for s, e in keep:
        if t < s:
            return None
        if t <= e:
            return pos + (t - s)
        pos += e - s
    return None


def plan(duration, moves, speech, solve_at, first_move_at):
    """[(start, end, speed), ...] on the finished video's clock, and the speed chosen."""
    start = max(0.0, (first_move_at if first_move_at is not None else 0.0) - LEAD_S)
    end = min(duration, solve_at + AFTER_SOLVE_S) if solve_at is not None else duration
    if end - start <= 0.5:
        start, end = 0.0, duration
    busy = [[t - MOVE_PAD[0], t + MOVE_PAD[1]] for t in moves] + [[a - SPEECH_PAD, b + SPEECH_PAD] for a, b in speech]
    busy = [[max(a, start), min(b, end)] for a, b in busy if b > start and a < end]
    tail0 = max(start, end - TAIL_S)
    busy.append([tail0, end])
    spans = _merge(busy) or [[start, end]]
    if spans[0][0] - start < JOIN_S:
        spans[0][0] = start
    kept = []
    for a, b in spans:
        if kept and a - kept[-1][1] < JOIN_S:
            kept[-1][1] = b                           # a short quiet: kept whole
        else:
            if kept:
                kept[-1][1] += THINK_KEEP_S           # a long quiet: its first moment only
            kept.append([a, b])
    # the tail is its own piece at normal speed
    body, tail = [], []
    for a, b in kept:
        if b <= tail0:
            body.append([a, b])
        elif a >= tail0:
            tail.append([a, b])
        else:
            body.append([a, tail0]); tail.append([tail0, b])
    body_len = sum(b - a for a, b in body)
    tail_len = sum(b - a for a, b in tail)
    room = TARGET_S - CARD_S - tail_len
    speed = 1.0 if body_len <= room else min(MAX_SPEED, body_len / max(room, 1.0))
    segs = [(a, b, speed) for a, b in body if b - a > 0.05] + [(a, b, 1.0) for a, b in tail if b - a > 0.05]
    return segs, round(speed, 2)


def _atempo(speed):
    parts, s = [], speed
    while s > 2.0:
        parts.append("atempo=2.0"); s /= 2.0
    parts.append(f"atempo={s:.5f}")
    return ",".join(parts)


def make_video(src: Path, segs, card_png: Path, out: Path, preset="veryfast"):
    """The pieces of src at their speeds, one after another, then the card over the last frame held still."""
    body = sum((b - a) / sp for a, b, sp in segs)
    g = []
    for i, (a, b, sp) in enumerate(segs):
        g.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{sp:.5f}[v{i}]")
        g.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,{_atempo(sp)}[a{i}]")
    n = len(segs)
    g.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    g.append(f"[vc]fps=30,tpad=stop_mode=clone:stop_duration={CARD_S}[vh]")
    g.append(f"[1:v]format=rgba,fade=in:st={body:.3f}:d=0.35:alpha=1[card]")
    g.append(f"[vh][card]overlay=0:0:enable='gte(t,{body:.3f})',scale=out_range=tv:out_color_matrix=bt709,format=yuv420p[vout]")
    g.append(f"[ac]apad=pad_dur={CARD_S}[aout]")
    total = body + CARD_S
    maxrate = render.video_maxrate_kbps(total)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-v", "error", "-i", str(src),
           "-loop", "1", "-t", f"{total:.3f}", "-i", str(card_png),
           "-filter_complex", ";".join(g), "-map", "[vout]", "-map", "[aout]", "-t", f"{total:.3f}",
           "-color_range", "tv", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
           "-c:v", "libx264", "-preset", preset, "-crf", "18", "-profile:v", "high", "-level", "4.2",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart",
           "-maxrate", f"{maxrate}k", "-bufsize", f"{maxrate * 2}k", "-force_key_frames", "expr:gte(t,n_forced*2)",
           "-c:a", "aac", "-b:a", f"{render.AUDIO_KBPS}k", "-ar", "48000", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("the story could not be made:\n" + r.stderr[-2000:])
    return round(total, 2)


CARD_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0;width:1080px;height:1920px;background:transparent}
.dim{position:absolute;inset:0;background:rgba(10,10,12,.86)}
.box{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;
     font-family:-apple-system,'Helvetica Neue',Arial,sans-serif;color:#fff;text-align:center}
.name{display:flex;align-items:flex-start;color:#fff}.name svg{width:720px;height:auto;display:block}
.name sup{font-size:44px;font-weight:600;margin:-6px 0 0 8px}
.day{font-size:56px;font-weight:600;margin-top:40px;opacity:.85}
.time{font-size:96px;font-weight:800;margin-top:70px;padding:22px 56px;border-radius:999px;background:#009E73}
.site{font-size:50px;font-weight:600;margin-top:90px;opacity:.9}
</style></head><body><div class="dim"></div><div class="box">
<div class="name">{logo}<sup>&reg;</sup></div><div class="day">{day}</div>{time}<div class="site">twixtle.games</div>
</div></body></html>"""


def card(out: Path, date: str = None, solve_ms=None):
    """The end card as a picture, drawn by the same Chromium that redraws the game (replay.py)."""
    import datetime
    day = ""
    if date:
        try:
            d = datetime.date.fromisoformat(date)
            day = d.strftime("%A ") + str(d.day) + d.strftime(" %B")
        except ValueError:
            pass
    time_html = ""
    if solve_ms:
        s = int(round(float(solve_ms) / 1000))
        time_html = f'<div class="time">Solved in {s // 60}:{s % 60:02d}</div>'
    logo = (Path(__file__).with_name("twixtle-wordmark.svg")).read_text()   # the game's own wordmark, as its adverts draw it
    html = CARD_HTML.replace("{logo}", logo).replace("{day}", day).replace("{time}", time_html)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        import os
        args = ["--no-sandbox", "--disable-dev-shm-usage", "--force-color-profile=srgb"]
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):   # as replay.py launches it there
            args += ["--no-zygote", "--single-process", "--disable-gpu"]
        b = p.chromium.launch(args=args)
        pg = b.new_page(viewport={"width": 1080, "height": 1920})
        pg.set_content(html)
        pg.screenshot(path=str(out), omit_background=True)
        b.close()
    return out


def make(job_dir: Path, replayed: dict, date: str = None) -> dict:
    """out-story.mp4 beside out.mp4, and out.mp4 again with the card on its end. What was done, for the log."""
    job_dir = Path(job_dir)
    full = job_dir / "out.mp4"
    cut = json.loads((job_dir / "cut.json").read_text())
    keep = cut["keep"]
    duration = render.probe(full)["duration"]
    png = card(job_dir / "card.png", date, (replayed or {}).get("solve_ms"))
    moves = [m for m in (to_out_time(t, keep) for t in (replayed or {}).get("moves") or []) if m is not None]
    wav = job_dir / "story.wav"
    transcribe.extract_audio(full, wav)
    from .pipeline import _speech_stretches   # the take's own sound: the voice and the game's, both count
    speech = [[w["s"], w["e"]] for w in _speech_stretches(wav)]
    wav.unlink(missing_ok=True)
    solve = to_out_time((replayed or {}).get("solved_at"), keep)
    first = to_out_time((replayed or {}).get("first_move_at"), keep)
    segs, speed = plan(duration, moves, speech, solve, first)
    from . import progress
    progress.report("story", 0.89)
    story_len = make_video(full, segs, png, job_dir / "out-story.mp4")
    progress.report("story", 0.94)
    tmp = job_dir / "out.card.mp4"
    full_len = make_video(full, [(0.0, duration, 1.0)], png, tmp)
    tmp.replace(full)
    info = {"story_s": story_len, "speed": speed, "pieces": len(segs), "full_s": full_len,
            "kept_s": round(sum(b - a for a, b, _ in segs), 2), "moves": len(moves), "solve_ms": (replayed or {}).get("solve_ms")}
    (job_dir / "story.json").write_text(json.dumps(info))
    return info
