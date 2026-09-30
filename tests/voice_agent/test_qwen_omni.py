from __future__ import annotations

import asyncio
import base64
import json

import numpy
import pytest
import pytest_asyncio
from aiohttp import WSMsgType, web

from voice_agent.pipeline import voice_contracts
from voice_agent.realtime import events
from voice_agent.realtime.qwen import protocol
from voice_agent.realtime.qwen.session import QWEN_OMNI_DUPLEX, QwenOmniSession

EVENT_TIMEOUT_S = 5.0

WEATHER_TOOL: dict[str, object] = {
    "type": "function",
    "name": "get_current_weather",
    "description": "Current weather for a city.",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
}


class RecordingAudioSink:
    def __init__(self) -> None:
        self.vwrites: list[tuple[bytes, int]] = []
        self.vclears = 0

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        self.vwrites.append((vpcm, vsample_rate_hz))

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        self.vclears += 1


class FakeDashScopeServer:
    """The server half of the Qwen realtime protocol, enough to exercise the adapter offline."""

    def __init__(self) -> None:
        self.vreceived: list[dict[str, object]] = []
        self.vauthorization = ""
        self.vquery = ""
        self.vscript: list[dict[str, object]] = []
        self.vafter_cancel: list[dict[str, object]] = []
        self._vrunner: web.AppRunner | None = None
        self.vurl = ""

    async def start(self) -> None:
        vapp = web.Application()
        vapp.router.add_get("/api-ws/v1/realtime", self._handle)
        self._vrunner = web.AppRunner(vapp)
        await self._vrunner.setup()
        await web.TCPSite(self._vrunner, "127.0.0.1", 0).start()
        self.vurl = f"http://127.0.0.1:{self._vrunner.addresses[0][1]}/api-ws/v1/realtime"

    async def stop(self) -> None:
        if self._vrunner is not None:
            await self._vrunner.cleanup()

    async def _handle(self, vrequest: web.Request) -> web.WebSocketResponse:
        self.vauthorization = vrequest.headers.get("Authorization", "")
        self.vquery = vrequest.query_string
        vws = web.WebSocketResponse()
        await vws.prepare(vrequest)

        async for vmessage in vws:
            if vmessage.type is not WSMsgType.TEXT:
                break
            self.vreceived.append(json.loads(vmessage.data))
            if self.vreceived[-1].get("type") == protocol.SESSION_UPDATE:
                for vframe in self.vscript:
                    await vws.send_str(json.dumps(vframe))
            if self.vreceived[-1].get("type") == protocol.RESPONSE_CANCEL:
                for vframe in self.vafter_cancel:
                    await vws.send_str(json.dumps(vframe))
        return vws


@pytest_asyncio.fixture
async def server():
    vserver = FakeDashScopeServer()
    await vserver.start()
    yield vserver
    await vserver.stop()


def build_session(vserver: FakeDashScopeServer, vsink: RecordingAudioSink) -> QwenOmniSession:
    return QwenOmniSession(
        vapi_key="sk-test",
        vconversation_id="conv_test",
        vsession_id="sess_test",
        vepoch_provider=lambda: 0,
        vaudio_sink=vsink,
        vbase_url=vserver.vurl,
        vtools=[WEATHER_TOOL],
    )


async def collect(vsession: QwenOmniSession, vcount: int) -> list[events.RealtimeEvent]:
    vstream = vsession.events()
    return [await asyncio.wait_for(anext(vstream), EVENT_TIMEOUT_S) for _ in range(vcount)]


def sent_of_type(vserver: FakeDashScopeServer, vtype: str) -> list[dict[str, object]]:
    return [vframe for vframe in vserver.vreceived if vframe.get("type") == vtype]


def test_native_tool_calling_removes_the_transcript_router() -> None:
    # This is the difference that matters against PersonaPlex: delegation stops being inferred.
    assert QWEN_OMNI_DUPLEX.function_calling is True
    assert QWEN_OMNI_DUPLEX.requires_transcript_router() is False
    assert QWEN_OMNI_DUPLEX.can_deliver_out_of_band() is True
    assert QWEN_OMNI_DUPLEX.supports_tool_results is True


@pytest.mark.asyncio
async def test_start_authenticates_and_declares_tools(server: FakeDashScopeServer) -> None:
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()
    for _ in range(50):
        if sent_of_type(server, protocol.SESSION_UPDATE):
            break
        await asyncio.sleep(0.02)
    await vsession.close()

    assert server.vauthorization == "Bearer sk-test"
    assert f"model={protocol.DEFAULT_MODEL}" in server.vquery
    vupdate = sent_of_type(server, protocol.SESSION_UPDATE)[0]["session"]
    assert vupdate["voice"] == protocol.DEFAULT_VOICE
    assert vupdate["tools"] == [WEATHER_TOOL]
    assert vupdate["modalities"] == ["text", "audio"]


@pytest.mark.asyncio
async def test_room_audio_is_downsampled_to_the_model_rate(server: FakeDashScopeServer) -> None:
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()

    vsamples = numpy.zeros(voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ, dtype="<i2")
    await vsession.send_audio(events.InputAudioChunk(vpcm=vsamples.tobytes(), vsample_rate_hz=voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ))
    for _ in range(50):
        if sent_of_type(server, protocol.INPUT_AUDIO_APPEND):
            break
        await asyncio.sleep(0.02)
    await vsession.close()

    vappended = sent_of_type(server, protocol.INPUT_AUDIO_APPEND)
    vdecoded = base64.b64decode(str(vappended[0]["audio"]))
    # One second in at 24 kHz must leave as roughly one second at 16 kHz.
    vout_samples = len(vdecoded) // 2
    assert abs(vout_samples - protocol.INPUT_SAMPLE_RATE_HZ) < protocol.INPUT_SAMPLE_RATE_HZ * 0.02


@pytest.mark.asyncio
async def test_a_foreign_sample_rate_is_refused(server: FakeDashScopeServer) -> None:
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()
    with pytest.raises(ValueError, match="16000"):
        await vsession.send_audio(events.InputAudioChunk(vpcm=b"\x00\x00", vsample_rate_hz=44100))
    await vsession.close()


@pytest.mark.asyncio
async def test_tool_call_surfaces_with_parsed_arguments(server: FakeDashScopeServer) -> None:
    server.vscript = [
        {
            "type": protocol.FUNCTION_CALL_ARGUMENTS_DONE,
            "call_id": "call_abc",
            "name": "get_current_weather",
            "arguments": '{"city": "London"}',
        }
    ]
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()
    vevents = await collect(vsession, 1)
    await vsession.close()

    vcall = vevents[0]
    assert isinstance(vcall, events.RealtimeToolCallRequested)
    assert vcall.vtool_name == "get_current_weather"
    assert vcall.vtool_call_id == "call_abc"
    assert vcall.varguments == {"city": "London"}


@pytest.mark.asyncio
async def test_unparsable_arguments_do_not_kill_the_reader(server: FakeDashScopeServer) -> None:
    server.vscript = [
        {"type": protocol.FUNCTION_CALL_ARGUMENTS_DONE, "call_id": "call_bad", "name": "get_current_weather", "arguments": "{not json"},
        {"type": protocol.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "delta": "still here"},
    ]
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()
    vevents = await collect(vsession, 2)
    await vsession.close()

    assert isinstance(vevents[0], events.RealtimeToolCallRequested)
    assert vevents[0].varguments == {}
    assert isinstance(vevents[1], events.AssistantTranscript)


@pytest.mark.asyncio
async def test_tool_result_is_returned_and_the_response_resumed(server: FakeDashScopeServer) -> None:
    vsession = build_session(server, RecordingAudioSink())
    await vsession.start()

    await vsession.send_tool_result(
        events.ToolResultPayload(vtool_call_id="call_abc", vresult={"summary": "12 degrees"}, vcorrelation=vsession.correlation())
    )
    for _ in range(50):
        if sent_of_type(server, protocol.RESPONSE_CREATE):
            break
        await asyncio.sleep(0.02)
    await vsession.close()

    vitem = sent_of_type(server, protocol.CONVERSATION_ITEM_CREATE)[0]["item"]
    assert vitem["type"] == "function_call_output"
    assert vitem["call_id"] == "call_abc"
    assert json.loads(str(vitem["output"])) == {"summary": "12 degrees"}
    assert sent_of_type(server, protocol.RESPONSE_CREATE)


@pytest.mark.asyncio
async def test_both_sides_of_the_conversation_are_reported(server: FakeDashScopeServer) -> None:
    server.vscript = [
        {"type": protocol.SPEECH_STARTED},
        {"type": protocol.INPUT_TRANSCRIPTION_COMPLETED, "transcript": "what's the weather in London"},
        {"type": protocol.RESPONSE_AUDIO_TRANSCRIPT_DELTA, "delta": "It is twelve degrees.", "response_id": "resp_1"},
        {"type": protocol.RESPONSE_AUDIO_DELTA, "delta": base64.b64encode(b"\x00\x01" * 480).decode()},
        {"type": protocol.RESPONSE_DONE},
    ]
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()
    vevents = await collect(vsession, 4)
    await vsession.close()

    assert isinstance(vevents[0], events.UserSpeechStarted)
    assert isinstance(vevents[1], events.UserTranscriptFinal)
    assert vevents[1].vtext == "what's the weather in London"
    assert isinstance(vevents[2], events.AssistantTranscript)
    assert isinstance(vevents[3], events.AssistantSpeechStopped)
    assert vsink.vwrites == [(b"\x00\x01" * 480, protocol.OUTPUT_SAMPLE_RATE_HZ)]


@pytest.mark.asyncio
async def test_interrupt_cancels_the_response_and_drops_playback(server: FakeDashScopeServer) -> None:
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()

    await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="barge_in"))
    vevents = await collect(vsession, 1)
    for _ in range(50):
        if sent_of_type(server, protocol.RESPONSE_CANCEL):
            break
        await asyncio.sleep(0.02)
    await vsession.close()

    assert sent_of_type(server, protocol.RESPONSE_CANCEL)
    assert vsink.vclears == 1
    assert isinstance(vevents[0], events.RealtimeInterrupted)


@pytest.mark.asyncio
async def test_a_cancelled_response_neither_plays_on_nor_reports_completion(server: FakeDashScopeServer) -> None:
    vaudio = base64.b64encode(b"\x00\x01" * 480).decode()
    server.vscript = [{"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_1"}}]
    server.vafter_cancel = [
        {"type": protocol.RESPONSE_AUDIO_DELTA, "response_id": "resp_1", "delta": vaudio},
        {"type": protocol.RESPONSE_DONE, "response": {"id": "resp_1", "status": "cancelled"}},
    ]
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()
    vstarted = await collect(vsession, 1)
    await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="barge_in"))
    vevents = await collect(vsession, 2)
    await vsession.close()

    assert isinstance(vstarted[0], events.AssistantSpeechStarted) and vstarted[0].vresponse_id == "resp_1"
    assert vsink.vwrites == [], "DashScope sends audio after response.cancel; it must not play"
    vstopped = vevents[1]
    assert isinstance(vstopped, events.AssistantSpeechStopped) and vstopped.vresponse_id == "resp_1" and not vstopped.vcompleted


@pytest.mark.asyncio
async def test_a_cancel_before_the_response_exists_still_cancels_that_response(server: FakeDashScopeServer) -> None:
    vaudio = base64.b64encode(b"\x00\x01" * 480).decode()
    server.vscript = [
        {"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_old"}},
        {"type": protocol.RESPONSE_DONE, "response": {"id": "resp_old", "status": "completed"}},
    ]
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()
    await collect(vsession, 2)
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation()))
    await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="barge_in"))
    await vsession._on_frame({"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_new"}})
    await vsession._on_frame({"type": protocol.RESPONSE_AUDIO_DELTA, "response_id": "resp_new", "delta": vaudio})
    for _ in range(50):
        if len(sent_of_type(server, protocol.RESPONSE_CANCEL)) == 2:
            break
        await asyncio.sleep(0.02)
    await vsession.close()

    assert vsink.vwrites == [], "the audio belongs to the response the cancel was meant for"
    assert len(sent_of_type(server, protocol.RESPONSE_CANCEL)) == 2, "cancelled again once it exists"


@pytest.mark.asyncio
async def test_a_response_cancelled_before_it_existed_reports_no_start_for_a_newer_request_to_claim(server: FakeDashScopeServer) -> None:
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation(), vtext_only=True))
    await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="route_timeout"))
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation()))
    await vsession._on_frame({"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_route"}})
    await vsession._on_frame({"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_answer"}})
    vevents = await collect(vsession, 2)
    await vsession.close()

    vstarts = [vevent.vresponse_id for vevent in vevents if isinstance(vevent, events.AssistantSpeechStarted)]
    assert vstarts == ["resp_answer"]


@pytest.mark.asyncio
async def test_a_refused_creation_no_longer_counts_as_pending(server: FakeDashScopeServer) -> None:
    vaudio = base64.b64encode(b"\x00\x01" * 480).decode()
    vsink = RecordingAudioSink()
    vsession = build_session(server, vsink)
    await vsession.start()
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation()))
    await vsession._on_frame({"type": protocol.ERROR, "error": {"message": "Conversation already has an active response"}})
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation()))
    await vsession._on_frame({"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_ok"}})
    await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="barge_in"))
    await vsession._on_frame({"type": protocol.RESPONSE_AUDIO_DELTA, "response_id": "resp_ok", "delta": vaudio})
    await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation()))
    await vsession._on_frame({"type": protocol.RESPONSE_CREATED, "response": {"id": "resp_next"}})
    vevents = await collect(vsession, 4)
    await vsession.close()

    assert vsink.vwrites == [], "the interrupted response's late audio is dropped"
    vstarts = [vevent.vresponse_id for vevent in vevents if isinstance(vevent, events.AssistantSpeechStarted)]
    assert vstarts == ["resp_ok", "resp_next"], "and the next answer is not cancelled in its place"


def test_manual_response_mode_keeps_turn_detection_but_never_answers_on_its_own() -> None:
    vframe = protocol.session_update_frame("Tina", "You are Karen.", [], vauto_response=False)
    vturn_detection = vframe["session"]["turn_detection"]  # type: ignore[index]
    assert vturn_detection["create_response"] is False
    assert vturn_detection["interrupt_response"] is True
