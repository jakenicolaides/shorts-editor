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
AUDIO_RATE = 48000
GAME_GAIN_DB = -2.0    # the game's own sounds sit a touch under the voice (Jake, by ear, 2026-11-19)


def _has_audio(path: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    return bool(r.stdout.strip())


def _sound_faults(path: Path) -> float:
    """Seconds by which the file's sound packets depart from a steady clock: sound that
    its timestamps say is missing, or packets piled onto one instant. A sound track
    should have none, and everything after this step trusts that it has none."""
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "packet=pts_time",
                        "-of", "csv=p=0", str(path)], capture_output=True, text=True)
    pts = []
    for line in r.stdout.splitlines():
        try:
            pts.append(float(line.split(",")[0]))
        except ValueError:
            pass
    step = 1024 / AUDIO_RATE   # one AAC packet
    return sum(abs(b - a - step) for a, b in zip(pts, pts[1:]) if abs(b - a - step) > 0.5 * step)


def mix(game: Path, cam: Path, out_wav: Path, offset_ms: float = 0.0, token: Token = None) -> Path:
    """The take's one sound track, as a wav: the voice, with the game's own sounds under it.

    Made on its own, with no picture in the same ffmpeg run, and as a wav, which is
    samples in a row with no timestamps to go wrong. Both of those are the fix for a
    real fault (Jake's first take on the live recorder, 2026-11-20): mixed inside the
    same run as the picture, the sound came out with a hole or a pile-up a quarter of
    a second in, and the voice ran up to 1.5 s out until the first cut.

    Each track is first laid on the clock from zero (aresample async, first_pts=0):
    silence up to where it really starts and across any hole in it, so the mixer, which
    goes by the ORDER samples arrive in and not by their timestamps, is handed two
    tracks that already agree about when zero is."""
    tok = token or Token()
    game_audio, cam_audio = _has_audio(game), _has_audio(cam)
    if not (game_audio or cam_audio):
        raise RuntimeError("neither track has any sound: the microphone was not recorded")
    off = max(-5.0, min(5.0, float(offset_ms or 0) / 1000.0))
    lay = f"aresample={AUDIO_RATE}:async=1:first_pts=0"
    if game_audio and cam_audio:
        # normalize=0: a plain sum. The default halves each input, which would drop the voice 6 dB
        graph = f"[0:a]{lay},volume={GAME_GAIN_DB}dB[ga];[1:a]{lay}[ca];[ca][ga]amix=inputs=2:duration=shortest:normalize=0[a]"
    else:
        graph = f"[{1 if cam_audio else 0}:a]{lay}[a]"
    tmp = out_wav.with_name(out_wav.stem + ".tmp.wav")
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-itsoffset", f"{max(-off, 0):.3f}", "-i", str(game),
           "-itsoffset", f"{max(off, 0):.3f}", "-i", str(cam),
           "-filter_complex", graph, "-map", "[a]", "-vn",
           "-c:a", "pcm_s16le", "-ar", str(AUDIO_RATE), "-ac", "2", str(tmp)]
    try:
        tok.run(cmd, check=True)
        os.replace(tmp, out_wav)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("could not mix the two tracks' sound: " + (e.stderr or "").strip()[-400:])
    finally:
        tmp.unlink(missing_ok=True)
    return out_wav


# A PICTURE THAT STOPS SHORT OF THE SOUND HOLDS ITS LAST FRAME, for up to this long. A browser
# whose encoder falls behind saves fewer frames, and its file's picture ends before its sound:
# Jake's take of 2026-11-26 saved 3 frames a second and its picture stopped 3.4 s before he
# did, which cut his closing words. The take runs to the end of the sound instead.
HOLD_S = 10.0


def stack_filters(out: str = "st", game: str = "0:v", cam: str = "1:v") -> list:
    """The picture: the webcam over the game, as ffmpeg filters ending in [out]. The one
    statement of the layout, used by the join below and by the render (render.py)."""
    ov = os.environ.get("STACK_OVERLAY")
    if ov:
        # EXPERIMENT (replay.py, REPLAY_LAYOUT=fullscreen): the game ON TOP, filling the width, and the
        # camera in a band of this height BELOW it, cropped to fill, kept a little below centre where a
        # selfie has its face. Upside down from the presenters' layout, and nothing overlaps.
        ch = int(ov)
        return [
            f"[{game}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},scale={W}:{H - ch}:flags=lanczos,setsar=1[_game]",
            f"[{cam}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},"
            f"scale={W}:{ch}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{ch}:(iw-{W})/2:(ih-{ch})*0.8,setsar=1[_cam]",
            f"[_game][_cam]vstack=inputs=2:shortest=1,format=yuv420p[{out}]",
        ]
    return [
            f"[{game}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},scale={W}:{H}:flags=lanczos,setsar=1[_game]",
            f"[{cam}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},"
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,crop={w}:{h}:(iw-{w})/2:(ih-{h})*0.8,setsar=1[_cam]",
            f"[_game][_cam]overlay={x}:{y}:shortest=1,format=yuv420p[{out}]",
        ]
    return [
        # the webcam fills its panel and is cropped to it; the game is fitted whole inside its own
        # A landscape webcam (a presenter's) fills its panel and is cropped to it, as it always was. An
        # UPRIGHT camera (a phone held upright, a player recording themselves) is fitted WHOLE, with
        # black either side (Jake, 2026-11-26): cropped, a selfie lost its top and bottom and read as
        # zoomed in. One chain does both, choosing by the picture's own shape, so nothing is passed.
        f"[{cam}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},"
        f"scale=w='trunc(iw*if(gt(ih,iw),min({W}/iw,{CAM_H}/ih),max({W}/iw,{CAM_H}/ih))/2)*2'"
        f":h='trunc(ih*if(gt(ih,iw),min({W}/iw,{CAM_H}/ih),max({W}/iw,{CAM_H}/ih))/2)*2':flags=lanczos,"
        f"crop=w='min(iw,{W})':h='min(ih,{CAM_H})',pad={W}:{CAM_H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[_cam]",
        f"[{game}]fps={FPS},tpad=stop_mode=clone:stop_duration={HOLD_S:g},scale={W}:{GAME_H}:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad={W}:{GAME_H}:(ow-iw)/2:(oh-ih)/2:color={GAME_BG},setsar=1[_game]",
        f"[_cam][_game]vstack=inputs=2:shortest=1,format=yuv420p[{out}]",
    ]


class Tracks:
    """A recorder take as the rest of the editor uses it: the two picture tracks as they
    were recorded, and the one mixed sound track (mix, above).

    There is no joined clip. There used to be: the two were stacked and encoded into
    one file, which the render then decoded and encoded again. That first encode was a
    fifth of the whole edit on the server and bought nothing, so the render now stacks
    the picture itself, in the same pass that cuts it."""

    def __init__(self, game: Path, cam: Path, sound: Path, offset_ms: float = 0.0):
        self.game, self.cam, self.sound = Path(game), Path(cam), Path(sound)
        self.off = max(-5.0, min(5.0, float(offset_ms or 0) / 1000.0))   # the webcam started this much later (s)

    # the later track is delayed, as in mix(): sound and picture must agree about that
    @property
    def game_delay(self) -> float: return max(-self.off, 0.0)
    @property
    def cam_delay(self) -> float: return max(self.off, 0.0)

    def inputs(self) -> list:
        """ffmpeg input arguments: 0 = game, 1 = webcam, 2 = the mixed sound.

        -reinit_filter 0 on both pictures: a tab capture changes size for a few frames when Chrome
        shows or hides a bar above the page (a presenter's take did, 2026-11-25, by 8 to 18 pixels
        for a fifth of a second). Left alone ffmpeg rebuilds the whole graph at each change, which
        restarts `trim` and loses everything before it (27s of that take), and warns about every
        sound packet after. The scale filter takes a frame of another size as it comes."""
        return ["-reinit_filter", "0", "-itsoffset", f"{self.game_delay:.3f}", "-i", str(self.game),
                "-reinit_filter", "0", "-itsoffset", f"{self.cam_delay:.3f}", "-i", str(self.cam),
                "-i", str(self.sound)]

    def duration(self) -> float:
        """How long the take is: the sound, unless a picture stops more than HOLD_S before it
        (a picture a little short holds its last frame, stack_filters)."""
        def dur(path, stream):
            r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", stream, "-show_entries", "stream=duration:format=duration",
                                "-of", "json", str(path)], capture_output=True, text=True, check=True)
            import json
            j = json.loads(r.stdout)
            d = (j.get("streams") or [{}])[0].get("duration") or j["format"]["duration"]
            return float(d)
        return min(dur(self.game, "v:0") + self.game_delay + HOLD_S, dur(self.cam, "v:0") + self.cam_delay + HOLD_S, dur(self.sound, "a:0"))

    def frame(self, t: float, out: Path, token: Token = None, width: int = 720) -> Path:
        """One frame of the finished layout at time t, for reading the puzzle off the picture."""
        cmd = ["ffmpeg", "-y", "-v", "error",
               "-ss", f"{max(t - self.game_delay, 0):.2f}", "-i", str(self.game),
               "-ss", f"{max(t - self.cam_delay, 0):.2f}", "-i", str(self.cam),
               "-filter_complex", ";".join(stack_filters("st")) + f";[st]scale={width}:-2[f]",
               "-map", "[f]", "-frames:v", "1", "-pix_fmt", "yuvj420p", str(out)]
        (token or Token()).run(cmd, check=True)
        return out


def compose(game: Path, cam: Path, out: Path, offset_ms: float = 0.0, token: Token = None, sound: Path = None) -> Path:
    """Stack `cam` over `game` into `out`. offset_ms is how much LATER the webcam
    recording started than the game recording (negative: earlier), as the recorder
    measured it; the later one is delayed by that much so the two stay in step.
    The editor itself no longer does this (it renders from the Tracks, above): this is
    for looking at a take as one file, by hand.
    Two runs: the sound first (mix), then the picture with that sound laid beside it.
    `sound` is a mix already made (the pipeline makes it early, to transcribe while the
    picture is still being joined); it is the caller's file and is left alone.
    Written beside `out` and moved into place, so a cancelled run never leaves a
    short file that looks finished."""
    tok = token or Token()
    off = max(-5.0, min(5.0, float(offset_ms or 0) / 1000.0))   # a few ms in practice; a wild value is a bug, not a delay
    own = sound is None
    if own:
        sound = mix(game, cam, out.with_name(out.stem + ".mix.wav"), offset_ms, tok)
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-itsoffset", f"{max(-off, 0):.3f}", "-i", str(game),
           "-itsoffset", f"{max(off, 0):.3f}", "-i", str(cam),
           "-i", str(sound)]
    graph = stack_filters("v")
    tmp = out.with_name(out.stem + ".tmp" + out.suffix)
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "2:a",
            # an intermediate, re-encoded once more by the render: near-lossless, and quick
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-r", str(FPS),
            "-c:a", "aac", "-b:a", "256k", "-ar", str(AUDIO_RATE), "-ac", "2",
            "-shortest", "-movflags", "+faststart", str(tmp)]
    try:
        tok.run(cmd, check=True)
        bad = _sound_faults(tmp)
        if bad > 0.1:   # never hand on a clip whose sound and picture have parted
            raise RuntimeError(f"the joined clip's sound is {bad:.2f}s off a steady clock")
        os.replace(tmp, out)
    except subprocess.CalledProcessError as e:
        raise RuntimeError("could not join the two tracks: " + (e.stderr or "").strip()[-400:])
    finally:
        tmp.unlink(missing_ok=True)
        if own:
            sound.unlink(missing_ok=True)
    return out


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    print(compose(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
