# shorts-editor

Automatic first cut for the daily puzzle-game shorts. Drop the raw OBS clip in,
get back a cut, normalised, TikTok-ready MP4, talk to the editor in plain
English until it is right, save it to Dropbox, post from the phone.

## Daily workflow

1. Double-click Shorts Editor.app (or `.venv/bin/python run.py`); the page opens
   at http://localhost:8790
2. Name (date plus puzzle), game, speed, then drop the OBS clip. About 90s.
3. Watch the cut on the page. Type notes to the editor if it needs changes
   (it re-cuts and re-renders, about 2 min).
4. Save to Dropbox: `<name>.mp4` lands in `twixtle/Dailies/` or `vowelsweeper/`.
   Post it from the Dropbox app on the phone.

Closing the page quits the server and its Terminal window.

## Run

    .venv/bin/python run.py                        # http://localhost:8790

or from the command line:

    .venv/bin/python -m shorts_editor.cli run fixtures/twixtle-2026-09-12.mp4 --speed 1.15 --game twixtle
    .venv/bin/python -m shorts_editor.cli note <job_id> "end a bit later after the solve"

## Setup

    /opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env    # fill in keys

ffmpeg from Homebrew. Whisper runs locally (mlx-whisper, large-v3-turbo, first
run downloads the model).

## The rules (shorts_editor/cutlist.py)

* start on the first real word, snapped to the audio onset (no lead-in)
* a phrase restarted after a pause loses its first attempt (false start)
* any stretch over 5s with no real word (silence, or only um/err) is removed,
  leaving 0.5s of air either side; shorter pauses are kept whole
* end 0.15s after the last word said within 8s of the solve chime, snapped to
  the audio; the 15s before the solve are never cut
* under 1:30 after the speed-up: loosen the gap threshold (7/10/15s), then keep
  fillers; still short is flagged on the review page
* speed is chosen at drop time and applied uniformly (pitch preserved)
* audio: two-pass loudnorm to -14 LUFS, -1 dBTP
* video: 1080x1920 H.264 high, CRF 18, AAC 192k 48kHz, faststart

## Solve tone templates

`templates/<game>.wav` is the game's solved sound clipped from a real
recording (mono 16k). Make one from a finished job:

    .venv/bin/python -m shorts_editor.cli template <job_id> twixtle <start_s> <end_s>

Detection is normalised spectrogram cross-correlation; threshold in
solvetone.py.

## Notes loop

A note goes to Claude with the transcript timeline, the current parameters
and the current keep list. It can only move the cut knobs or add keep/cut
ranges (editor_llm.py, strict schema), then the job re-cuts and re-renders.

## Layout

    shorts_editor/
      transcribe.py   whisper words + filler tagging
      cutlist.py      pure cut-list rules (tested)
      solvetone.py    solve tone detection + template maker
      render.py       ffmpeg graph, loudness passes, encode
      editor_llm.py   note -> typed decision
      pipeline.py     job dirs under work/<id>/
      server.py       local drop UI
      publish.py      save to Dropbox
    templates/        solve tone wavs
    fixtures/         raw test clips (gitignored)
