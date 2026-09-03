from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

from voice_agent.correlation import Correlation, TURN_ID_PREFIX, new_id
from voice_agent.realtime import events
from voice_agent.realtime.capabilities import NATIVE_DUPLEX, RealtimeModelCapabilities
from voice_agent.realtime.session import RealtimeSessionState


class FakeRealtimeSpeechSession:
    """A scriptable stand-in for a real duplex speech model.

    It exists so the entire agent plane can be tested without audio: tests push events in and
    assert on what the bridge sent back. It is a test double, not a placeholder for the real
    adapter, and it implements exactly the protocol a real backend must implement.
    """

    def __init__(
        self,
        *,
        vconversation_id: str,
        vsession_id: str,
        vepoch_provider: Callable[[], int],
        vcapabilities: RealtimeModelCapabilities = NATIVE_DUPLEX,
    ) -> None:
        self.vconversation_id = vconversation_id
        self.vsession_id = vsession_id
        self._vepoch_provider = vepoch_provider
        self._vcapabilities = vcapabilities
        self._vstate = RealtimeSessionState.IDLE
        self._vevents: asyncio.Queue[events.RealtimeEvent | None] = asyncio.Queue()
        self.vcurrent_turn_id: str | None = None

        self.vsent_context: list[events.SessionContextUpdate] = []
        self.vsent_tool_results: list[events.ToolResultPayload] = []
        self.vresponse_requests: list[events.ResponseRequest] = []
        self.vinterrupts: list[events.InterruptRequest] = []
        self.vaudio_chunks: list[events.InputAudioChunk] = []
        self.vcommits: list[events.InputAudioCommit] = []

    @property
    def vcapabilities(self) -> RealtimeModelCapabilities:
        return self._vcapabilities

    @property
    def vstate(self) -> RealtimeSessionState:
        return self._vstate

    async def start(self) -> None:
        self._vstate = RealtimeSessionState.READY

    async def close(self) -> None:
        self._vstate = RealtimeSessionState.CLOSED
        await self._vevents.put(None)

    async def send_audio(self, vchunk: events.InputAudioChunk) -> None:
        self.vaudio_chunks.append(vchunk)

    async def commit_audio(self, vcommit: events.InputAudioCommit) -> None:
        self.vcommits.append(vcommit)

    async def add_context(self, vupdate: events.SessionContextUpdate) -> None:
        self.vsent_context.append(vupdate)

    async def send_tool_result(self, vresult: events.ToolResultPayload) -> None:
        self.vsent_tool_results.append(vresult)

    async def request_response(self, vrequest: events.ResponseRequest) -> None:
        self.vresponse_requests.append(vrequest)

    async def interrupt(self, vrequest: events.InterruptRequest) -> None:
        self.vinterrupts.append(vrequest)

    async def events(self) -> AsyncIterator[events.RealtimeEvent]:
        while True:
            vevent = await self._vevents.get()
            if vevent is None:
                return
            yield vevent

    def correlation(self, *, vturn_id: str | None = None) -> Correlation:
        return Correlation.create(
            vconversation_id=self.vconversation_id,
            vsession_id=self.vsession_id,
            vconversation_epoch=self._vepoch_provider(),
            vturn_id=vturn_id if vturn_id is not None else self.vcurrent_turn_id,
        )

    async def _emit(self, vevent: events.RealtimeEvent) -> None:
        await self._vevents.put(vevent)

    async def emit_user_speech_started(self) -> str:
        self.vcurrent_turn_id = new_id(TURN_ID_PREFIX)
        await self._emit(events.UserSpeechStarted(vcorrelation=self.correlation()))
        return self.vcurrent_turn_id

    async def emit_user_speech_stopped(self, *, vduration_s: float = 1.0) -> None:
        await self._emit(events.UserSpeechStopped(vcorrelation=self.correlation(), vspeech_duration_s=vduration_s))

    async def emit_partial_transcript(self, vtext: str, *, vrevision: int = 0) -> None:
        await self._emit(events.UserTranscriptPartial(vcorrelation=self.correlation(), vtext=vtext, vrevision=vrevision))

    async def emit_final_transcript(self, vtext: str, *, vrevision: int = 0) -> None:
        await self._emit(events.UserTranscriptFinal(vcorrelation=self.correlation(), vtext=vtext, vrevision=vrevision))

    async def emit_tool_call(self, vtool_name: str, varguments: dict[str, object], *, vtool_call_id: str | None = None) -> str:
        vcall_id = vtool_call_id or new_id("call_")
        await self._emit(
            events.RealtimeToolCallRequested(
                vcorrelation=self.correlation(),
                vtool_call_id=vcall_id,
                vtool_name=vtool_name,
                varguments=varguments,
            )
        )
        return vcall_id

    async def emit_assistant_speech_started(self, *, vresponse_id: str = "resp_1") -> None:
        await self._emit(events.AssistantSpeechStarted(vcorrelation=self.correlation(), vresponse_id=vresponse_id))

    async def emit_assistant_speech_stopped(self, *, vresponse_id: str = "resp_1", vcompleted: bool = True) -> None:
        await self._emit(events.AssistantSpeechStopped(vcorrelation=self.correlation(), vresponse_id=vresponse_id, vcompleted=vcompleted))

    async def emit_interrupted(self, *, vreason: str = "barge_in", vresponse_id: str = "resp_1") -> None:
        await self._emit(events.RealtimeInterrupted(vcorrelation=self.correlation(), vreason=vreason, vresponse_id=vresponse_id))

    async def emit_error(self, vmessage: str, *, vrecoverable: bool = True) -> None:
        await self._emit(events.RealtimeSessionError(vcorrelation=self.correlation(), vmessage=vmessage, vrecoverable=vrecoverable))
