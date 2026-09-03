from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from voice_agent.realtime import events
from voice_agent.realtime.capabilities import RealtimeModelCapabilities


class RealtimeSessionState(enum.Enum):
    IDLE = "idle"
    STARTING = "starting"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@runtime_checkable
class RealtimeSpeechSession(Protocol):
    """A persistent duplex connection to a speech model.

    Implementations own spoken output and conversational timing. They must not own business logic:
    a tool call is reported as an event and answered with a result, never executed here.

    Adding a backend means implementing this protocol plus declaring capabilities. Nothing else in
    the system may import a provider SDK.
    """

    @property
    def vcapabilities(self) -> RealtimeModelCapabilities: ...

    @property
    def vstate(self) -> RealtimeSessionState: ...

    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def send_audio(self, vchunk: events.InputAudioChunk) -> None: ...

    async def commit_audio(self, vcommit: events.InputAudioCommit) -> None: ...

    async def add_context(self, vupdate: events.SessionContextUpdate) -> None: ...

    async def send_tool_result(self, vresult: events.ToolResultPayload) -> None: ...

    async def request_response(self, vrequest: events.ResponseRequest) -> None: ...

    async def interrupt(self, vrequest: events.InterruptRequest) -> None: ...

    def events(self) -> AsyncIterator[events.RealtimeEvent]: ...
