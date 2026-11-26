"""How far a player's edit has got, for their phone (the game's record mode shows a bar and the step).

When PROGRESS_FILE is set (bin/selfrec-watch.py sets it per take), report() writes the step and the
whole edit's fraction there, and the watcher sends it on to storage every couple of seconds, where the
phone's status checks read it. Unset, it does nothing, so the presenters' edits are untouched.

Steps and their share of the whole: drawing the game 0 to 65%, cutting 66%, balancing the sound 70%,
rendering 70 to 98%. Measured on a phone take: drawing is most of the time.
"""
import json
import os
import time


def report(step: str, frac: float):
    path = os.environ.get("PROGRESS_FILE")
    if not path:
        return
    try:
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"step": step, "frac": round(max(0.0, min(1.0, frac)), 3), "at": time.time()}, f)
        os.replace(tmp, path)
    except OSError:
        pass
