"""Redraw a game from its LOG, as a game track like any other.

On trial from 2026-11-26. The recorder normally films the game off the presenter's
screen, which a two-core laptop cannot do smoothly while it also records a camera.
With the recorder's ?game=log the game is not filmed: the game page keeps a log of
what is on its screen (rrweb: the page once, then every change to it, with times)
and of every sound it plays, and only the camera is recorded. This module turns that
log back into the game track the rest of the editor has always read: a video of the
game, 1080 wide, 30 frames a second, with the game's sound, starting at the moment
the camera started. Everything after it (the mix, the transcript, the cut, the
render) is unchanged and does not know the difference.

How:
  * The log is played in headless Chromium by rrweb's own player, in real time, and
    each frame Chromium draws is taken from its screencast with the time it was drawn.
    Real time because the game's own CSS animations run in the player as they ran in
    the game; stepping a paused player frame by frame would freeze them.
  * Frames are written at a steady 30 a second: each slot gets whichever frame was on
    screen at that moment, so a still screen costs nothing to capture and still fills
    its seconds.
  * The sound is made again, not recorded: the log names each sound as the game played
    it (a tone, or a sample at a rate and a volume, see _logSound in the game), and an
    OfflineAudioContext in the same browser plays them at their times into a wav. Same
    Web Audio as the game, so the same sound.
  * The page's fonts, images and sound files are fetched from the game's own site, which
    is why the browser runs with web security off: it opens nothing but this log.

The clock: the game stamps its events with Date.now() and the recorder put the moment
its camera started (cam_start_ms) on the same clock, so second 0 here is second 0 of
the camera track and the take's offset is 0.
"""
import asyncio
import base64
import gzip
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).parent / "replay"
WIDE = 1080          # the game's width in the finished video (compose.py)
FPS = 30
SETTLE_S = 2.0       # for the player's fonts and images to arrive before anything is filmed
# How much faster than real time the log is played. Measured 2026-11-26 on a 34s Twixtle log:
# at 2x every animation kept all its frames (the picture changes counted the same as at 1x);
# at 4x and 8x about half and a third were lost, because the screencast draws only so many
# frames a second, so shakes and slides went choppy. REPLAY_SPEED overrides it.
SPEED = 2.0

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>%s
html, body { margin: 0; padding: 0; background: #fff; overflow: hidden; }
.replayer-mouse-tail { display: none !important; }
.replayer-wrapper { position: absolute; top: 0; left: 0; }
</style></head><body></body></html>"""

# THE POINTER IS DRAWN (Jake, 2026-11-26: it shows what the presenter is talking about). The
# game logs where the mouse is (sampling.mousemove in armGameLog) and rrweb's player moves its
# own pointer element; this dresses that element as our arrow (replay/cursor.png, Jake's, filled
# white so it reads on the dark squares), sized for the finished video and with its tip on the
# point, plus a small pulse on each click. The player's own dot under the pointer is off.
CURSOR_PX = 54          # the arrow's height in the finished 1080-wide game
CURSOR_TIP = (0.066, 0.006)   # where the tip sits in cursor.png, as fractions of its size
CURSOR_CSS = """
.replayer-mouse { width: %(w).2fpx !important; height: %(h).2fpx !important; margin-left: %(mx).2fpx; margin-top: %(my).2fpx;
  background-image: url(data:image/png;base64,%(png)s) !important; background-size: 100%% 100%% !important;
  transition: left .04s linear, top .04s linear; z-index: 10; filter: drop-shadow(0 1px 1.5px rgba(0,0,0,.35)); }
.replayer-mouse:after { position: absolute; left: %(tx).2fpx; top: %(ty).2fpx; width: %(dot).2fpx; height: %(dot).2fpx;
  background: rgba(0,0,0,.28); opacity: 0; }
.replayer-mouse.active:after { animation: ps-click .3s ease-out 1; }
@keyframes ps-click { 0%% { opacity: .55; transform: translate(-50%%,-50%%) scale(.4); } 100%% { opacity: 0; transform: translate(-50%%,-50%%) scale(1.6); } }
"""


def _cursor_css(scale: float) -> str:
    h = CURSOR_PX / scale
    w = h * 60 / 96
    tx, ty = CURSOR_TIP[0] * w, CURSOR_TIP[1] * h
    png = base64.b64encode((HERE / "cursor.png").read_bytes()).decode()
    return CURSOR_CSS % {"w": w, "h": h, "mx": -tx, "my": -ty, "tx": tx, "ty": ty, "dot": 26 / scale, "png": png}

SOUND_JS = r"""
async ([sounds, seconds]) => {
  const rate = 48000, ctx = new OfflineAudioContext(1, Math.max(1, Math.ceil(seconds * rate)), rate);
  const bufs = {};
  for (const [, s] of sounds) {
    if (s.k !== 'sample' || s.u in bufs) continue;
    bufs[s.u] = null;
    try { bufs[s.u] = await ctx.decodeAudioData(await (await fetch(s.u)).arrayBuffer()); } catch (e) {}
  }
  // Vowelsweeper's voices run through its master: a gain (s.m) and a lowpass at 7500 Hz.
  // Its recorded stings ('el') are plain <audio> elements and bypass it, as in the game.
  let vsMaster = null;
  const master = (m) => {
    if (!vsMaster) {
      vsMaster = ctx.createGain(); vsMaster.gain.value = m == null ? 0.9 : m;
      const cap = ctx.createBiquadFilter(); cap.type = 'lowpass'; cap.frequency.value = 7500;
      vsMaster.connect(cap); cap.connect(ctx.destination);
    }
    return vsMaster;
  };
  const LEAD = 0.01;   // the game schedules each voice a buffer ahead (sfxAt)
  const noise = (dur) => {
    const len = Math.max(1, Math.floor(rate * dur)), buf = ctx.createBuffer(1, len, rate), d = buf.getChannelData(0);
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * (1 - i / len);
    return buf;
  };
  let grit = null;
  const gritCurve = () => { if (!grit) { grit = new Float32Array(512); for (let i = 0; i < 512; i++) grit[i] = Math.tanh((i * 2 / 512 - 1) * 5); } return grit; };
  for (const [, s] of sounds) {
    if (s.k !== 'el' || s.u in bufs) continue;
    bufs[s.u] = null;
    try { bufs[s.u] = await ctx.decodeAudioData(await (await fetch(s.u)).arrayBuffer()); } catch (e) {}
  }
  let made = 0;
  for (const [t, s] of sounds) {
    if (t < 0 || t > seconds) continue;
    if (s.k === 'blip') {
      const at = t + LEAD, osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = s.type; osc.frequency.setValueAtTime(s.f0, at); osc.frequency.exponentialRampToValueAtTime(s.f1, at + s.d);
      g.gain.setValueAtTime(0.0001, at); g.gain.exponentialRampToValueAtTime(s.p, at + 0.008); g.gain.exponentialRampToValueAtTime(0.0001, at + s.d);
      osc.connect(g); g.connect(master(s.m)); osc.start(at); osc.stop(at + s.d + 0.02); made++;
    } else if (s.k === 'note') {
      const at = t + LEAD + (s.delay || 0), osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = 'triangle'; osc.frequency.setValueAtTime(s.f, at);
      g.gain.setValueAtTime(0.0001, at); g.gain.exponentialRampToValueAtTime(s.p, at + 0.012); g.gain.exponentialRampToValueAtTime(0.0001, at + s.d);
      osc.connect(g); g.connect(master(s.m)); osc.start(at); osc.stop(at + s.d + 0.03); made++;
    } else if (s.k === 'tick') {
      const at = t + LEAD, src = ctx.createBufferSource(), lp = ctx.createBiquadFilter(), g = ctx.createGain();
      src.buffer = noise(s.d); lp.type = 'lowpass'; lp.frequency.value = s.cut;
      g.gain.setValueAtTime(s.np, at); g.gain.exponentialRampToValueAtTime(0.0001, at + s.d);
      src.connect(lp); lp.connect(g); g.connect(master(s.m)); src.start(at); src.stop(at + s.d + 0.01);
      if (s.bp) {
        const osc = ctx.createOscillator(), og = ctx.createGain(); osc.type = 'sine';
        osc.frequency.setValueAtTime(s.bf, at); osc.frequency.exponentialRampToValueAtTime(s.bf * 0.7, at + s.d);
        og.gain.setValueAtTime(0.0001, at); og.gain.exponentialRampToValueAtTime(s.bp, at + 0.006); og.gain.exponentialRampToValueAtTime(0.0001, at + s.d + 0.01);
        osc.connect(og); og.connect(master(s.m)); osc.start(at); osc.stop(at + s.d + 0.04);
      }
      made++;
    } else if (s.k === 'boom') {
      const at = t + LEAD + s.t0, osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = 'sine'; osc.frequency.setValueAtTime(s.f0, at); osc.frequency.exponentialRampToValueAtTime(s.f0 * 0.28, at + s.d);
      g.gain.setValueAtTime(0.0001, at); g.gain.exponentialRampToValueAtTime(s.bp, at + 0.014); g.gain.exponentialRampToValueAtTime(0.0001, at + s.d + 0.03);
      osc.connect(g); g.connect(master(s.m)); osc.start(at); osc.stop(at + s.d + 0.05);
      const src = ctx.createBufferSource(), sh = ctx.createWaveShaper(), lp = ctx.createBiquadFilter(), ng = ctx.createGain();
      src.buffer = noise(s.d); sh.curve = gritCurve(); sh.oversample = '2x';
      lp.type = 'lowpass'; lp.frequency.setValueAtTime(s.cut, at); lp.frequency.exponentialRampToValueAtTime(320, at + s.d);
      ng.gain.setValueAtTime(s.np, at); ng.gain.exponentialRampToValueAtTime(0.0001, at + s.d);
      src.connect(sh); sh.connect(lp); lp.connect(ng); ng.connect(master(s.m)); src.start(at); src.stop(at + s.d + 0.02); made++;
    } else if (s.k === 'el') {
      const b = bufs[s.u]; if (!b) continue;
      const src = ctx.createBufferSource(), g = ctx.createGain();
      src.buffer = b; g.gain.value = s.v == null ? 1 : s.v;
      src.connect(g); g.connect(ctx.destination); src.start(t); made++;
    } else if (s.k === 'tone') {
      const t0 = t + (s.delay || 0);
      if (t0 < 0 || t0 > seconds) continue;
      const osc = ctx.createOscillator(), g = ctx.createGain();
      osc.type = s.type || 'sine';
      osc.frequency.setValueAtTime(s.f, t0);
      if (s.ef) osc.frequency.exponentialRampToValueAtTime(s.ef, t0 + s.d);
      g.gain.setValueAtTime(s.vol, t0);
      g.gain.exponentialRampToValueAtTime(0.001, t0 + s.d);
      osc.connect(g); g.connect(ctx.destination);
      osc.start(t0); osc.stop(t0 + s.d + 0.02); made++;
    } else if (s.k === 'sample') {
      const b = bufs[s.u];
      if (!b || t < 0 || t > seconds) continue;
      const src = ctx.createBufferSource(), g = ctx.createGain();
      src.buffer = b;
      if (s.det) src.detune.value = s.det; else src.playbackRate.value = s.r || 1;
      g.gain.value = s.v == null ? 0.5 : s.v;
      src.connect(g); g.connect(ctx.destination);
      src.start(t); made++;
    }
  }
  const out = (await ctx.startRendering()).getChannelData(0);
  const pcm = new Int16Array(out.length);
  for (let i = 0; i < out.length; i++) pcm[i] = Math.max(-32768, Math.min(32767, Math.round(out[i] * 32767)));
  const bytes = new Uint8Array(pcm.buffer);
  let bin = '';
  for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return { pcm: btoa(bin), made, missing: Object.keys(bufs).filter(u => !bufs[u]) };
}
"""


def load(path: Path) -> dict:
    raw = Path(path).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    log = json.loads(raw)
    if not isinstance(log, dict) or not log.get("events") or not log.get("cam_start_ms"):
        raise ValueError("the game log is empty or has no camera start")
    return log


def _wav(path: Path, pcm: bytes, rate=48000):
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)


def render(log_path: Path, out_path: Path, seconds: float, speed: float = None) -> dict:
    """The game track for `seconds` of camera, from the log at log_path, into out_path (mp4)."""
    speed = float(speed or os.environ.get("REPLAY_SPEED") or SPEED)
    return asyncio.run(_render(Path(log_path), Path(out_path), float(seconds), speed))


async def _render(log_path: Path, out_path: Path, seconds: float, speed: float = 1.0) -> dict:
    from playwright.async_api import async_playwright
    log = load(log_path)
    events = sorted(log["events"], key=lambda e: e["timestamp"])
    cam0 = int(log["cam_start_ms"])
    meta = next((e for e in events if e.get("type") == 4), None)
    if meta is None:
        raise ValueError("the game log has no page size")
    w, h = int(meta["data"]["width"]), int(meta["data"]["height"])
    scale = WIDE / w
    first = events[0]["timestamp"]
    start_offset = max(0, cam0 - first)          # into the log, where the camera started
    lead_in = max(0, first - cam0) / 1000.0     # the camera started before the log: hold its first frame
    sounds = [[(e["timestamp"] - cam0) / 1000.0, e["data"]["payload"]] for e in events
              if e.get("type") == 5 and (e.get("data") or {}).get("tag") == "sound"]
    # the game marks the moment it was solved, so the editor need not listen for the chime
    solved = [(e["timestamp"] - cam0) / 1000.0 for e in events
              if e.get("type") == 5 and (e.get("data") or {}).get("tag") == "solved"]
    solved_at = round(solved[0], 2) if solved and 0 <= solved[0] <= seconds else None
    css = (HERE / "rrweb.css").read_text()
    t_all = time.time()

    async with async_playwright() as p:
        args = ["--disable-web-security", "--no-sandbox", "--disable-dev-shm-usage", "--hide-scrollbars",
                "--force-color-profile=srgb", "--autoplay-policy=no-user-gesture-required"]
        if os.environ.get("AWS_LAMBDA_FUNCTION_NAME"):   # Lambda: no /dev/shm, no zygote, one process, home in /tmp
            args += ["--no-zygote", "--single-process", "--disable-gpu"]
            os.environ.setdefault("HOME", "/tmp")
        browser = await p.chromium.launch(args=args)
        # drawn at the finished size, not scaled up after: the page is laid out at its own size and the
        # player zoomed, so text is drawn sharp at 1080 (the screencast ignores a device scale)
        W, H = round(w * scale), round(h * scale)
        ctx = await browser.new_context(viewport={"width": W, "height": H}, device_scale_factor=1)
        page = await ctx.new_page()
        await page.set_content(PAGE % (css + _cursor_css(scale)))
        await page.add_script_tag(path=str(HERE / "rrweb.umd.min.cjs"))

        # the sound first, all at once: it does not need real time
        t_sound = time.time()
        snd = await page.evaluate(SOUND_JS, [sounds, seconds])
        wav = out_path.with_suffix(".wav")
        _wav(wav, base64.b64decode(snd["pcm"]))
        t_sound = time.time() - t_sound

        await page.evaluate("""(ev) => {
          window.R = new rrweb.Replayer(ev, { root: document.body, mouseTail: false, showWarning: false,
                                              UNSAFE_replayCanvas: true,   /* Vowelsweeper's confetti is a canvas, logged as pictures */
                                              skipInactive: false, triggerFocus: false, speed: %s });
          window.R.wrapper.style.transform = 'scale(%s)';
          window.R.wrapper.style.transformOrigin = '0 0';
        }""" % (speed, scale), events)
        await page.evaluate("off => window.R.pause(off)", start_offset)
        await page.wait_for_timeout(int(SETTLE_S * 1000))

        # FRAMES ARE ONLY SAVED WHILE THE PLAYER RUNS, never encoded. Encoding as they arrived meant
        # that after a long still stretch the handler wrote every held frame at once (46 seconds of a
        # Vowelsweeper take is 1,380 of them) and the screencast's next frames were dropped meanwhile:
        # the double taps that followed never appeared (Jake's first take, 2026-11-26). So each frame
        # goes to a file with its time, and ffmpeg lays them on a steady 30 a second at the end.
        import tempfile
        fdir = Path(tempfile.mkdtemp(prefix="replay-", dir=str(out_path.parent)))
        shots = []                      # (video seconds, path); the still before play stands at 0
        state = {"t0": None, "got": 0}
        cdp = await ctx.new_cdp_session(page)

        def on_frame(params):
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))
            ts = params["metadata"]["timestamp"]
            path = fdir / f"{state['got']:06d}.jpg"
            path.write_bytes(base64.b64decode(params["data"]))
            state["got"] += 1
            at = 0.0 if state["t0"] is None else (ts - state["t0"]) * speed + lead_in
            if state["t0"] is None and shots:
                shots[-1] = (0.0, path)   # only the latest still before play counts
            else:
                shots.append((at, path))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 92, "everyNthFrame": 1,
                                                  "maxWidth": W, "maxHeight": H})
        await page.wait_for_timeout(300)                     # the paused still arrives
        # second 0 of the video is when the player starts, less the lead-in where the log had not begun
        # faster than real time: the player's clock AND every CSS animation in the page run at `speed`,
        # and a frame drawn s seconds after play stands for s * speed seconds of the game
        await cdp.send("Animation.enable")
        await cdp.send("Animation.setPlaybackRate", {"playbackRate": speed})
        state["t0"] = await page.evaluate("() => { window.R.play(%d); return (performance.timeOrigin + performance.now()) / 1000; }" % start_offset)
        await page.wait_for_timeout(int((seconds - lead_in) / speed * 1000) + 500)
        await cdp.send("Page.stopScreencast")
        await browser.close()

    # one frame per 30th of a second, each slot showing the newest frame drawn by then
    total = int(round(seconds * FPS))
    slots = {}
    for at, path in sorted(shots, key=lambda x: x[0]):
        slots[max(0, min(total - 1, int(round(at * FPS))))] = path
    order = sorted(slots)
    if not order:
        raise RuntimeError("the game could not be redrawn from its log: no picture was drawn")
    lst = fdir / "frames.txt"
    with open(lst, "w") as f:
        for i, n in enumerate(order):
            nxt = order[i + 1] if i + 1 < len(order) else total
            f.write(f"file '{slots[n]}'\nduration {(nxt - n) / FPS:.6f}\n")
        f.write(f"file '{slots[order[-1]]}'\n")
    r = subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", str(lst), "-i", str(wav), "-map", "0:v", "-map", "1:a",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p", "-fps_mode", "cfr", "-r", str(FPS),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
        "-c:a", "aac", "-b:a", "192k", "-t", f"{seconds:.3f}", str(out_path)])
    ok = r.returncode == 0
    import shutil
    shutil.rmtree(fdir, ignore_errors=True)
    wav.unlink(missing_ok=True)
    if not ok or not out_path.exists():
        raise RuntimeError("the game could not be redrawn from its log")
    return {"frames_drawn": state["got"], "frames_written": total, "seconds": round(time.time() - t_all, 1),
            "sound_seconds": round(t_sound, 1), "sounds": snd["made"], "sounds_missing": snd["missing"],
            "size": [w, h], "scale": round(scale, 3), "speed": speed, "lead_in": round(lead_in, 3), "solved_at": solved_at}


if __name__ == "__main__":
    import sys
    print(json.dumps(render(Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]))))
