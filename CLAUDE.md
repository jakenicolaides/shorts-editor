# CLAUDE.md

Automatic first cut for the daily puzzle-game shorts (Twixtle, Vowelsweeper). Jake
records one 9:16 OBS clip per game per day, this turns it into a posted-ready MP4 in
about 90 seconds, he steers it with plain-English notes, saves to Dropbox, posts from
the phone. Built 2026-09-14 to take editing from ~1.5h/day to ~30min. `README.md` has
the workflow and the cut rules; this file is the WHY and the invariants. Keep it short.

## Shape

Local only, on the M3 MacBook: Python + ffmpeg + mlx-whisper, a tiny stdlib HTTP server
on `localhost:8790` (`run.py`, or `~/Applications/Shorts Editor.app` which just opens
Terminal running it). No cloud, no database, no build step. One directory per job under
`work/<id>/` holds everything (raw copy, wav, transcript, params, cut, render, status).
`.venv` is Python 3.13 from Homebrew; `python3` on the PATH is not it.

**Nothing runs on the Twixtle prod box and nothing should.** A phone review page on S3
was built and removed the same day (2026-09-14): the phone gets the file from Dropbox,
so review happens on the Mac page and S3 added a step for nothing. The `twixtle-shorts`
bucket and `shorts-uploader` IAM user may still exist; nothing here uses them.

## Invariants

- **The LLM never touches media.** A note becomes a typed decision (`editor_llm.py`,
  `EditDecision`): knob changes as a flat name/value list, plus keep/cut ranges and
  start/end/solve pins. The same deterministic cutter (`cutlist.py`, pure, unit-tested
  inline) re-runs with the new params. **Keep the schema flat**: `Optional` fields each
  count against the API's structured-output complexity limit and it returned "Schema is
  too complex" at ~14 of them.
- **Edges come from the audio, not from Whisper.** Word timestamps are late at onsets and
  early at offsets by up to ~0.3s, and the brief is no air either side. `pipeline.recut`
  snaps the first and last cut to the speech envelope (`transcribe.snap_start/snap_end`,
  threshold = 20 dB under the 90th-percentile frame level; a percentile-30 floor was
  tried and sat inside room noise). Pinned starts snap too: a pin is approximate.
- **Whisper lies over silence.** It emits phantom phrases ("Thank you.") and lone
  punctuation tokens with real timestamps. Both are dropped in `transcribe.py` (energy
  gate 20 dB under the median word, and no-alphanumeric tokens). Anything kept as content
  over silence becomes a held shot of nothing.
- **False starts are dropped by rule, not by the LLM**: a phrase restarted after a pause
  with the same first three words loses the first attempt, looking past a short aside
  ("let's start that again"), and bare restart phrases go too. This is the most common
  correction Jake made by hand.
- **Solve tone templates** (`templates/<game>.wav`, 16k mono) are clipped from real
  recordings; detection is normalised spectrogram cross-correlation, threshold 0.55,
  measured true matches 0.76-0.88 against a worst false peak of 0.50. The detected time is
  the chime START. The video ends after the last word within 8s of it; nothing after.
- **The 1:30 floor only loosens** (gap threshold, then fillers). It cannot invent length,
  so a short take flags `under_min_duration` and that is the answer.
- **Speed is Jake's call per clip**, not detected: different speakers, different pace.
- **Filenames and folders are the archive's contract**: the name typed at upload becomes
  `<name>.mp4` in `Dropbox/twixtle/Dailies/` or `Dropbox/vowelsweeper/` by game. The
  page refuses an upload with no name past the date.

## Environment traps

- `~/.zshrc` exports `DYLD_LIBRARY_PATH=/opt/homebrew/lib`, which makes the venv's MLX
  extension load Homebrew's `libmlx` (wrong version, missing symbol, Whisper import
  fails). `_env.clean_dyld()` re-execs without any `DYLD_*` at the top of `run.py` and
  the CLI. Do not remove it because "it works in my shell".
- macOS TCC: processes under the Claude app cannot read or list
  `~/Library/CloudStorage/Dropbox` (writes from a Terminal-launched server work). Test
  clips live in `fixtures/` (gitignored), copied in by Finder.
- The server watchdog quits the process and closes its own Terminal window when the
  page's pings stop; it never quits mid-job. `page.html` is read per request; Python
  changes need a restart.
- The Anthropic key is resolved from `.env`, else the Twixtle `.env.local`, else
  prospector's `.env.server` (`editor_llm._ensure_key`). Credentials never pass through
  chat: Jake runs any key-handling command in his own terminal.
