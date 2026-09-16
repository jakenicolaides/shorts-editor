"""Pure cut-list logic. All times are SOURCE seconds.

A cut list is a list of [start, end] keep segments in source time, in order.
The rules (see README) in one place:
  * start on the first real word (start_air is near zero; the pipeline snaps
    it to the audio onset)
  * any stretch longer than max_gap with no real word in it (silence, or
    fillers only) is collapsed to `air` seconds either side
  * end right after the last word said within post_solve_window of the solve
    chime (plus end_air); the `protect_before` seconds before the solve are
    never cut, so the solve moment keeps its natural pacing
  * explicit overrides (start/end/keep_ranges/extra_cuts) come from the
    human-editor loop and win over the automatic rules
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Params:
    max_gap: float = 5.0          # seconds of no-content before we cut
    air: float = 0.5              # seconds left either side of a cut
    start_air: float = 0.05       # seconds before the first word (start right away)
    end_air: float = 0.15         # seconds after the last word (end right away)
    post_solve_window: float = 8.0  # the last word within this many s after the chime ends the video
    chime_len: float = 0.8        # fallback end when nothing is said after the chime
    protect_before: float = 15.0  # no cuts inside this window before the solve
    min_duration: float = 90.0    # target floor for the FINAL (post-speed) length
    remove_fillers: bool = True   # fillers count as non-content (so they can be cut)
    drop_false_starts: bool = True  # a phrase restarted after a pause loses its first attempt
    speed: float = 1.0
    start_override: Optional[float] = None
    end_override: Optional[float] = None
    solve_override: Optional[float] = None
    keep_ranges: list = field(default_factory=list)   # [[s,e],...] never cut inside
    extra_cuts: list = field(default_factory=list)    # [[s,e],...] always removed

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        p = cls()
        for k, v in (d or {}).items():
            if hasattr(p, k):
                setattr(p, k, v)
        return p


def _merge(intervals):
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _subtract(keep, cuts):
    for cs, ce in cuts:
        nxt = []
        for s, e in keep:
            if ce <= s or cs >= e:
                nxt.append([s, e])
                continue
            if cs > s:
                nxt.append([s, cs])
            if ce < e:
                nxt.append([ce, e])
        keep = nxt
    return keep


def _norm(w):
    import re
    return re.sub(r"[^a-z']", "", w.lower())


RESTART_PHRASES = ("start again", "start that again", "try again", "do that again", "go again", "take two", "from the top")


def false_starts(words, min_words=3, pause=1.5, max_len=12.0, lookahead=2):
    """Ranges to drop: a phrase, then (optionally a short aside like "let's start
    that again"), then a phrase that starts with the same first `min_words`
    words is a restart; everything before the restart goes."""
    groups, cur = [], []
    for w in words:
        if w.get("filler"):
            continue
        if cur and w["s"] - cur[-1]["e"] > pause:
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)

    def head(g):
        return [_norm(w["w"]) for w in g[:min_words]]

    def text(g):
        return " ".join(_norm(w["w"]) for w in g)

    drops = []
    i = 0
    while i < len(groups):
        a = groups[i]
        hit = None
        if len(a) >= min_words and a[-1]["e"] - a[0]["s"] <= max_len:
            for k in range(1, lookahead + 1):
                if i + k >= len(groups):
                    break
                b = groups[i + k]
                between = groups[i + 1:i + k]
                # anything skipped over must be a short aside (a restart phrase, or a few words)
                if any(len(g) > 6 or (g[-1]["e"] - g[0]["s"]) > 4.0 for g in between):
                    break
                if len(b) >= min_words and head(b) == head(a):
                    hit = k
                    break
        if hit:
            drops.append([a[0]["s"], groups[i + hit - 1][-1]["e"]])
            i += hit
        else:
            i += 1
    # a bare restart phrase right before a phrase is a false start too, even
    # when the retake does not repeat the words
    for g, nxt in zip(groups, groups[1:]):
        if any(ph in text(g) for ph in RESTART_PHRASES) and len(g) <= 6:
            drops.append([g[0]["s"], g[-1]["e"]])
    return _merge(sorted(drops)) if drops else []


def build(words, duration, solve_at, p: Params):
    """Return {"keep": [[s,e],...], "source_kept": float, "final_duration": float,
    "start": float, "end": float, "solve_at": float|None, "flags": [..]}"""
    flags = []
    solve = p.solve_override if p.solve_override is not None else solve_at
    if p.drop_false_starts:
        fs = false_starts(words)
        if fs:
            flags.append(f"false_starts_dropped:{len(fs)}")
            words = [w for w in words if not any(a <= w["s"] and w["e"] <= b for a, b in fs)]
    content = [(w["s"], w["e"]) for w in words if not (p.remove_fillers and w.get("filler"))]
    if not content:
        flags.append("no_speech")
        content = [(0.0, duration)]

    # Bounds
    start = p.start_override if p.start_override is not None else max(0.0, content[0][0] - p.start_air)
    if p.end_override is not None:
        end = p.end_override
    elif solve is not None:
        after = [e for s_, e in content if solve <= e <= solve + p.post_solve_window]
        end = (max(after) if after else solve + p.chime_len) + p.end_air
    else:
        end = content[-1][1] + p.end_air
        flags.append("no_solve_detected")
    end = min(end, duration)
    start = max(0.0, min(start, end))

    # Protected windows behave like content.
    protected = list(p.keep_ranges or [])
    if solve is not None:
        protected.append([max(start, solve - p.protect_before), end])
    content = content + [(s, e) for s, e in protected]

    # Clip to bounds and group by max_gap.
    content = sorted((max(start, s), min(end, e)) for s, e in content if e > start and s < end)
    groups = []
    for s, e in content:
        if groups and s - groups[-1][1] <= p.max_gap:
            groups[-1][1] = max(groups[-1][1], e)
        else:
            groups.append([s, e])

    keep = []
    for i, (s, e) in enumerate(groups):
        ks = start if i == 0 else s - p.air
        ke = end if i == len(groups) - 1 else e + p.air
        keep.append([max(start, ks), min(end, ke)])
    keep = _merge(keep)
    keep = _subtract(keep, [[float(a), float(b)] for a, b in (p.extra_cuts or [])])
    keep = [[round(s, 3), round(e, 3)] for s, e in keep if e - s >= 0.1]

    kept = sum(e - s for s, e in keep)
    return {
        "keep": keep,
        "source_kept": round(kept, 3),
        "final_duration": round(kept / max(p.speed, 0.01), 3),
        "start": round(start, 3),
        "end": round(end, 3),
        "solve_at": solve,
        "flags": flags,
    }


RELAX_LADDER = [
    ("max_gap", 7.0), ("max_gap", 10.0), ("max_gap", 15.0),
    ("remove_fillers", False),
]


def build_with_relax(words, duration, solve_at, p: Params):
    """Apply the rules, then loosen them step by step until the final length
    reaches min_duration or the ladder runs out. Returns (result, params_used, steps)."""
    steps = []
    cur = Params.from_dict(p.to_dict())
    res = build(words, duration, solve_at, cur)
    for key, val in RELAX_LADDER:
        if res["final_duration"] >= cur.min_duration:
            break
        if getattr(cur, key) == val:
            continue
        setattr(cur, key, val)
        steps.append(f"{key}={getattr(cur, key)}")
        res = build(words, duration, solve_at, cur)
    if res["final_duration"] < cur.min_duration:
        res["flags"].append("under_min_duration")
    return res, cur, steps
