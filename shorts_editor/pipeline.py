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
  title.json       what was read off the frame and the scheduled puzzle it matched
"""
import json
import os
import shutil
import time
import traceback
from datetime import datetime
from pathlib import Path

from . import transcribe, cutlist, solvetone, render
from .cancel import Cancelled, Token

ROOT = Path(__file__).resolve().parent.parent
WORK = Path(os.environ.get("SHORTS_WORK") or ROOT / "work")  # override: a test instance beside the real one

_tokens = {}  # job id -> the Token of the action running on it


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

    def exists(self):
        return (self.dir / "meta.json").is_file()

    # ---- cancel / delete ------------------------------------------------------
    @property
    def token(self) -> Token:
        return _tokens.setdefault(self.id, Token())

    def begin(self):
        """A fresh token for a new background action (the last one may be spent)."""
        _tokens[self.id] = Token()

    def cancel(self):
        self.token.cancel()

    def _cancelled(self):
        (self.dir / "out.tmp.mp4").unlink(missing_ok=True)
        has_video = (self.dir / "out.mp4").exists()
        self.set(stage="review" if has_video else "cancelled", progress=100 if has_video else 0,
                 msg="cancelled" + (": showing the last render" if has_video else ""))

    def delete(self):
        """Remove the job's working directory. A copy saved to Dropbox is not touched."""
        shutil.rmtree(self.dir)
        _tokens.pop(self.id, None)

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
        # Ids are timestamps to the second, so several clips dropped together (their
        # uploads finish within a second of each other) must not share one: on 2026-09-24
        # four did, and one job ran four times over a mix of two clips' files.
        base = datetime.now().strftime("%Y-%m-%d-%H%M%S")
        job_id, n = base, 2
        while (WORK / job_id).exists():
            job_id, n = f"{base}-{n}", n + 1
        job = cls(job_id)
        job.dir.mkdir(parents=True, exist_ok=False)
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
        except Cancelled:
            self._cancelled()
        except Exception as e:  # surfaced on the UI
            self.set(stage="failed", error=str(e), msg="failed: " + str(e))
            (self.dir / "traceback.txt").write_text(traceback.format_exc())

    def rerun(self):
        """Re-cut and re-render with the current params. With no render yet (a first
        run that was cancelled or failed) pick the run back up where it stopped."""
        if not (self.dir / "out.mp4").exists():
            return self.run()
        try:
            self.recut()
            self.rerender()
        except Cancelled:
            self._cancelled()
        except Exception as e:
            self.set(stage="failed", error=str(e), msg="re-render failed: " + str(e))
            (self.dir / "traceback.txt").write_text(traceback.format_exc())

    def _run(self):
        tok = self.token
        src = self.input
        info = render.probe(src)
        _write(self.dir / "probe.json", info)
        self.set(stage="audio", progress=5, msg=f"probed: {info['duration']:.1f}s {info.get('width')}x{info.get('height')}")

        wav = self.dir / "audio.wav"
        if not wav.exists():
            transcribe.extract_audio(src, wav, token=tok)
        self.set(stage="transcribe", progress=10, msg="transcribing")

        tpath = self.dir / "transcript.json"
        if not tpath.exists():
            t0 = time.time()
            t = transcribe.transcribe_killable(wav, tpath, tok, on_wait=lambda: self.set(msg="waiting for another clip's transcription to finish"))
            self.set(msg=f"transcribed {len(t['words'])} words in {time.time() - t0:.0f}s")
        self.set(stage="solve", progress=40)

        game = self.meta.get("game") or "auto"
        if game == "auto":
            game, when, score = solvetone.detect_any(wav)
        else:
            when, score = solvetone.detect(wav, game)
        tok.check()
        _write(self.dir / "solve.json", {"game": game, "at": when, "score": score})
        self.set(msg=f"solve tone: {game} at {when} (score {score})" if when is not None
                 else f"no solve tone found (best score {score})")

        found = self.infer_title(game, when, info["duration"])
        if found and found != game:
            # the tone was matched against the wrong game's template (or the best of two misses)
            when, score = solvetone.detect(wav, found)
            _write(self.dir / "solve.json", {"game": found, "at": when, "score": score})
            self.set(msg=f"solve tone again as {found}: at {when} (score {score})")
        self.recut(relax=True)
        self.rerender()

    def set_speed(self, speed: float):
        """Speed is chosen after the first cut, on the job's panel. The 1:30 floor is
        measured after the speed-up, so the relax ladder runs again."""
        undo = {f: _read(self.dir / f) for f in ("params.json", "cut.json", "relax.json", "meta.json")}
        try:
            _write(self.dir / "params.json", {**undo["params.json"], "speed": speed})
            self._write_meta({**undo["meta.json"], "speed": speed})
            self.set(msg=f"speed {speed}x")
            self.recut(relax=True)
            self.rerender()
        except Cancelled:
            # it never happened: the render on screen is still the old speed
            for f, data in undo.items():
                if data is not None:
                    _write(self.dir / f, data)
                else:
                    (self.dir / f).unlink(missing_ok=True)
            self._cancelled()
        except Exception as e:
            self.set(stage="failed", error=str(e), msg="re-render failed: " + str(e))
            (self.dir / "traceback.txt").write_text(traceback.format_exc())

    def infer_title(self, game, solve_at, duration):
        """Name the job from the clip (title.py). With a typed name this only checks
        it against the schedule and offers the schedule's version if they differ.
        Never fatal: without a name the page asks for one before saving.
        Returns the game, which also decides the Dropbox folder. Nobody picks it
        at the drop: the schedule's match is the authority, then what the frame
        showed, then the solve tone. A game given explicitly (CLI) only yields to
        a schedule match: clips have been dropped under the wrong game."""
        if (self.dir / "title.json").exists():
            return None
        from . import title
        self.set(stage="title", progress=42, msg="working out the name from the clip")
        if solve_at is None:
            # no tone for the game that was picked: a tone from the other game still says
            # where the solved screen is (and the clip may be under the wrong game)
            _, solve_at, _ = solvetone.detect_any(self.dir / "audio.wav")
        try:
            res = title.infer(self.input, self.dir, duration, solve_at, game, token=self.token)
        except Cancelled:
            raise
        except Exception as e:
            res = {"title": None, "why": str(e)}
        else:
            _write(self.dir / "title.json", res)
        meta = self.meta
        typed = (meta.get("title") or "").strip()
        if not res["title"]:
            meta["title_why"] = res["why"]
            self.set(msg="no name from the clip: " + res["why"])
        elif not typed:
            meta["title"] = res["title"]
            self.set(msg=f"named from the schedule: {res['title']}")
        elif typed.lower() != res["title"]:
            meta["title_suggested"] = res["title"]
            self.set(msg=f"the schedule has this puzzle as: {res['title']}")
        picked = meta.get("game") or "auto"
        found = res.get("game") if (res["title"] or picked == "auto") else None
        if found not in title.GAMES:
            found = None
        if found and picked != found:
            if picked != "auto":
                self.set(msg=f"this is a {found} puzzle, not {picked}: game changed")
            meta["game"] = found  # concrete from here on, so a resumed run keeps it
        self._write_meta(meta)
        return found

    def rename(self, new_title: str):
        meta = self.meta
        meta["title"] = new_title.strip()
        meta.pop("title_suggested", None)
        meta.pop("title_why", None)
        self._write_meta(meta)
        if (self.dir / "cut.json").exists():
            self.write_sidecar()
        self.set(msg=f"renamed to {meta['title']}")

    def recut(self, relax=False):
        """Rebuild cut.json from transcript + params. relax=True (first cut, speed
        change) runs the min-length ladder; what it loosened is remembered in
        relax.json and put back first, so a slower speed tightens the cut again.
        A knob a note has since moved is the reviewer's and is left alone."""
        words = _read(self.dir / "transcript.json")["words"]
        info = _read(self.dir / "probe.json")
        solve = (_read(self.dir / "solve.json") or {}).get("at")
        p = cutlist.Params.from_dict(_read(self.dir / "params.json"))
        if relax:
            for k, (was, now) in (_read(self.dir / "relax.json") or {}).items():
                if getattr(p, k) == now:
                    setattr(p, k, was)
            before = p.to_dict()
            res, p, steps = cutlist.build_with_relax(words, info["duration"], solve, p)
            _write(self.dir / "relax.json", {k: [before[k], v] for k, v in p.to_dict().items() if before[k] != v})
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
        tok = self.token
        tok.check()
        src = self.input
        cut = _read(self.dir / "cut.json")
        p = _read(self.dir / "params.json")
        speed = float(p.get("speed", 1.0))
        self.set(stage="loudness", progress=50, msg="measuring loudness")
        loud = render.measure_loudness(src, cut["keep"], speed, token=tok)
        _write(self.dir / "loudness_in.json", loud)
        self.set(stage="render", progress=55, msg=f"input loudness {loud['input_i']} LUFS, rendering")
        out = self.dir / "out.mp4"
        tmp = self.dir / "out.tmp.mp4"
        total = max(cut["final_duration"], 0.1)

        def prog(t):
            self.set(progress=55 + int(40 * min(t / total, 1.0)))

        render.render(src, cut["keep"], speed, tmp, loud, on_progress=prog, token=tok)
        os.replace(tmp, out)
        # bump a version so the player cache-busts
        meta = self.meta
        meta["version"] = int(meta.get("version", 0)) + 1
        _write(self.dir / "meta.json", meta)
        final = render.measure_output_loudness(out)  # not killable: out.mp4 is already the new render
        meta["too_large"] = out.stat().st_size > render.SHARE_LIMIT   # the phone cannot share it (Chrome's cap)
        _write(self.dir / "meta.json", meta)
        if meta["too_large"]:
            self.set(msg=f"render is {out.stat().st_size / 1e6:.0f} MB, over the {render.SHARE_LIMIT / 1e6:.0f} MB the phone can share")
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
            prior = list(history)
            duration = _read(self.dir / "probe.json")["duration"]
            d = editor_llm.decide(note, words, params, cut, history, duration)
            self.token.check()
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
        except Cancelled:
            # the note never happened: the old render is still the one on screen
            _write(self.dir / "params.json", params)
            _write(self.dir / "cut.json", cut)
            _write(self.dir / "history.json", prior)
            self._cancelled()
            return None
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
