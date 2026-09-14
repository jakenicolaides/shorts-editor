"""Pure cut-list logic. All times are SOURCE seconds.

A cut list is a list of [start, end] keep segments in source time, in order.
The rules (see README) in one place:
  * start at the first real word minus start_air
  * any stretch longer than max_gap with no real word in it (silence, or
    fillers only) is collapsed to `air` seconds either side
  * end at the solve moment plus `tail`; the `protect_before` seconds before
    the solve are never cut, so the solve moment keeps its natural pacing
  * explicit overrides (start/end/keep_ranges/extra_cuts) come from the
    human-editor loop and win over the automatic rules
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Params:
    max_gap: float = 5.0          # seconds of no-content before we cut
    air: float = 0.5              # seconds left either side of a cut
    start_air: float = 0.3        # seconds before the first word
    tail: float = 2.5             # seconds kept after the solve moment
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


def false_starts(words, min_words=3, pause=1.5, max_len=12.0):
    """Ranges to drop: a phrase, then a pause, then a phrase that starts with
    the same first `min_words` words is a restart; the first attempt goes."""
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
    drops = []
    for a, b in zip(groups, groups[1:]):
        if len(a) < min_words or len(b) < min_words:
            continue
        if a[-1]["e"] - a[0]["s"] > max_len:
            continue
        if [_norm(w["w"]) for w in a[:min_words]] == [_norm(w["w"]) for w in b[:min_words]]:
            drops.append([a[0]["s"], a[-1]["e"]])
    return drops


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
        end = solve + p.tail
    else:
        end = content[-1][1] + p.tail
        flags.append("no_solve_detected")
    end = min(end, duration)
    start = max(0.0, min(start, end))

    # Protected windows behave like content.
    protected = list(p.keep_ranges or [])
    if solve is not None:
        protected.append([max(start, solve - p.protect_before), min(end, solve + p.tail)])
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
    ("tail", "+2"), ("tail", "+2"),
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
        if key == "tail" and isinstance(val, str):
            cur.tail = cur.tail + float(val)
        else:
            if getattr(cur, key) == val:
                continue
            setattr(cur, key, val)
        steps.append(f"{key}={getattr(cur, key)}")
        res = build(words, duration, solve_at, cur)
    if res["final_duration"] < cur.min_duration:
        res["flags"].append("under_min_duration")
    return res, cur, steps
