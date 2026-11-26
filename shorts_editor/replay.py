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
# THE BROWSER REACHES THE GAMES' OWN SITES AND NOTHING ELSE (2026-11-26). The log comes from a
# presenter's browser and presenters are untrusted, so a crafted one could otherwise make this
# browser, which runs with web security off inside a function holding the model's key and the
# bucket's role, fetch anything it names. Every request is checked against these origins (plus
# data: and blob:, which go nowhere) and anything else is refused before it leaves. A local
# test of a log recorded on a dev server adds its address with REPLAY_ALLOWED_ORIGINS.
ALLOWED_ORIGINS = ("https://twixtle.games", "https://vowelsweeper.games",
                   "https://fonts.googleapis.com", "https://fonts.gstatic.com")   # Twixtle's own type comes from Google Fonts


def _allowed(url: str) -> bool:
    if url.startswith(("data:", "blob:", "about:")):
        return True
    extra = tuple(o.strip().rstrip("/") for o in os.environ.get("REPLAY_ALLOWED_ORIGINS", "").split(",") if o.strip())
    return any(url == o or url.startswith(o + "/") for o in ALLOWED_ORIGINS + extra)


# Each game's layout width in the presenters' recorder (record.js, LAYOUT_WIDTH), and the video's
# game panel (compose.py: 1080 by 1920 less the camera's 762).
LAYOUT_WIDTH = {"twixtle": 750, "vowelsweeper": 530}
# A PLAYER'S OWN RECORDING (REPLAY_PROFILE=player, set for selfrec takes) is laid out as the
# presenters' is: at the game's LAYOUT_WIDTH, so Twixtle's 460px column has the page's margins
# either side, with its keyboard. For an hour on 2026-11-26 it was drawn 460 wide with no keyboard,
# so the board filled the panel; Jake put both back the same evening. A width here overrides
# LAYOUT_WIDTH for players' takes only, should that be wanted again.
PLAYER_LAYOUT_WIDTH = {}
PLAYER_HIDE = {"twixtle": [".countdown-strip"]}
GAME_PANEL = (1080, 1158)

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
# own pointer element; this dresses that element as Jake's arrow (replay/cursor.png, his black
# outline filled white so it reads on the dark squares), sized for the finished video with its rounded tip
# on the point. A click fires a little burst of dashes from the tip (PAGE_JS), his reference.
CURSOR_PX = 88          # the arrow's height in the finished 1080-wide game (1.5x the first arrow, Jake)
# Vowelsweeper only (Jake): pointing at a square is what its talk is about; Twixtle is typed.
POINTER_GAMES = {"vowelsweeper"}
CURSOR_SIZE = (176, 208)      # cursor.png
CURSOR_TIP = (0.082, 0.046)   # where the tip sits in cursor.png, as fractions of its size
CURSOR_CSS = """
.replayer-mouse { width: %(w).2fpx !important; height: %(h).2fpx !important; margin-left: %(mx).2fpx; margin-top: %(my).2fpx;
  background-image: url(data:image/png;base64,%(png)s) !important; background-size: 100%% 100%% !important;
  transition: left .04s linear, top .04s linear; z-index: 10; filter: drop-shadow(0 1px 2px rgba(0,0,0,.3)); }
.replayer-mouse:after { display: none !important; }
.ps-burst { position: absolute; z-index: 11; pointer-events: none; width: 0; height: 0; }
.ps-burst i { position: absolute; left: 0; top: 0; width: %(dl).2fpx; height: %(dw).2fpx; margin-top: %(dm).2fpx;
  border-radius: %(dw).2fpx; background: #111; transform-origin: 0 50%%; opacity: 0;
  animation: ps-dash .42s ease-out 1 forwards; }
@keyframes ps-dash {
  0%%   { opacity: 0; transform: rotate(var(--a)) translateX(%(r0).2fpx) scaleX(.3); }
  25%%  { opacity: 1; transform: rotate(var(--a)) translateX(%(r1).2fpx) scaleX(1); }
  100%% { opacity: 0; transform: rotate(var(--a)) translateX(%(r2).2fpx) scaleX(.6); } }
.ps-confetti { position: absolute; left: 0; top: 0; z-index: 9; pointer-events: none; }
"""


def _cursor_css(scale: float, pointer: bool = True) -> str:
    if not pointer:
        return ".replayer-mouse { display: none !important; }\n.ps-confetti { position: absolute; left: 0; top: 0; z-index: 9; pointer-events: none; }"
    h = CURSOR_PX / scale
    w = h * CURSOR_SIZE[0] / CURSOR_SIZE[1]
    tx, ty = CURSOR_TIP[0] * w, CURSOR_TIP[1] * h
    png = base64.b64encode((HERE / "cursor.png").read_bytes()).decode()
    return CURSOR_CSS % {"w": w, "h": h, "mx": -tx, "my": -ty, "png": png,
                         "dl": h * 0.26, "dw": h * 0.075, "dm": -h * 0.0375, "r0": h * 0.12, "r1": h * 0.2, "r2": h * 0.42}


# Run in the player's page once it exists: the click burst, and Vowelsweeper's confetti, which the
# game logs as the burst's starting pieces (a 'confetti' custom event) rather than as pictures of
# its canvas. Pictures came at 15 a second and looked laggy (Jake); this is the game's own physics
# (fireConfetti in its index.php: same gravity, drag and pieces), drawn on every frame here, on
# the player's clock, so it is as smooth as the game was and costs the presenter's laptop nothing.
PAGE_JS = r"""
([speed, pointer]) => {
  const R = window.R, wrap = R.wrapper;
  // the burst: five dashes out from the tip, as in Jake's reference (up, up-right, and round to down-left)
  const ANGLES = [-90, -45, -135, 180, 135];
  // On each PRESS (a mouse interaction of type MouseDown or TouchStart), where it happened. Not the
  // player's own click flag: Vowelsweeper's taps log as press and release with no click at all.
  if (pointer) R.on('event-cast', (e) => {
    if (!e || e.type !== 3 || !e.data || e.data.source !== 2 || (e.data.type !== 1 && e.data.type !== 7)) return;
    const b = document.createElement('div'); b.className = 'ps-burst';
    b.style.left = e.data.x + 'px'; b.style.top = e.data.y + 'px';
    for (const a of ANGLES) { const i = document.createElement('i'); i.style.setProperty('--a', a + 'deg'); b.appendChild(i); }
    wrap.appendChild(b);
    setTimeout(() => b.remove(), 1000 / speed);
  });
  R.on('custom-event', (e) => {
    if (!e || !e.data || e.data.tag !== 'confetti') return;
    const p = e.data.payload, scale = window.SCALE;
    const cv = document.createElement('canvas'); cv.className = 'ps-confetti';
    cv.style.width = p.w + 'px'; cv.style.height = p.h + 'px';
    cv.width = Math.round(p.w * scale); cv.height = Math.round(p.h * scale);
    wrap.appendChild(cv);
    const ctx = cv.getContext('2d'); ctx.setTransform(scale, 0, 0, scale, 0, 0);
    const bits = p.bits.map(b => Object.assign({}, b));
    let last = null, elapsed = 0;
    const step = (ts) => {
      if (last === null) last = ts;
      let dt = (ts - last) / 1000 * speed; last = ts;
      if (dt > 0.05 * speed) dt = 0.05 * speed;
      elapsed += dt;
      ctx.clearRect(0, 0, p.w, p.h);
      let alive = 0;
      for (const b of bits) {
        b.vy += p.g * dt; b.vx *= Math.pow(p.drag, dt * 60);
        b.x += b.vx * dt; b.y += b.vy * dt; b.rot += b.spin * dt;
        if (b.y < p.h + 40) alive++;
        ctx.save(); ctx.translate(b.x, b.y); ctx.rotate(b.rot); ctx.fillStyle = b.colour;
        ctx.fillRect(-b.size / 2, -b.size / 2, b.size, b.size * 0.6); ctx.restore();
      }
      if (alive > 0 && elapsed < p.runFor) requestAnimationFrame(step); else cv.remove();
    };
    requestAnimationFrame(step);
  });
}
"""

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


def _control_ids(events) -> set:
    """rrweb ids of the recording's controls (.ps-rec-ui) and everything inside them, from every
    snapshot of the page and every node added to it."""
    ids = set()

    def walk(node, inside=False):
        if not isinstance(node, dict):
            return
        cls = str((node.get("attributes") or {}).get("class") or "")
        inside = inside or "ps-rec-ui" in cls.split()
        if inside and node.get("id") is not None:
            ids.add(node["id"])
        for c in node.get("childNodes") or []:
            walk(c, inside)
    for e in events:
        d = e.get("data") or {}
        if e.get("type") == 2:
            walk(d.get("node"))
        elif e.get("type") == 3 and d.get("source") == 0:
            for a in d.get("adds") or []:
                walk(a.get("node"), a.get("parentId") in ids)
    return ids


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
    # A PLAYER'S PHONE IS LAID OUT AGAIN AT THE PRESENTERS' SIZE (2026-11-26). The log is the page,
    # not a picture of it, so it can be laid out at any size: a phone's tall narrow screen, fitted
    # into the video's game panel, left wide empty columns either side (Jake's first phone take).
    # The presenters' recorder lays each game out at its own width, at the panel's shape; a log
    # recorded at another size is replayed at that one, and the page reflows to it as it would
    # have on a laptop. Pointer positions would no longer match, which is why it is not done to a
    # log already within a tenth of that size: those are the presenters' own.
    player = os.environ.get("REPLAY_PROFILE") == "player"
    lw = (PLAYER_LAYOUT_WIDTH if player else {}).get(log.get("game") or "") or LAYOUT_WIDTH.get(log.get("game") or "")
    if lw:
        lh = round(lw * GAME_PANEL[1] / GAME_PANEL[0])
        if abs(w - lw) > lw * 0.1 or abs(h - lh) > lh * 0.1:
            for e in events:
                if e.get("type") == 4 or (e.get("type") == 3 and (e.get("data") or {}).get("source") == 4):
                    e["data"]["width"], e["data"]["height"] = lw, lh
            w, h = lw, lh
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
    # the player's first move after the camera started: a tap, a click, a typed letter or a key sound,
    # which is where a player's quick cut starts (pipeline.py, quick)
    # A tap on the recording's own controls (Stop, above all) is not a move: a take with no move before
    # Stop otherwise started its cut at the Stop tap and came out a second long (Jake, 2026-11-26).
    controls = _control_ids(events)
    moves = [(e["timestamp"] - cam0) / 1000.0 for e in events
             if e["timestamp"] >= cam0 and ((e.get("type") == 3 and (e.get("data") or {}).get("source") in (2, 5, 6)
                                             and (e.get("data") or {}).get("id") not in controls)
                                            or (e.get("type") == 5 and (e.get("data") or {}).get("tag") == "sound"))]
    first_move_at = round(moves[0], 2) if moves else None
    css = (HERE / "rrweb.css").read_text()
    hide = [sel + " { display: none !important; }" for sel in
            [".ps-rec-ui", ".app-footer"] + (PLAYER_HIDE.get(log.get("game") or "", []) if player else [])]
    pointer = log.get("game") in POINTER_GAMES
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
        refused = []

        async def guard(route):
            if _allowed(route.request.url):
                await route.continue_()
            else:
                refused.append(route.request.url[:120])
                await route.abort()
        await ctx.route("**/*", guard)
        page = await ctx.new_page()
        await page.set_content(PAGE % (css + _cursor_css(scale, pointer)))
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
                                              /* a player's own recording controls (the game's record mode) are in the log, being on the
                                                 page; the video is the game, so they are hidden in the redraw */
                                              /* and the site footer, which a presenter's recording never shows (Twixtle's
                                                 body.recording hides it) and a player's own does, on screen */
                                              insertStyleRules: %s,
                                              skipInactive: false, triggerFocus: false, speed: %s });
          window.R.wrapper.style.transform = 'scale(%s)';
          window.R.wrapper.style.transformOrigin = '0 0';
        }""" % (json.dumps(hide), speed, scale), events)
        await page.evaluate("s => { window.SCALE = s; }", scale)
        await page.evaluate(PAGE_JS, [speed, pointer])
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
        from . import progress
        wait_s = (seconds - lead_in) / speed + 0.5
        t_play = time.time()
        while time.time() - t_play < wait_s:   # waited in steps, so a player's phone can show how far it has got
            await page.wait_for_timeout(int(min(1.0, wait_s - (time.time() - t_play)) * 1000) + 1)
            progress.report("draw", 0.65 * min(1.0, (time.time() - t_play) / wait_s))
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
    # exactly `total` frames, piped in order now that capture is over (the concat demuxer, tried
    # first, came out 0.8 s short on a Twixtle take, and the editor stops at the shorter track)
    ff = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
        "-i", str(wav), "-map", "0:v", "-map", "1:a",
        "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "192k", "-t", f"{seconds:.3f}", str(out_path)], stdin=subprocess.PIPE)
    cur, k = None, 0
    try:
        for n in range(total):
            while k < len(order) and order[k] <= n:
                cur = slots[order[k]].read_bytes(); k += 1
            ff.stdin.write(cur)
        ff.stdin.close()
    except BrokenPipeError:
        pass
    r = ff
    r.returncode = ff.wait()
    ok = r.returncode == 0
    import shutil
    shutil.rmtree(fdir, ignore_errors=True)
    wav.unlink(missing_ok=True)
    if not ok or not out_path.exists():
        raise RuntimeError("the game could not be redrawn from its log")
    return {"frames_drawn": state["got"], "frames_written": total, "seconds": round(time.time() - t_all, 1),
            "sound_seconds": round(t_sound, 1), "sounds": snd["made"], "sounds_missing": snd["missing"],
            "size": [w, h], "scale": round(scale, 3), "speed": speed, "pointer": pointer, "refused": refused[:10], "lead_in": round(lead_in, 3), "solved_at": solved_at, "first_move_at": first_move_at}


if __name__ == "__main__":
    import sys
    print(json.dumps(render(Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3]))))
