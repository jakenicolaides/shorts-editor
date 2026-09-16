"""Job orchestration. One directory per job under work/<id>/ holding:
  input.<ext>      the raw clip (copied in)
  audio.wav        mono 16k
  transcript.json  whisper words
  params.json      current cut parameters
  cut.json         current keep list + durations + flags
  out.mp4          current render
  sidecar.json     everything the review page shows
  status.json      stage / progress / log for the UI
  history.json     reviewer notes and editor replies
"""
import json
import os
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import transcribe, cutlist, solvetone, render

ROOT = Path(__file__).resolve().parent.parent
WORK = ROOT / "work"


def _read(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _write(p, data):
    Path(p).write_text(json.dumps(data, indent=1))


class Job:
    def __init__(self, job_id: str):
        self.id = job_id
        self.dir = WORK / job_id
        self.dir.mkdir(parents=True, exist_ok=True)

    # ---- status -------------------------------------------------------------
    def status(self):
        return _read(self.dir / "status.json", {"stage": "new", "progress": 0, "log": []})

    def set(self, stage=None, progress=None, msg=None, error=None, **extra):
        st = self.status()
        if stage is not None:
            st["stage"] = stage
            if stage != "failed":
                st.pop("error", None)
        if progress is not None:
            st["progress"] = progress
        if msg:
            st.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')} {msg}")
        if error is not None:
            st["error"] = error
        st.update(extra)
        st["updated"] = time.time()
        _write(self.dir / "status.json", st)

    @property
    def input(self):
        for p in self.dir.glob("input.*"):
            return p
        return None

    @property
    def meta(self):
        return _read(self.dir / "meta.json", {})

    def _write_meta(self, meta):
        _write(self.dir / "meta.json", meta)

    # ---- steps --------------------------------------------------------------
    @classmethod
    def create(cls, src: Path, speed: float, game: str, name: str = None, title: str = None) -> "Job":
        job_id = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        job = cls(job_id)
        dst = job.dir / ("input" + src.suffix.lower())
        shutil.copy2(src, dst)
        _write(job.dir / "meta.json", {
            "name": name or src.name, "title": (title or "").strip(), "game": game, "speed": speed, "created": time.time(),
        })
        p = cutlist.Params(speed=speed)
        _write(job.dir / "params.json", p.to_dict())
        _write(job.dir / "history.json", [])
        job.set(stage="queued", progress=0, msg=f"queued {src.name} (speed {speed}, game {game})")
        return job

    def run(self):
        try:
            self._run()
        except Exception as e:  # surfaced on the UI
            self.set(stage="failed", error=str(e), msg="failed: " + str(e))
            (self.dir / "traceback.txt").write_text(traceback.format_exc())

    def _run(self):
        src = self.input
        info = render.probe(src)
        _write(self.dir / "probe.json", info)
        self.set(stage="audio", progress=5, msg=f"probed: {info['duration']:.1f}s {info.get('width')}x{info.get('height')}")

        wav = self.dir / "audio.wav"
        if not wav.exists():
            transcribe.extract_audio(src, wav)
        self.set(stage="transcribe", progress=10, msg="transcribing")

        tpath = self.dir / "transcript.json"
        if not tpath.exists():
            t0 = time.time()
            t = transcribe.transcribe(wav)
            _write(tpath, t)
            self.set(msg=f"transcribed {len(t['words'])} words in {time.time() - t0:.0f}s")
        self.set(stage="solve", progress=40)

        game = self.meta.get("game") or "auto"
        if game == "auto":
            game, when, score = solvetone.detect_any(wav)
        else:
            when, score = solvetone.detect(wav, game)
        _write(self.dir / "solve.json", {"game": game, "at": when, "score": score})
        self.set(msg=f"solve tone: {game} at {when} (score {score})" if when is not None
                 else f"no solve tone found (best score {score})")

        self.recut(first=True)
        self.rerender()

    def recut(self, first=False):
        """Rebuild cut.json from transcript + params (+ relax ladder on first pass)."""
        words = _read(self.dir / "transcript.json")["words"]
        info = _read(self.dir / "probe.json")
        solve = (_read(self.dir / "solve.json") or {}).get("at")
        p = cutlist.Params.from_dict(_read(self.dir / "params.json"))
        if first:
            res, p, steps = cutlist.build_with_relax(words, info["duration"], solve, p)
            if steps:
                self.set(msg="relaxed to reach min length: " + ", ".join(steps))
            _write(self.dir / "params.json", p.to_dict())
        else:
            res = cutlist.build(words, info["duration"], solve, p)
        # snap the first and last cut to the audio: Whisper's word times are late
        # at onsets and early at offsets, and the brief is no space either side
        if res["keep"]:
            db, dt = transcribe.frame_db(self.dir / "audio.wav")
            s0 = transcribe.snap_start(db, dt, res["keep"][0][0] + p.start_air)
            res["keep"][0][0] = round(max(0.0, s0 - p.start_air), 3)
            res["start"] = res["keep"][0][0]
            e1 = transcribe.snap_end(db, dt, res["keep"][-1][1] - p.end_air)
            res["keep"][-1][1] = round(min(info["duration"], e1 + p.end_air), 3)
            res["end"] = res["keep"][-1][1]
            kept = sum(e - s_ for s_, e in res["keep"])
            res["source_kept"] = round(kept, 3)
            res["final_duration"] = round(kept / max(p.speed, 0.01), 3)
        _write(self.dir / "cut.json", res)
        self.set(stage="cut", progress=45,
                 msg=f"cut: {len(res['keep'])} segments, {res['source_kept']:.0f}s kept -> {res['final_duration']:.0f}s final"
                     + (f" flags={res['flags']}" if res['flags'] else ""))
        return res

    def rerender(self):
        src = self.input
        cut = _read(self.dir / "cut.json")
        p = _read(self.dir / "params.json")
        speed = float(p.get("speed", 1.0))
        self.set(stage="loudness", progress=50, msg="measuring loudness")
        loud = render.measure_loudness(src, cut["keep"], speed)
        _write(self.dir / "loudness_in.json", loud)
        self.set(stage="render", progress=55, msg=f"input loudness {loud['input_i']} LUFS, rendering")
        out = self.dir / "out.mp4"
        tmp = self.dir / "out.tmp.mp4"
        total = max(cut["final_duration"], 0.1)

        def prog(t):
            self.set(progress=55 + int(40 * min(t / total, 1.0)))

        render.render(src, cut["keep"], speed, tmp, loud, on_progress=prog)
        os.replace(tmp, out)
        # bump a version so the player cache-busts
        meta = self.meta
        meta["version"] = int(meta.get("version", 0)) + 1
        _write(self.dir / "meta.json", meta)
        final = render.measure_output_loudness(out)
        _write(self.dir / "loudness_out.json", final)
        self.write_sidecar()
        self.set(stage="review", progress=100,
                 msg=f"rendered v{meta['version']}: {cut['final_duration']:.0f}s, {final.get('input_i')} LUFS")

    def write_sidecar(self):
        cut = _read(self.dir / "cut.json")
        side = {
            "id": self.id,
            "meta": self.meta,
            "params": _read(self.dir / "params.json"),
            "cut": cut,
            "solve": _read(self.dir / "solve.json"),
            "loudness_out": _read(self.dir / "loudness_out.json"),
            "probe": _read(self.dir / "probe.json"),
            "history": _read(self.dir / "history.json", []),
            "flags": cut.get("flags", []),
        }
        _write(self.dir / "sidecar.json", side)
        return side

    # ---- reviewer note --------------------------------------------------------
    def apply_note(self, note: str):
        from . import editor_llm
        try:
            self.set(stage="editing", progress=45, msg=f"note: {note}")
            words = _read(self.dir / "transcript.json")["words"]
            params = _read(self.dir / "params.json")
            cut = _read(self.dir / "cut.json")
            history = _read(self.dir / "history.json", [])
            duration = _read(self.dir / "probe.json")["duration"]
            d = editor_llm.decide(note, words, params, cut, history, duration)
            newp = editor_llm.apply(d, params)
            changed = {k: v for k, v in newp.items() if params.get(k) != v}
            history.append({"note": note, "reply": d.reply, "changed": changed, "at": time.time()})
            _write(self.dir / "history.json", history)
            _write(self.dir / "params.json", newp)
            self.set(msg="editor: " + d.reply)
            if not changed:
                self.write_sidecar()
                self.set(stage="review", progress=100, msg="nothing changed")
                return d.reply
            self.recut()
            self.rerender()
            return d.reply
        except Exception as e:
            self.set(stage="failed", error=str(e), msg="note failed: " + str(e))
            (self.dir / "traceback.txt").write_text(traceback.format_exc())
            raise


def list_jobs():
    out = []
    if not WORK.exists():
        return out
    for d in sorted(WORK.iterdir(), reverse=True):
        if d.is_dir() and (d / "meta.json").exists():
            j = Job(d.name)
            out.append({"id": j.id, "meta": j.meta, "status": j.status(),
                        "cut": _read(d / "cut.json"), "has_video": (d / "out.mp4").exists()})
    return out
