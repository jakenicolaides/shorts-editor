"""Two tracks in, one clip out.

The recorder (the page a presenter records in) does not hand over a finished clip
the way OBS does. It records the game and the webcam SEPARATELY: the game as the
browser tab cropped to the game, at whatever size it had on that person's screen,
and the webcam at the camera's own size. Stacking them is done here, so the layout
is ours to change and a weak laptop is never asked to composite video while someone
is playing.

The result is the same shape OBS always produced (1080x1920, webcam above, game
below, one mixed sound track), so everything after this step is unchanged: the
cut, the title read, the solve tone and the render all see an ordinary clip.

  python -m shorts_editor.compose game.mp4 cam.mp4 out.mp4
"""
import os
import subprocess
from pathlib import Path

from .cancel import Token

W, H = 1080, 1920
CAM_H = 762            # the webcam is the top 40% of the frame (the in-house layout)
GAME_H = H - CAM_H
GAME_BG = "0xf4f4f2"   # both games' page colour, for the bars when the game's shape differs
FPS = 30               # the recorder caps both tracks at 30


def _has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def compose(game: Path, cam: Path, out: Path, offset_ms: float = 0.0, token: Token = None) -> Path:
    """Stack `cam` over `game` into `out`. offset_ms is how much LATER the webcam
    recording started than the game recording (negative: earlier), as the recorder
    measured it; the later one is delayed by that much so the two stay in step.
    Written beside `out` and moved into place, so a cancelled run never leaves a
    short file that looks finished."""
    tok = token or Token()
    game_audio, cam_audio = _has_audio(game), _has_audio(cam)
    if not (game_audio or cam_audio):
        raise RuntimeError("neither track has any sound: the microphone was not recorded")
    off = max(-5.0, min(5.0, float(offset_ms or 0) / 1000.0))   # a few ms in practice; a wild value is a bug, not a delay
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-itsoffset", f"{max(-off, 0):.3f}", "-i", str(game),
           "-itsoffset", f"{max(off, 0):.3f}", "-i", str(cam)]
    graph = [
        # the webcam fills its panel and is cropped to it; the game is fitted whole inside its own
        f"[1:v]fps={FPS},scale={W}:{CAM_H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{CAM_H},setsar=1[cam]",
        f"[0:v]fps={FPS},scale={W}:{GAME_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2:color={GAME_BG},setsar=1[game]",
        "[cam][game]vstack=inputs=2:shortest=1,format=yuv420p[v]",
    ]
    if game_audio and cam_audio:
        # normalize=0: a plain sum. The default halves each input, which would drop the voice 6 dB
        graph.append("[1:a][0:a]amix=inputs=2:duration=shortest:normalize=0[a]")
        amap = "[a]"
    else:
        amap = "1:a" if cam_audio else "0:a"
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", amap,
            # an intermediate, re-encoded once more by the render: near-lossless, and quick
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "256k", "-ar", "48000", "-ac", "2",
            "-shortest", "-movflags", "+faststart", str(tmp)]
    try:
        tok.run(cmd, check=True)
        os.replace(tmp, out)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("could not join the two tracks: " + (e.stderr or "").strip()[-400:])
    finally:
        tmp.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    print(compose(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
