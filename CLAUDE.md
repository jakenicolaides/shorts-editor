# CLAUDE.md

Automatic first cut for the daily puzzle-game shorts (Twixtle, Vowelsweeper, and Spelling
Garden practice since 2026-10-14). Jake
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

## Recorder takes (2026-11-19)

**A take can arrive as two tracks instead of one clip.** The ambassador recorder (a page a
presenter records in; the plan is `AMBASSADORS.md` in the Twixtle repo) records the game and
the webcam SEPARATELY, the game as the browser tab cropped to it at whatever size it had on
that person's screen. `compose.py` stacks them into the frame OBS always produced (1080x1920,
webcam in the top 762, one mixed sound track) and **everything after that is unchanged**: it
is the job's `input.mp4`, and the cut, the title read, the solve tone and the render see an
ordinary clip. Checked against a real recording split in two: the same seven segments to
within 0.04s, the same solve time, 25s of joining on a five minute take.

- **Stacking is ours, not the browser's**, so the layout can change without anyone
  re-recording and a weak laptop is not asked to composite while someone plays.
- **The mix is a plain sum** (`amix normalize=0`): the default halves each input, which
  drops the voice 6 dB before the loudness pass ever sees it.
- **The recorder knows which puzzle it opened**, so the game is passed in, never guessed from
  the sound. It does not know the puzzle's WORDS, so the name is still read off the clip and
  checked against the schedule. Something that does know them passes a title, and then
  nothing asks the model (`create_from_tracks` writes `title.json` itself).
- **The recorder is a page on another address**, so the server names it
  (`SHORTS_RECORDER_ORIGINS`, default the test page at `http://localhost:8795`) and answers
  only that address across origins. Routes are in `server.py`'s header: two PUTs and a start.
- **Not done yet**: the voice and the game's sound arrive apart and are mixed before
  anything listens. Transcribing the microphone alone and finding the chime in the game's
  sound alone would both be cleaner than what OBS ever gave.

## The editor on Linux (2026-11-20)

**The editor runs on Linux, for the server that will edit ambassadors' takes.** The only
Mac-only part was the program that runs Whisper. `transcribe.py` now has two: mlx-whisper on
Apple Silicon, faster-whisper everywhere else, picked by the machine (`backend()`;
`SHORTS_WHISPER=mlx|faster` forces one). **Same model, same weights, same settings, same
output shape**, so nothing after transcription knows which ran, and a Mac's own output is
byte for byte what it was. `Dockerfile` builds it (model baked in, 4.7 GB image),
`requirements-linux.txt` is its package list, and `worker.py` is the entry point: two tracks
in, a finished job folder out, no page.

- **Measured, not assumed.** Five past takes cut from each runner's transcript: identical
  segments on all five, boundaries within 0.1s on four, and on the fifth the Linux runner
  missed the clip's last word ("cool", alone, two seconds after the talking stopped), ending
  the edit 2.5s early. A 4:17 take edited whole inside Linux: the same 14 segments as the Mac
  to within 0.02s, the same solve time.
- **`float32`, never `int8`.** int8 is the usual advice for CPU and is quicker, and on the
  same take it heard different words and punctuated differently. `SHORTS_WHISPER_COMPUTE`
  exists to measure that again, not to turn it on.
- **`beam_size=1`.** mlx-whisper decodes greedily and faster-whisper defaults to a beam of
  5; left alone, the two would disagree for a reason that has nothing to do with the machine.
- **The sound goes in as samples, not a path.** Given a path faster-whisper decodes it with
  PyAV, and the PyAV that pip resolves today breaks that call.
- **It runs on Lambda, timed 2026-11-20** (function `shorts-editor`, eu-west-1, ARM, 10 GB
  which is what buys its 6 processors, 15 minute limit; `lambda_handler.py` is the entry,
  set on the function, and the image is `shorts-editor` in ECR). A 4:17 take, warm: **4:18**
  (join 51s, speech 127s, render 80s), about 3.5 cents, and the same 14 segments as the Mac
  to within 0.02s. A 41 second take: 69s warm.
- **A cold start costs about 50 seconds, and the model comes from S3, not the image.**
  Lambda reads an image a block at a time as it is touched, and the 1.6 GB of weights read
  that way made speech take 115s cold against 29s warm (202s the first time a new image
  ran). `lambda_handler._fetch_model` copies the model from `models/` in the bucket to /tmp
  once per container (19s) and points the runner at it; a 41 second take then runs in 1:59
  cold against 1:09 warm. The image keeps its own copy as the fallback and for running the
  image by hand. **A new model means both**: rebuild the image AND replace `models/`.
- **The function's role reaches `takes/` in `twixtle-shorts`, reads `models/` there, and
  writes its own logs, nothing else.** A take is `takes/<id>/game.*` and `cam.*`; the edit and the job's small files come
  back under `takes/<id>/edit/`. **Not done**: nothing calls the function yet, nothing
  expires `takes/`, a note has no way in, and the name wants a title passed in (no key for
  the model or the posting app is on the function).
- **Rebuild and ship**: `docker build -t shorts-editor .`, tag and push to the ECR
  repository, then `aws lambda update-function-code --function-name shorts-editor
  --image-uri <repo>:latest`. In zsh write `${ACCT}` in braces: `$ACCT:role` is read as a
  modifier and eats the colon.

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
- **A new Mac is one line** (2026-09-24): `curl -fsSL https://shorts.unseenforms.com/install.sh
  | bash`, served by the posting app. It installs what is missing, clones this repo, asks for
  the two `POSTER_` lines, fetches the speech model and builds the app; run again to update.
  The Anthropic key is no longer pasted: the posting app hands its shared key to any editor
  token (`editor_llm._ensure_key`, after this `.env`, before Jake's estate fallbacks), so one
  key serves everyone and is revoked in one place. The installer needs the repo readable.
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
records from by hand. Which puzzles is the poster's pick, from a CHECKLIST (2026-09-26): one
row per puzzle date from yesterday on, one box per game and track, each labelled by when its
video POSTS, since that is how the recording is planned ("it is 13:43, I have missed the
08:00 post; I want the 18:00 one and tomorrow's two"). Missed slots are greyed, already
recorded ones marked; the posting app's `record_plan` (tested) pre-ticks the earliest still
wanted per track. A "days" number was tried first and could not express that afternoon.
Tabs open in date order, then game, Daily before Hard. Chrome is addressed by profile
(`prepare.py`), because Jake has several and the sign-ins live in one.

## Invariants

- **The LLM never touches media.** A note becomes a typed decision (`editor_llm.py`,
  `EditDecision`): knob changes as a flat name/value list, plus keep/cut ranges and
  start/end/solve pins. The same deterministic cutter (`cutlist.py`, pure, self-test:
  `python -m shorts_editor.cutlist`) re-runs with the new params. **Keep the schema flat**: `Optional` fields each
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
- **Nothing over 3:00 is approved** (2026-10-01): Instagram takes reels up to three minutes,
  and six of September's approved videos ran longer (up to 5:11, all Hard solves). Approve
  checks the rendered FILE against `render.LENGTH_LIMIT`, posting app or not; the fit aims 2s
  under (`Params.max_duration`, room for the edge snapping). Over it, pauses go first
  (`max_gap` 3, then 2), then speed, just enough in 0.05 steps, up to 1.5x (Jake's cap). Pauses
  alone cannot do it: on the longest takes the talk itself runs past 3:00. Of September's
  nine over-length takes eight now fit untouched; the ninth (5:38) is 3:25 at 1.5x, and there
  the reviewer's note makes the editor cut talk. A note's result is not refitted, so the
  editor is told the limit and must stay under it. The limit is per game
  (`render.length_limit`): Spelling Garden never goes to Instagram, so it has none
  (`Params.max_duration` None) and only the 1:30 floor applies.
- **Jobs run in parallel; Whisper runs one at a time** (`transcribe._one_at_a_time`,
  2026-09-24). Serialising whole jobs was tried first and made three of four dropped clips
  look stuck. Transcription alone is taken in turn: each run loads the 1.5 GB model and
  they share one GPU, so overlapping them is no faster and can exhaust memory. Cutting and
  rendering overlap freely. A cancel while waiting for the turn is honoured at once.
- **The intake is the drop and nothing else** (2026-09-19; it was name, game, speed, clip).
  Every question asked before the drop was one the clip answers or one that can wait. The
  game comes from the schedule's match, then what the frame showed, then the solve tone;
  nobody picks it, because when it was picked it was sometimes picked wrong.
- **Speed is the reviewer's call per clip**, not detected: different speakers, different
  pace. The one exception is the 3:00 ceiling (above), which speeds a long take up as far
  as it must. It is made on the job's panel after the 1.0x first cut (almost every clip
  stays at 1.0x, so asking up front cost a step to save a re-render that rarely happens).
  A speed change re-runs the fit (`cutlist.build_to_fit`), since both limits are
  post-speed; what it changed is kept in `relax.json` and put back first, so slowing down
  tightens the cut again, and a speed too slow for 3:00 is raised to the slowest that fits.
  "Only loosens" above is about one pass, not about the job's history.
- **Spelling Garden is practice, not a puzzle** (2026-10-14): Jake reviews his daily words,
  so there is no schedule to ask, no solve tone, nothing for Prepare puzzles to open. The
  frame read only says which game it is (`title.PRACTICE`); the name is `<date>
  spelling-garden`, the date taken from the dropped file's name (OBS writes it), else the
  drop day. With no solve the cut ends on the last word, as when a tone is missed, minus the
  `no_solve_detected` flag. It does not go to Instagram, so no 3:00 ceiling: a raw 8:45
  session comes out at 8:10, 1.0x. The 50 MiB cap still holds (about 0.8 Mbps at that
  length; the game's flat art and the webcam stand it). The posting app takes it as a third
  game on the daily track (`poster.puzzle_from_name`), so it posts the morning after its
  recording, TikTok only (that app's CLAUDE.md). `<date>.mp4` names in its Dropbox folder are
  Jake's own, and the suffix keeps an approve from overwriting one.
- **Filenames and folders are the archive's contract**: the job's name (worked out from
  the clip, or typed) becomes `<name>.mp4` in `Dropbox/twixtle/Dailies/`,
  `Dropbox/vowelsweeper/` or `Dropbox/spelling-garden/` by game. Save is refused with no name past the date. Delete
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
