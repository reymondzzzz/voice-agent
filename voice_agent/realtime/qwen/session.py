"""Qwen Omni Realtime as a `RealtimeSpeechSession`.

The second backend behind the same protocol, and the one that shows what the boundary is for: it has
native function calling, so `requires_transcript_router()` is false and delegation stops being
something the agent plane has to infer from words.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

from voice_agent.pipeline import voice_contracts
from voice_agent.pipeline.voice_pcm_resample import PcmResampler
from voice_agent.correlation import Correlation, TURN_ID_PREFIX, new_id
from voice_agent.realtime import events
from voice_agent.realtime.audio import AudioSink
from voice_agent.realtime.capabilities import RealtimeModelCapabilities
from voice_agent.realtime.qwen import protocol
from voice_agent.realtime.session import RealtimeSessionState

QWEN_OMNI_DUPLEX = RealtimeModelCapabilities(
    native_audio_input=True,
    native_audio_output=True,
    full_duplex=True,
    barge_in=True,
    function_calling=True,
    transcript_events=True,
    context_injection=True,
    async_context_updates=True,
    supports_response_cancel=True,
    supports_tool_results=True,
    supports_manual_response_trigger=True,
    supports_audio_playback_ack=False,
)


class QwenOmniSession:
    """A duplex speech session against DashScope's Qwen Omni Realtime endpoint.

    Unlike the local PersonaPlex adapter this one reports both sides of the conversation: the caller's
    words arrive as `UserTranscriptFinal`, the model's as `AssistantTranscript`. Tool calls arrive as
    `RealtimeToolCallRequested` and are answered with `send_tool_result`, so nothing has to be parsed
    out of prose.
    """

    def __init__(
        self,
        *,
        vapi_key: str,
        vconversation_id: str,
        vsession_id: str,
        vepoch_provider: Callable[[], int],
        vaudio_sink: AudioSink,
        vbase_url: str = protocol.SINGAPORE_WS_URL,
        vmodel: str = protocol.DEFAULT_MODEL,
        vvoice: str = protocol.DEFAULT_VOICE,
        vinstructions: str = protocol.DEFAULT_INSTRUCTIONS,
        vtools: list[dict[str, object]] | None = None,
        vauto_response: bool = True,
        vsilence_ms: int = 800,
        vtranscription_language: str = "",
    ) -> None:
        self.vconversation_id = vconversation_id
        self.vsession_id = vsession_id
        self.vmodel = vmodel
        self.vvoice = vvoice
        self.vinstructions = vinstructions
        self.vtools = vtools or []
        self.vauto_response = vauto_response
        self.vsilence_ms = vsilence_ms
        self.vtranscription_language = vtranscription_language
        self._vapi_key = vapi_key
        self._vbase_url = vbase_url
        self._vepoch_provider = vepoch_provider
        self._vaudio_sink = vaudio_sink
        self._vstate = RealtimeSessionState.IDLE
        self._vevents: asyncio.Queue[events.RealtimeEvent | None] = asyncio.Queue()
        self._vsession: aiohttp.ClientSession | None = None
        self._vws: aiohttp.ClientWebSocketResponse | None = None
        self._vreader: asyncio.Task[None] | None = None
        # The room runs at 24 kHz and Qwen listens at 16 kHz, so microphone audio is downsampled on
        # the way out. Output already arrives at the room rate and is passed through untouched.
        self._vresampler = PcmResampler(voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ, protocol.INPUT_SAMPLE_RATE_HZ)
        self.vcurrent_turn_id: str | None = None
        self._vresponse_id = ""

    @property
    def vcapabilities(self) -> RealtimeModelCapabilities:
        return QWEN_OMNI_DUPLEX

    @property
    def vstate(self) -> RealtimeSessionState:
        return self._vstate

    def correlation(self) -> Correlation:
        return Correlation.create(
            vconversation_id=self.vconversation_id,
            vsession_id=self.vsession_id,
            vconversation_epoch=self._vepoch_provider(),
            vturn_id=self.vcurrent_turn_id,
        )

    async def start(self) -> None:
        self._vstate = RealtimeSessionState.STARTING
        self._vsession = aiohttp.ClientSession()
        self._vws = await self._vsession.ws_connect(
            protocol.connection_url(self._vbase_url, self.vmodel),
            headers={"Authorization": f"Bearer {self._vapi_key}"},
            max_msg_size=0,
        )
        await self._send(self._session_frame())
        self._vstate = RealtimeSessionState.READY
        self._vreader = asyncio.create_task(self._read_loop(), name="qwen-omni-reader")

    async def close(self) -> None:
        self._vstate = RealtimeSessionState.CLOSING
        if self._vreader is not None:
            self._vreader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._vreader
        if self._vws is not None:
            await self._vws.close()
        if self._vsession is not None:
            await self._vsession.close()
        self._vstate = RealtimeSessionState.CLOSED
        await self._vevents.put(None)

    def _session_frame(self) -> dict[str, object]:
        vframe = protocol.session_update_frame(self.vvoice, self.vinstructions, self.vtools, vsilence_ms=self.vsilence_ms, vauto_response=self.vauto_response)
        if self.vtranscription_language:
            # Unpinned, the recogniser guesses per utterance and wrote Russian speech as Polish or Chinese.
            vframe["session"]["input_audio_transcription"]["language"] = self.vtranscription_language  # type: ignore[index]
        return vframe

    async def _send(self, vframe: dict[str, object]) -> None:
        if self._vws is None:
            raise RuntimeError("session is not started")
        await self._vws.send_str(json.dumps(vframe))

    async def send_audio(self, vchunk: events.InputAudioChunk) -> None:
        if vchunk.vsample_rate_hz == protocol.INPUT_SAMPLE_RATE_HZ:
            vpcm = vchunk.vpcm
        elif vchunk.vsample_rate_hz == voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ:
            vpcm = self._vresampler.process(vchunk.vpcm)
        else:
            raise ValueError(f"qwen omni accepts {protocol.INPUT_SAMPLE_RATE_HZ} or {voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ} Hz, got {vchunk.vsample_rate_hz}")
        if not vpcm:
            return
        await self._send({"type": protocol.INPUT_AUDIO_APPEND, "audio": base64.b64encode(vpcm).decode()})

    async def commit_audio(self, vcommit: events.InputAudioCommit) -> None:
        await self._send({"type": protocol.INPUT_AUDIO_COMMIT})

    async def add_context(self, vupdate: events.SessionContextUpdate) -> None:
        vrole = "user" if vupdate.vrole is events.RealtimeRole.USER else "assistant"
        await self._send(protocol.message_frame(vrole, vupdate.vtext))

    async def update_instructions(self, vinstructions: str) -> None:
        """Replace the session prompt. Unlike `response.instructions`, which makes the endpoint stop
        calling tools, this keeps function calling, and unlike a message it does not accumulate."""
        self.vinstructions = vinstructions
        await self._send(self._session_frame())

    async def send_tool_result(self, vresult: events.ToolResultPayload, *, vrespond: bool = True) -> None:
        await self._send(protocol.function_output_frame(vresult.vtool_call_id, json.dumps(vresult.vresult)))
        if vrespond:
            await self._send({"type": protocol.RESPONSE_CREATE})

    async def request_response(self, vrequest: events.ResponseRequest) -> None:
        vresponse: dict[str, object] = {}
        if vrequest.vinstructions:
            vresponse["instructions"] = vrequest.vinstructions
        if vrequest.vtext_only:
            vresponse["modalities"] = ["text"]
        vframe: dict[str, object] = {"type": protocol.RESPONSE_CREATE}
        if vresponse:
            vframe["response"] = vresponse
        await self._send(vframe)

    async def interrupt(self, vrequest: events.InterruptRequest) -> None:
        await self._send({"type": protocol.RESPONSE_CANCEL})
        await self._vaudio_sink.clear()
        await self._vevents.put(
            events.RealtimeInterrupted(vcorrelation=self.correlation(), vreason=vrequest.vreason, vresponse_id=self._vresponse_id)
        )

    async def events(self) -> AsyncIterator[events.RealtimeEvent]:
        while True:
            vevent = await self._vevents.get()
            if vevent is None:
                return
            yield vevent

    async def _read_loop(self) -> None:
        assert self._vws is not None
        try:
            async for vmessage in self._vws:
                if vmessage.type is aiohttp.WSMsgType.TEXT:
                    await self._on_frame(json.loads(vmessage.data))
                elif vmessage.type is aiohttp.WSMsgType.ERROR:
                    await self._vevents.put(
                        events.RealtimeSessionError(vcorrelation=self.correlation(), vmessage=str(self._vws.exception()), vrecoverable=False)
                    )
                    break
        finally:
            await self._vevents.put(None)

    async def _on_frame(self, vframe: dict[str, Any]) -> None:
        vtype = vframe.get("type")

        if vtype == protocol.RESPONSE_AUDIO_DELTA:
            await self._vaudio_sink.write(base64.b64decode(vframe["delta"]), protocol.OUTPUT_SAMPLE_RATE_HZ)

        elif vtype == protocol.SPEECH_STARTED:
            self.vcurrent_turn_id = new_id(TURN_ID_PREFIX)
            await self._vevents.put(events.UserSpeechStarted(vcorrelation=self.correlation()))

        elif vtype == protocol.SPEECH_STOPPED:
            await self._vevents.put(events.UserSpeechStopped(vcorrelation=self.correlation()))

        elif vtype == protocol.INPUT_TRANSCRIPTION_COMPLETED:
            await self._vevents.put(
                events.UserTranscriptFinal(vcorrelation=self.correlation(), vtext=str(vframe.get("transcript", "")))
            )

        elif vtype in (protocol.RESPONSE_AUDIO_TRANSCRIPT_DELTA, protocol.RESPONSE_TEXT_DELTA):
            self._vresponse_id = str(vframe.get("response_id", self._vresponse_id))
            await self._vevents.put(
                events.AssistantTranscript(
                    vcorrelation=self.correlation(),
                    vtext=str(vframe.get("delta") or vframe.get("text") or ""),
                    vresponse_id=self._vresponse_id,
                    vfinal=False,
                )
            )

        elif vtype == protocol.FUNCTION_CALL_ARGUMENTS_DONE:
            await self._vevents.put(
                events.RealtimeToolCallRequested(
                    vcorrelation=self.correlation(),
                    vtool_call_id=str(vframe.get("call_id", "")),
                    vtool_name=str(vframe.get("name", "")),
                    varguments=_decode_arguments(vframe.get("arguments")),
                )
            )

        elif vtype == protocol.RESPONSE_DONE:
            await self._vevents.put(
                events.AssistantSpeechStopped(vcorrelation=self.correlation(), vresponse_id=self._vresponse_id, vcompleted=True)
            )

        elif vtype == protocol.ERROR:
            verror = vframe.get("error") or {}
            await self._vevents.put(
                events.RealtimeSessionError(
                    vcorrelation=self.correlation(),
                    vmessage=str(verror.get("message", vframe)),
                    vrecoverable=False,
                )
            )


def _decode_arguments(varguments: object) -> dict[str, object]:
    """Arguments arrive as a JSON string. A model can emit one that does not parse, and that must
    surface as an empty call the tool layer rejects rather than as a crash in the read loop."""
    if isinstance(varguments, dict):
        return varguments
    if not isinstance(varguments, str) or not varguments.strip():
        return {}
    try:
        vparsed = json.loads(varguments)
    except json.JSONDecodeError:
        return {}
    return vparsed if isinstance(vparsed, dict) else {}
