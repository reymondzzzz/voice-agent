from __future__ import annotations

import dataclasses
import enum

from voice_agent.correlation import Correlation


class RealtimeRole(enum.Enum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclasses.dataclass(frozen=True, slots=True)
class RealtimeEventBase:
    vcorrelation: Correlation


@dataclasses.dataclass(frozen=True, slots=True)
class UserSpeechStarted(RealtimeEventBase):
    pass


@dataclasses.dataclass(frozen=True, slots=True)
class UserSpeechStopped(RealtimeEventBase):
    vspeech_duration_s: float = 0.0


@dataclasses.dataclass(frozen=True, slots=True)
class UserTranscriptPartial(RealtimeEventBase):
    vtext: str = ""
    vrevision: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class UserTranscriptFinal(RealtimeEventBase):
    vtext: str = ""
    vrevision: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class AssistantSpeechStarted(RealtimeEventBase):
    vresponse_id: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class AssistantSpeechStopped(RealtimeEventBase):
    vresponse_id: str = ""
    vcompleted: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class AssistantTranscript(RealtimeEventBase):
    vtext: str = ""
    vresponse_id: str = ""
    vfinal: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class RealtimeToolCallRequested(RealtimeEventBase):
    vtool_call_id: str = ""
    vtool_name: str = ""
    varguments: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class RealtimeInterrupted(RealtimeEventBase):
    vreason: str = "barge_in"
    vresponse_id: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class RealtimeSessionError(RealtimeEventBase):
    vmessage: str = ""
    vrecoverable: bool = True


RealtimeEvent = (
    UserSpeechStarted
    | UserSpeechStopped
    | UserTranscriptPartial
    | UserTranscriptFinal
    | AssistantSpeechStarted
    | AssistantSpeechStopped
    | AssistantTranscript
    | RealtimeToolCallRequested
    | RealtimeInterrupted
    | RealtimeSessionError
)


@dataclasses.dataclass(frozen=True, slots=True)
class InputAudioChunk:
    vpcm: bytes
    vsample_rate_hz: int


@dataclasses.dataclass(frozen=True, slots=True)
class InputAudioCommit:
    vreason: str = "vad"


class ContextScope(enum.Enum):
    """Where an injected item lands in the model's view of the conversation.

    SPOKEN_HISTORY is a real conversational turn. BACKGROUND is knowledge the model may use but
    must not read out verbatim, which is what background task results use so they never become
    fake assistant speech.
    """

    SPOKEN_HISTORY = "spoken_history"
    BACKGROUND = "background"
    INSTRUCTION = "instruction"


@dataclasses.dataclass(frozen=True, slots=True)
class SessionContextUpdate:
    vscope: ContextScope
    vtext: str
    vcorrelation: Correlation
    vrole: RealtimeRole = RealtimeRole.ASSISTANT
    vmetadata: dict[str, object] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True, slots=True)
class ToolResultPayload:
    vtool_call_id: str
    vresult: dict[str, object]
    vcorrelation: Correlation


@dataclasses.dataclass(frozen=True, slots=True)
class ResponseRequest:
    vcorrelation: Correlation
    vinstructions: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class InterruptRequest:
    vcorrelation: Correlation
    vreason: str = "handoff"


RealtimeCommand = InputAudioChunk | InputAudioCommit | SessionContextUpdate | ToolResultPayload | ResponseRequest | InterruptRequest
