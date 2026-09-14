# shorts-editor

Automatic first cut for the daily puzzle-game shorts. Drop the raw OBS clip in,
get back a cut, normalised, TikTok-ready MP4, talk to the editor in plain
English until it is right, approve, and it lands on S3 (for the phone) and in
Dropbox (archive).

## Daily workflow

1. `.venv/bin/python run.py` from your own terminal (Terminal.app, so it can
   write to Dropbox), open http://localhost:8790
2. Drop the OBS clip in, pick the speed, wait ~90s
3. Watch the cut on the page. Type notes to the editor if it needs changes
   (it re-cuts and re-renders, ~2 min). Or press "Send for review on phone".
4. On the phone: the review link (copy it from the page, or set IMESSAGE_TO in
   .env and it is iMessaged to you). Approve, or send notes from there too.
5. Approve archives the final to `~/Library/CloudStorage/Dropbox/shorts/<date>/`
   and "Download to phone" on the review page saves the MP4 to the phone.

The S3 copy expires after 14 days on its own.

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

* start at the first real word (minus 0.3s)
* any stretch over 5s with no real word (silence, or only um/err) is removed,
  leaving 0.5s of air either side; shorter pauses are kept whole
* end at the solve tone plus 2.5s; the 15s before the solve are never cut
* under 1:30 after the speed-up: loosen the gap threshold (7/10/15s), then keep
  fillers, then lengthen the tail; still short is flagged on the review page
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
      publish.py      approve: Dropbox archive + S3 + prod register
    templates/        solve tone wavs
    fixtures/         raw test clips (gitignored)
