"""Re-exec without DYLD_* so the venv's own MLX library is the one that loads.
~/.zshrc exports DYLD_LIBRARY_PATH=/opt/homebrew/lib, and dyld then picks up
Homebrew's libmlx over the copy bundled with the mlx wheel (different version,
missing symbols, Whisper import fails). dyld reads the environment at process
start, so the fix has to be a fresh process."""
import os
import sys


def clean_dyld():
    bad = [k for k in os.environ if k.startswith("DYLD_")]
    if not bad:
        return
    env = {k: v for k, v in os.environ.items() if k not in bad}
    spec = getattr(sys.modules.get("__main__"), "__spec__", None)
    if spec and spec.name:  # started with -m: keep it that way
        argv = [sys.executable, "-m", spec.name] + sys.argv[1:]
    else:
        argv = [sys.executable] + sys.argv
    os.execve(sys.executable, argv, env)
