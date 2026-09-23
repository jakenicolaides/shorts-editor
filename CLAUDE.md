# CLAUDE.md

Automatic first cut for the daily puzzle-game shorts (Twixtle, Vowelsweeper). Jake
records one 9:16 OBS clip per game per day, this turns it into a posted-ready MP4 in
about 90 seconds, he steers it with plain-English notes, and approves it: the file goes to
Dropbox (the archive) and to the posting app, which tells the right person's phone when it
is time to post and hands them the video and the caption. Built 2026-09-14 to take editing from ~1.5h/day to ~30min. `README.md` has
the workflow and the cut rules; this file is the WHY and the invariants. Keep it short.

## Shape

Local only, on the M3 MacBook: Python + ffmpeg + mlx-whisper, a tiny stdlib HTTP server
on `localhost:8790` (`run.py`, or `~/Applications/Shorts Editor.app` which just opens
Terminal running it). No cloud, no database, no build step. One directory per job under
`work/<id>/` holds everything (raw copy, wav, transcript, params, cut, render, status).
`.venv` is Python 3.13 from Homebrew; `python3` on the PATH is not it.

**Nothing of the editor runs on the prod box: cutting and review are local.** A phone
REVIEW page on S3 was built and removed the same day (2026-09-14): review happens on the
Mac page and a second place to look added a step for nothing. The posting app (see
Posting, below) is a different thing and does live on the box, because it does what the
Mac cannot: notify a phone at a set time and hand it a file. It reuses that era's
`twixtle-shorts` bucket and `shorts-uploader` IAM user if they still exist; the editor's
`.env` still carries their keys only so `bin/server-setup.sh` over there can copy them to
the server once. The editor itself never uses them.
**The editor must run on a Mac with no access to the box** (Jake's partner and a colleague
will use it), so the one thing it needs from the games, a puzzle's scheduled date, is a
public HTTP call: `api/puzzle-date.php` in each game repo. It is a REVERSE lookup that
only answers a caller who already names the puzzle (both Twixtle words; 8+ Vowelsweeper
squares, all agreeing), so it needs no secret to hand out and cannot be used to read the
forward queue, which those sites guard. A token'd "list the schedule" endpoint was the
alternative and was rejected: it puts every future answer on each laptop that holds the
token. Both are live (2026-09-23) and the ssh fallback that bridged the gap is gone.

## Posting (2026-09-19)

**Posting stays manual and in-app, on purpose.** Posting through an API (Upload-Post) was
explored and dropped: Jake's judgement is that posts made by hand in the apps do better,
and driving the apps from outside breaks their terms. So the aim became removing every
step around the hand. That is `~/Documents/unseen_server/srv/www/sites/shorts.unseenforms.com`
(its own repo and CLAUDE.md): an installable phone page with push. This editor's part is
`poster.py`: on approve, after the Dropbox copy, it announces the puzzle, PUTs the render
to the URL it is given (a presigned S3 URL: this Mac holds no AWS keys) and confirms. The
app schedules it for the day AFTER the puzzle, Daily in the morning and Daily Hard in the
evening, and keeps one video per puzzle, so approving a re-edit replaces the last upload.

- **The job's name is the puzzle's identity.** `poster.puzzle_from_name` parses
  `<date> <puzzle> <daily|hard>` and that is what is sent: it is what the reviewer sees
  and can correct, and `title.py` wrote it from the schedule. A name that does not parse
  is refused BEFORE the Dropbox copy, so nothing is archived under a name that cannot be
  scheduled.
- **Dropbox first, then the schedule**: the archive must not depend on a server being up.
  Both halves overwrite, so pressing Approve again is always the fix.
- **Connected or not is two lines in `.env`** (`POSTER_URL`, `POSTER_TOKEN`, minted per
  computer in the app's Settings). Without them approve only saves to Dropbox, as before.
  The token can add videos as its user and nothing else.

**Prepare puzzles** (2026-09-24): one Chrome tab per scheduled puzzle for the next N days,
to record in one sitting. The games refuse to show a future puzzle to the public, so each
has a `record.php` that opens one only on a link signed with a secret the posting app
shares with them (`RECORD_LINK_SECRET`, in the three `.env.server` files on the box; the
posting app's `bin/record-secret.sh` writes it). The editor asks the posting app for the
links with its ordinary token (`poster.record_links`), so any editor can record and this
Mac never holds the secret. Twixtle opens in its admin test mode (the puzzle stashed in
the tab, no play recorded), Vowelsweeper as a `?b=` test board: the same surfaces Jake
records from by hand. Tabs are in day order, then game, Daily before Hard; the Days box
is capped at the shallowest ticked queue. Chrome is addressed by profile (`prepare.py`),
because Jake has several and the sign-ins live in one.

## Invariants

- **The LLM never touches media.** A note becomes a typed decision (`editor_llm.py`,
  `EditDecision`): knob changes as a flat name/value list, plus keep/cut ranges and
  start/end/solve pins. The same deterministic cutter (`cutlist.py`, pure, unit-tested
  inline) re-runs with the new params. **Keep the schema flat**: `Optional` fields each
  count against the API's structured-output complexity limit and it returned "Schema is
  too complex" at ~14 of them.
- **The name comes from the schedule, not from the model or from Whisper.** With no name
  typed, `title.py` reads the puzzle off a frame just after the solve (Twixtle shows start
  and end, Vowelsweeper every tile; the clip's last second when no tone was found) as a
  typed guess, then asks the game's schedule which puzzle that is. The name is
  `<date, year first> <puzzle> <difficulty>` (`2026-09-19 pros-towed hard`), where
  difficulty is the schedule's track: `daily`, or `hard` for `bonus` (Daily Hard to
  players). Date, spelling and track are the schedule's row; no match means no name and
  Jake types it. A match in the other game also corrects the job's game (and so the
  Dropbox folder and the solve tone): clips have been dropped under the wrong game. Whisper hears "prose to
  toad" for pros-towed, and the hand-typed dates were wrong about one time in three, which
  is why a typed name is also checked and the schedule's version offered. Dates in the
  queue derive from list order, so a reorder after recording changes the answer.
- **Cancel is killing a child process.** Every long stage is one (ffmpeg, and Whisper in
  its own interpreter via `transcribe_killable`, because a thread running Whisper cannot
  be stopped; the price is a model load per job). Files a killed stage was writing go to a
  temp name and are moved into place, so a resumed run never trusts a partial. A cancelled
  note is undone (params, cut, history); a cancelled first run resumes from Try again.
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
- **Every render is capped under 50 MiB** (`render.SHARE_LIMIT`, a VBV `-maxrate` computed from
  the cut's length). Chrome refuses to share a larger file from a page (blink
  `kMaxSharedFileBytes`), rejecting with a bare "Permission denied", and the phone app posts
  by sharing: on 2026-09-23 a 55 MB Vowelsweeper render would not post while a 39 MB Twixtle
  one did. Long takes get a lower bitrate rather than a refusal; a 279s take lands at ~47 MB.
  Approve refuses an oversized render and the panel says to re-render. A keyframe every
  second was tried on 2026-09-21 against Instagram cutting posts to 15s; it doubled file
  sizes and caused this, and the 15s was Instagram's Reels camera length setting. Keyframes
  are now every two seconds, which is what Instagram's own encodes use.
- **The 1:30 floor only loosens** (gap threshold, then fillers). It cannot invent length,
  so a short take flags `under_min_duration` and that is the answer.
- **The intake is the drop and nothing else** (2026-09-19; it was name, game, speed, clip).
  Every question asked before the drop was one the clip answers or one that can wait. The
  game comes from the schedule's match, then what the frame showed, then the solve tone;
  nobody picks it, because when it was picked it was sometimes picked wrong.
- **Speed is the reviewer's call per clip**, not detected: different speakers, different
  pace. It is made on the job's panel after the 1.0x first cut (almost every clip stays
  at 1.0x, so asking up front cost a step to save a re-render that rarely happens). A
  speed change re-runs the relax ladder, since the floor is post-speed; what the ladder
  loosened is kept in `relax.json` and put back first, so slowing down tightens the cut
  again. "Only loosens" below is about one pass, not about the job's history.
- **Filenames and folders are the archive's contract**: the job's name (worked out from
  the clip, or typed) becomes `<name>.mp4` in `Dropbox/twixtle/Dailies/` or
  `Dropbox/vowelsweeper/` by game. Save is refused with no name past the date. Delete
  removes `work/<id>/` only, never the Dropbox copy.

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
