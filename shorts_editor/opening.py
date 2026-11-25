"""Where the presenter starts on the puzzle. A video lives or dies in its first second or
two, and a take that opens on a hello, an introduction or a "welcome back" loses the viewer
before the puzzle is on screen (the presenters' guidelines say to start solving straight
away; this is the net under that). Claude reads the opening of the word-timed transcript
and names the first word that is about the puzzle; the cut then starts there, through the
same start_override a reviewer's note can set, so "Something to change?" can put it back.

It can only ever move the start LATER, to the start of a word that was really said, and by
no more than MAX_TRIM seconds. Anything else it returns, and any failure at all, trims
nothing: a video that opens on a hello is still a video, one that opens mid-solve is not."""
from typing import Optional
from pydantic import BaseModel, Field
import anthropic

from . import editor_llm

MAX_TRIM = 30.0     # never take more than this off the top
READ = 75.0         # how much of the take the model is shown
MIN_TRIM = 1.0      # a trim under this is not worth a model's judgement

SYSTEM = """You trim the opening of short vertical puzzle-game videos (TikTok style). A presenter records themselves solving a daily word puzzle and talking through it. The video must open on the puzzle itself, because viewers decide in the first second or two whether to keep watching.

You are given the opening of the raw recording as a numbered list of words with their start times. Name the index of the FIRST word the video should start on: the first word of the first sentence that gets on with THIS puzzle. Cut as close as you can: the viewer can already see which game it is, so anything that only announces it is wasted time.

About the puzzle, so keep it: stating the puzzle ("I've got to get from OVER to WEDDING in four moves"), reading what is on the board, a first idea, a first move, a reaction to the puzzle.
Not about the puzzle, so trim it: greetings, the presenter's name, "welcome back", naming the account or the day, how they are, what they are about to do in general ("today I'm going to be playing..."), a sentence that only names the game or which puzzle it is ("Today we're taking on the Twixtle Daily Hard", "This is today's Vowelsweeper"), a general explanation of who they are or what the channel is, an explanation of how the game works or what it is like ("it's like Minesweeper, but you flag vowels"), who the game is for, "let's get started", checking the recording is working.

Rules:
- A sentence that both greets and states the puzzle is kept from wherever its puzzle part starts, if that reads as a clean opening; otherwise keep the whole sentence.
- Start on the first word of a sentence or clause, never mid-phrase. The video must open on something that makes sense heard cold.
- A sentence that names the game AND states the puzzle in one breath ("Today's Twixtle is OVER to WEDDING") is kept whole: it cannot be cut cleanly. A sentence that names the game and stops there is trimmed, and the video starts on the next one ("We're going from OARS to MOON").
- An explanation of the game's rules before the solve begins is trimmed. People learn a game by watching it played, and nobody scrolling wants the rules read to them first. (Explaining a move while making it is the solve itself: that is never the opening you are trimming.)
- When the recording already opens on the puzzle, or you are unsure, answer 0. Trimming nothing is always safe."""


class Opening(BaseModel):
    index: int = Field(description="Index of the first word to keep. 0 keeps everything.")
    why: str = Field(description="One short sentence: what was trimmed, quoting it, or why nothing was.")


def find(words: list, game: str, title: Optional[str]) -> dict:
    """{"at": source seconds or None, "why": str}. Never raises."""
    try:
        return _find(words, game, title)
    except Exception as e:
        return {"at": None, "why": f"not checked: {e}"}


def _find(words, game, title):
    if not words:
        return {"at": None, "why": "no words"}
    t0 = words[0]["s"]
    head = [w for w in words if w["s"] - t0 <= READ]
    if len(head) < 8:
        return {"at": None, "why": "too little said to judge"}
    editor_llm._ensure_key()
    listing = "\n".join(f"{i:3d}  {w['s']:6.1f}  {w['w']}" for i, w in enumerate(head))
    user = f"""Game: {game}. {f'This puzzle is named: {title}.' if title else ''}

Opening of the recording (index, start in seconds, word):
{listing}"""
    resp = anthropic.Anthropic().messages.parse(
        model=editor_llm.MODEL, max_tokens=2000, system=SYSTEM,
        messages=[{"role": "user", "content": user}], output_format=Opening,
    )
    if resp.stop_reason == "refusal":
        return {"at": None, "why": "declined"}
    o = resp.parsed_output
    if not (0 < o.index < len(head)):
        return {"at": None, "why": o.why}
    trim = head[o.index]["s"] - t0
    if trim < MIN_TRIM:
        return {"at": None, "why": o.why}
    if trim > MAX_TRIM:
        return {"at": None, "why": f"would take {trim:.0f}s off the top, over the {MAX_TRIM:.0f}s limit: {o.why}"}
    return {"at": round(head[o.index]["s"], 2), "why": o.why, "trimmed": round(trim, 1)}
