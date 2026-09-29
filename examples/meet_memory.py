from __future__ import annotations

import dataclasses
import enum

MEET_CONTEXT_WINDOW_S = 300.0


class MeetRole(enum.Enum):
    PARTICIPANT = "user"
    BOT = "assistant"
    NOTE = "system"


@dataclasses.dataclass(frozen=True)
class MeetTurn:
    vat: float
    vspeaker: str
    vtext: str
    vrole: MeetRole = MeetRole.PARTICIPANT

    def line(self) -> str:
        return f"[{self.vspeaker}] {self.vtext}"


@dataclasses.dataclass
class MeetMemory:
    """The meeting as the bot remembers it: running notes plus every turn not yet folded into them.

    Turns older than the window are folded into the notes by a model, so the verbatim part stays
    bounded however long the meeting runs, and nothing leaves the verbatim part until the notes
    that replace it exist.
    """

    vnotes: str = ""
    vturns: list[MeetTurn] = dataclasses.field(default_factory=list)

    def add(self, vturn: MeetTurn) -> None:
        self.vturns.append(vturn)

    def expired(self, vnow: float) -> list[MeetTurn]:
        return [vturn for vturn in self.vturns if vturn.vat < vnow - MEET_CONTEXT_WINDOW_S]

    def fold(self, vfolded: list[MeetTurn], vnotes: str) -> None:
        self.vnotes = vnotes
        self.vturns = [vturn for vturn in self.vturns if vturn not in vfolded]

    def transcript(self) -> str:
        return "\n".join(vturn.line() for vturn in self.vturns)


def fold_prompt(vnotes: str, vturns: list[MeetTurn]) -> str:
    vlines = "\n".join(vturn.line() for vturn in vturns)
    return (
        "You keep running notes of a meeting for an assistant who attends it. Merge the new transcript "
        "lines into the notes. Keep every decision, date, number, owner, open question and request made "
        "to the assistant, attributed to who said it; drop small talk. Reply with the updated notes only, "
        "as short bullet points.\n\n"
        f"Current notes:\n{vnotes or '(none yet)'}\n\n"
        f"New transcript lines:\n{vlines}"
    )


def background_brief(vgoal: str, vrequester: str, vmemory: MeetMemory) -> str:
    """Everything the background model knows: an explicit brief, never the bot's own chat history."""

    return (
        f"{vrequester} asked, during a meeting: {vgoal}\n\n"
        f"Meeting notes so far:\n{vmemory.vnotes or '(none)'}\n\n"
        f"Recent transcript:\n{vmemory.transcript() or '(none)'}\n\n"
        "Do the work and reply with the answer only, in at most five short sentences that can be read "
        "aloud. If the meeting does not contain enough to answer, say exactly what is missing."
    )
