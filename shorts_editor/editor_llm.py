"""The human-editor loop: a free-text note from the reviewer becomes a change to
the cut parameters or explicit cut/keep ranges. Claude sees the transcript
timeline, the current parameters, the current keep list, and the note, and
returns a strictly-typed decision. It can only move the knobs below, so a note
can never do anything outside the cut list."""
from typing import Optional, List
from pydantic import BaseModel, Field
import anthropic

MODEL = "claude-opus-5"


class Range(BaseModel):
    start: float = Field(description="source seconds")
    end: float = Field(description="source seconds")


class EditDecision(BaseModel):
    reply: str = Field(description="One or two plain sentences back to the reviewer saying what you changed and why, or why you could not.")
    max_gap: Optional[float] = Field(default=None, description="New silence threshold in seconds, or null to leave it")
    air: Optional[float] = None
    start_air: Optional[float] = None
    tail: Optional[float] = Field(default=None, description="Seconds kept after the solve moment")
    protect_before: Optional[float] = None
    min_duration: Optional[float] = None
    remove_fillers: Optional[bool] = None
    speed: Optional[float] = None
    start_override: Optional[float] = Field(default=None, description="Force the video to start at this source time")
    end_override: Optional[float] = Field(default=None, description="Force the video to end at this source time")
    solve_override: Optional[float] = Field(default=None, description="Where the solve moment actually is, if the detector got it wrong")
    add_keep_ranges: List[Range] = Field(default_factory=list, description="Source ranges that must never be cut")
    add_extra_cuts: List[Range] = Field(default_factory=list, description="Source ranges that must be removed; leave ~0.5s of air yourself")
    clear_overrides: bool = Field(default=False, description="True to drop every previous keep range, extra cut and start/end/solve override before applying this decision")


SYSTEM = """You are the editor for short vertical puzzle-game videos (TikTok style). A reviewer watches the automatic cut and sends you a note, the way they would talk to a human editor. You turn the note into a change to the cut.

You can only change the parameters and ranges in the output schema. All times are SOURCE seconds (the raw recording), never output seconds. The timeline you are given is the transcript of the raw recording with gaps marked, and the current keep list says which source ranges survive.

How the automatic cut works, so you know which knob does what:
- The video starts at the first real word minus start_air.
- Any stretch longer than max_gap seconds with no real word (silence, or only fillers like um/err) is removed, leaving `air` seconds either side. Stretches shorter than max_gap are kept whole.
- The video ends at the solve moment plus `tail` seconds. The protect_before seconds before the solve are never cut, so the solve keeps its natural pacing.
- speed is a uniform playback speed-up applied to the whole video.
- If the result is under min_duration the system loosens max_gap, then stops removing fillers, then lengthens the tail.
- Overrides win over the rules: start_override/end_override/solve_override pin those moments; keep ranges are never cut; extra cuts are always removed.

Guidance:
- Prefer the smallest change that does what the note asks. Adjust a knob for a general complaint ("too choppy", "too many cuts", "it drags"); use ranges for a specific moment ("cut the bit where I mess up the second word", "keep the pause before the last guess").
- When the note names a moment, find it in the timeline and quote the words in your reply so the reviewer can check you found the right place.
- If a note is ambiguous, make the most likely change and say what you assumed.
- If the note asks for something you cannot do with these knobs, say so in the reply and change nothing."""


def _timeline(words, gap_mark=1.5):
    """Compact transcript: phrases with start-end times, gaps over gap_mark marked."""
    lines = []
    cur = []
    cur_s = None
    last_e = None
    for w in words:
        if last_e is not None and w["s"] - last_e > gap_mark:
            if cur:
                lines.append(f"{cur_s:7.1f}-{last_e:6.1f}  {' '.join(cur)}")
            lines.append(f"        [gap {w['s'] - last_e:.1f}s]")
            cur, cur_s = [], None
        if cur_s is None:
            cur_s = w["s"]
        cur.append(w["w"])
        last_e = w["e"]
    if cur:
        lines.append(f"{cur_s:7.1f}-{last_e:6.1f}  {' '.join(cur)}")
    return "\n".join(lines)


def _ensure_key():
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    from pathlib import Path
    for f in (Path(__file__).resolve().parent.parent / ".env",
              Path.home() / "Documents/unseen_server/srv/www/sites/twixt.games/.env.local",
              Path.home() / "Documents/unseen_server/srv/www/sites/prospector.unseenforms.com/.env.server"):
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY=") and line.split("=", 1)[1].strip():
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
                    return


def decide(note: str, words: list, params: dict, cut: dict, history: list, duration: float) -> EditDecision:
    _ensure_key()
    client = anthropic.Anthropic()
    keep_txt = "\n".join(f"  {s:.1f}-{e:.1f}" for s, e in cut["keep"])
    hist_txt = "\n".join(f"- reviewer: {h['note']}\n  editor: {h['reply']}" for h in history) or "(none)"
    user = f"""Raw recording length: {duration:.1f}s. Solve moment detected at: {cut.get('solve_at')}.
Current final length: {cut['final_duration']:.1f}s at speed {params.get('speed')}.

Current parameters:
{params}

Current keep list (source seconds):
{keep_txt}

Previous notes this session:
{hist_txt}

Transcript timeline (source seconds):
{_timeline(words)}

Reviewer's note:
{note}"""
    resp = client.messages.parse(
        model=MODEL,
        max_tokens=16000,
        system=SYSTEM,
        messages=[{"role": "user", "content": user}],
        output_format=EditDecision,
    )
    if resp.stop_reason == "refusal":
        return EditDecision(reply="The editor declined this note.")
    return resp.parsed_output


def apply(decision: EditDecision, params: dict) -> dict:
    p = dict(params)
    if decision.clear_overrides:
        p["keep_ranges"], p["extra_cuts"] = [], []
        p["start_override"] = p["end_override"] = p["solve_override"] = None
    for k in ("max_gap", "air", "start_air", "tail", "protect_before", "min_duration",
              "remove_fillers", "speed", "start_override", "end_override", "solve_override"):
        v = getattr(decision, k)
        if v is not None:
            p[k] = v
    p["keep_ranges"] = list(p.get("keep_ranges") or []) + [[r.start, r.end] for r in decision.add_keep_ranges]
    p["extra_cuts"] = list(p.get("extra_cuts") or []) + [[r.start, r.end] for r in decision.add_extra_cuts]
    return p
