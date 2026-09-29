from __future__ import annotations

import dataclasses
import enum

MEET_CONTEXT_WINDOW_S = 600.0


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
    """The last `MEET_CONTEXT_WINDOW_S` of the meeting as labelled text; older turns live only in the transcript file."""

    vturns: list[MeetTurn] = dataclasses.field(default_factory=list)

    def add(self, vturn: MeetTurn) -> None:
        self.vturns.append(vturn)

    def forget_before(self, vnow: float) -> None:
        self.vturns = [vturn for vturn in self.vturns if vturn.vat >= vnow - MEET_CONTEXT_WINDOW_S]

    def transcript(self) -> str:
        return "\n".join(vturn.line() for vturn in self.vturns)


def background_brief(vgoal: str, vrequester: str, vmemory: MeetMemory) -> str:
    """Everything the background model knows: an explicit brief, never the speech model's own session."""

    return (
        f"{vrequester} asked, during a meeting: {vgoal}\n\n"
        f"Meeting transcript, last {int(MEET_CONTEXT_WINDOW_S // 60)} minutes:\n{vmemory.transcript() or '(none)'}\n\n"
        "Do the work and reply with the answer only, in at most five short sentences that can be read "
        "aloud. If the meeting does not contain enough to answer, say exactly what is missing."
    )
