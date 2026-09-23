# shorts-editor

Automatic first cut for the daily puzzle-game shorts. Drop the raw OBS clip in,
get back a cut, normalised, TikTok-ready MP4, talk to the editor in plain
English until it is right, save it to Dropbox, post from the phone.

## Daily workflow

1. Double-click Shorts Editor.app (or `.venv/bin/python run.py`); the page opens
   at http://localhost:8790
2. Drop the OBS clip on the page. That is the whole intake. About 90s later it
   is cut at 1.0x, the game is identified, and the job is named
   `<date> <puzzle> <daily|hard>`, e.g. `2026-09-19 pros-towed hard`: the puzzle
   is read off the solved screen and looked up in the game's schedule (a public
   API call, nothing to set up). If it cannot be named, the panel asks for the
   name before saving.
3. Watch the cut on the page. Type notes to the editor if it needs changes
   (it re-cuts and re-renders, about 2 min). Speed buttons and Rename are on the
   same panel; a speed change is a re-render. Cancel stops whatever is running
   (a first run picks back up with Try again); Delete removes the job from this
   Mac and leaves any copy already saved to Dropbox.
4. Approve. `<name>.mp4` lands in Dropbox (`twixtle/Dailies/` or `vowelsweeper/`)
   as the archive, and the video is uploaded to the posting app and scheduled for
   the day after its puzzle (Daily in the morning, Daily Hard in the evening, UK
   time). At that time the phone of whoever posts that game is notified; the app
   there has the video, a Share button per platform and the caption ready to
   paste. Approving again after a re-edit replaces the scheduled video.
   Not connected to the posting app (no `POSTER_URL` in `.env`)? Then this step
   only saves to Dropbox, as it always did.

Closing the page quits the server and its Terminal window.

## Run

    .venv/bin/python run.py                        # http://localhost:8790

or from the command line:

    .venv/bin/python -m shorts_editor.cli run fixtures/twixtle-2026-09-12.mp4 --speed 1.15 --game twixtle
    .venv/bin/python -m shorts_editor.cli note <job_id> "end a bit later after the solve"

## Setup (a new Mac)

Needs an Apple Silicon Mac (Whisper runs on MLX) and Homebrew.

    brew install python@3.13 ffmpeg
    git clone <this repo> ~/Documents/shorts-editor && cd ~/Documents/shorts-editor
    /opt/homebrew/bin/python3.13 -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env

Then in `.env`: `ANTHROPIC_API_KEY` (given to you; used for the notes editor and for
reading the puzzle off a frame), and the two `POSTER_` lines from the posting app
(Settings > your row > Editor token, minted once per computer). `ARCHIVE_DIR` stays
empty unless this Mac has the shared Dropbox.

    .venv/bin/python run.py         # opens http://localhost:8790

The first run downloads the Whisper model (about 1.5 GB), so the first clip takes a
few minutes longer.

## The rules (shorts_editor/cutlist.py)

* start on the first real word, snapped to the audio onset (no lead-in)
* a phrase restarted after a pause loses its first attempt (false start)
* any stretch over 5s with no real word (silence, or only um/err) is removed,
  leaving 0.5s of air either side; shorter pauses are kept whole
* end 0.15s after the last word said within 8s of the solve chime, snapped to
  the audio; the 15s before the solve are never cut
* under 1:30 after the speed-up: loosen the gap threshold (7/10/15s), then keep
  fillers; still short is flagged on the review page
* speed is chosen on the job after the first cut (1.0x until then) and applied
  uniformly (pitch preserved); the 1:30 rule is re-applied at the new speed
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
      title.py        frame -> puzzle -> scheduled date -> name (matchers self-test: python -m shorts_editor.title)
      cancel.py       kill the running child process, raise Cancelled
      pipeline.py     job dirs under work/<id>/
      server.py       local drop UI
      publish.py      approve: save to Dropbox, then schedule
      poster.py       upload to the posting app (shorts.unseenforms.com)
    templates/        solve tone wavs
    fixtures/         raw test clips (gitignored)
