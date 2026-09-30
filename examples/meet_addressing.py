from __future__ import annotations

import dataclasses
import difflib
import re
from collections.abc import Awaitable, Callable

NAME_MATCH_RATIO = 0.8

_WORD = re.compile(r"\w+", re.UNICODE)
# People address the bot in their own script: "Карен" has to match "Karen".
_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})
_RESPOND = re.compile(r"\bRESPOND\b")

ADDRESSEE_PROMPT = """You decide whether the latest line in a meeting transcript is said to {name}, an AI assistant attending the meeting, or to the other people.

The transcript comes from speech recognition over a video call. The start of an utterance is often clipped, so a first word missing its first sound or two, or a similar-sounding word, can be a mangled "{name}". Lines marked [{name}] are the assistant's own replies.

Say it is for {name} when the line names {name}, or continues an exchange with {name}: a follow-up or reaction to what {name} just said, a question aimed at the assistant, or a request for the assistant to do something. Say it is not when it is talk between the participants, addresses someone else by name, is a filler or acknowledgement (угу, ok, thanks), is a sound check, or is too garbled to be meant for anyone. Greetings and small talk ("как дела?", "how are you?") are for the people unless they name {name} or answer something {name} just said.

Transcript, oldest first:
{transcript}

Latest line, from {speaker}: {text}

Reply with exactly one word: RESPOND if the latest line is for {name}, IGNORE otherwise."""

AddresseeJudge = Callable[[str], Awaitable[str]]


def _words(vtext: str) -> list[str]:
    return _WORD.findall(vtext.casefold().translate(_CYRILLIC_TO_LATIN))


def mentions_name(vtext: str, vname: str) -> bool:
    """Fuzzy, because STT spells an unusual bot name a new way every call, and glues or splits its words."""

    vname_words = _words(vname)
    vtext_words = _words(vtext)
    vtarget = "".join(vname_words)
    return any(
        difflib.SequenceMatcher(None, "".join(vtext_words[vstart : vstart + vspan]), vtarget).ratio() >= NAME_MATCH_RATIO
        for vspan in range(1, len(vname_words) + 1)
        for vstart in range(len(vtext_words) - vspan + 1)
    )


def addressee_prompt(vbot_name: str, vtranscript: str, vspeaker: str, vtext: str) -> str:
    return ADDRESSEE_PROMPT.format(name=vbot_name, transcript=vtranscript or "(nothing yet)", speaker=vspeaker, text=vtext)


def parse_addressee(vreply: str) -> bool:
    """Anything but an explicit yes is a no: speaking when not asked costs more than staying quiet."""

    vupper = vreply.upper()
    return bool(_RESPOND.search(vupper)) and "IGNORE" not in vupper


@dataclasses.dataclass
class MeetAddressing:
    """Decides whether a meeting turn is meant for the bot; silence is the default.

    The name is the fast path. Everything else is judged from the dialogue by the model, because only
    the context tells "Почему?" after Karen's answer from "Почему?" between two colleagues, and a clipped
    "Арон, который час?" from a question to the room.
    """

    vbot_name: str
    vjudge: AddresseeJudge
    vengaged_speaker: str = ""

    async def is_addressed(self, vspeaker: str, vtext: str, vtranscript: str) -> bool:
        vaddressed = mentions_name(vtext, self.vbot_name) or parse_addressee(await self.vjudge(addressee_prompt(self.vbot_name, vtranscript, vspeaker, vtext)))
        if vaddressed:
            self.engage(vspeaker)
        return vaddressed

    def engage(self, vspeaker: str) -> None:
        self.vengaged_speaker = vspeaker
