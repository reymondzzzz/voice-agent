from __future__ import annotations

import dataclasses
import difflib
import re

FOLLOW_UP_WINDOW_S = 12.0
NAME_MATCH_RATIO = 0.8

_WORD = re.compile(r"\w+", re.UNICODE)
# People address the bot in their own script: "Карен" has to match "Karen".
_CYRILLIC_TO_LATIN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
})


def _words(vtext: str) -> list[str]:
    return _WORD.findall(vtext.casefold().translate(_CYRILLIC_TO_LATIN))


# Acknowledgements, not questions. A word count cannot tell them apart: "Почему?" is one word and deserves an answer.
_FILLERS = frozenset(
    " ".join(_words(vfiller))
    for vfiller in ("ok", "okay", "mhm", "hmm", "uh huh", "thanks", "thank you", "got it", "угу", "ага", "ок", "окей", "понятно", "ясно", "спасибо", "хорошо")
)


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


@dataclasses.dataclass
class MeetAddressing:
    """Decides whether a meeting turn is meant for the bot; silence is the default.

    Naming the bot engages whoever said it. That speaker may then follow up without the name until
    the window lapses. Anyone else speaking, or the engaged speaker naming another participant, hands
    the floor back to the humans: a reply nobody asked for costs more than one that needs the name.
    """

    vbot_name: str
    vengaged_speaker: str = ""
    vengaged_until: float = 0.0
    vseen_speakers: set[str] = dataclasses.field(default_factory=set)

    def is_addressed(self, vspeaker: str, vtext: str, vnow: float) -> bool:
        self.vseen_speakers.add(vspeaker)
        if mentions_name(vtext, self.vbot_name):
            self.engage(vspeaker, vnow)
            return True
        vfollow_up = vspeaker == self.vengaged_speaker and vnow < self.vengaged_until
        if not vfollow_up or self._names_another_participant(vspeaker, vtext):
            self.vengaged_speaker = ""
            return False
        if " ".join(_words(vtext)) in _FILLERS:
            return False
        self.engage(vspeaker, vnow)
        return True

    def bot_finished_speaking(self, vnow: float) -> None:
        if self.vengaged_speaker:
            self.vengaged_until = vnow + FOLLOW_UP_WINDOW_S

    def engage(self, vspeaker: str, vnow: float) -> None:
        self.vengaged_speaker = vspeaker
        self.vengaged_until = vnow + FOLLOW_UP_WINDOW_S

    def _names_another_participant(self, vspeaker: str, vtext: str) -> bool:
        return any(
            mentions_name(vtext, vother.split()[0])
            for vother in self.vseen_speakers
            if vother != vspeaker and vother.strip()
        )
