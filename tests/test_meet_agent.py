import asyncio
import collections
import time

import aiohttp
import pytest

from examples.meet_addressing import MeetAddressing
from examples.meet_agent import MeetCall, PendingResult, RoomAudioSink
from voice_agent.correlation import Correlation
from voice_agent.realtime import events


class ClosingSession:
    async def send_audio(self, _vchunk) -> None:
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")


class OpenSession:
    def __init__(self) -> None:
        self.vframes = 0

    async def send_audio(self, _vchunk) -> None:
        self.vframes += 1


@pytest.mark.asyncio
async def test_a_closing_session_costs_a_frame_not_the_audio_pump() -> None:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vsession = ClosingSession()
    await vcall.forward_frame(b"\x00\x00" * 480)
    vnext = OpenSession()
    vcall.vsession = vnext
    await vcall.forward_frame(b"\x00\x00" * 480)
    assert vnext.vframes == 1


@pytest.mark.asyncio
async def test_everything_waiting_is_told_in_one_turn_and_a_talked_over_turn_is_finished_whole() -> None:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vaddressing = MeetAddressing("Karen", lambda _vprompt: asyncio.sleep(0, ""))
    vcall.vpending = collections.deque([PendingResult("Carl", "a science fact", "honey keeps"), PendingResult("Anna", "the deadline", "it holds")])
    vcall.vpending_added = asyncio.Event()
    vcall.vplayed = asyncio.Event()
    vcall.vinterrupted = False
    vspoken: list[str] = []

    async def quiet(_vpause_s: float) -> None:
        pass

    async def speak(vline: str) -> None:
        vspoken.append(vline)
        vcall.vinterrupted = len(vspoken) == 1
        vcall.vplayed.set()
        vcall.release_floor()

    vcall.wait_until_quiet = quiet
    vcall.speak = speak
    vcall.vfloor = asyncio.Lock()
    vcall.vpending_added.set()
    vworker = asyncio.create_task(vcall.deliver_pending())
    for _ in range(50):
        if len(vspoken) == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    vworker.cancel()

    assert len(vspoken) == 2
    assert "honey keeps" in vspoken[0] and "it holds" in vspoken[0]
    assert "cut off" in vspoken[1] and "honey keeps" in vspoken[1] and "it holds" in vspoken[1]
    assert not vcall.vpending


@pytest.mark.asyncio
async def test_a_routing_verdict_is_never_spoken_never_logged_as_karen_and_frees_the_floor() -> None:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vfloor = asyncio.Lock()
    vcall.vrouting = None
    vcall.vroute_parts = []
    vcall.vdiscard_next = False
    vcall.vtool_followup = False
    vcall.vreply_parts = []
    vremembered = []
    vcall.remember = vremembered.append
    vcorrelation = Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)
    vrequests = []

    class RoutingSession:
        def correlation(self):
            return vcorrelation

        async def request_response(self, vrequest):
            vrequests.append(vrequest)
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(answer()))

    class Source:
        def __init__(self) -> None:
            self.vframes = 0

        async def capture_frame(self, _vframe) -> None:
            self.vframes += 1

    vsource = Source()
    vcall.vsink = RoomAudioSink(vsource)
    vspoken_verdict = b"\x01\x00" * 480

    async def answer():
        await vcall.vsink.write(vspoken_verdict, 24000)
        await vcall.on_event(events.AssistantTranscript(vcorrelation=vcorrelation, vtext="IGN"))
        await vcall.on_event(events.AssistantTranscript(vcorrelation=vcorrelation, vtext="ORE"))
        await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=vcorrelation))

    vcall.vsession = RoutingSession()
    assert await vcall.route("is it for Karen?") == "IGNORE"
    assert vsource.vframes == 0
    assert not vcall.vsink.vmuted
    assert vrequests[0].vtext_only
    assert vremembered == []
    assert not vcall.vfloor.locked()


def test_karen_goes_straight_on_after_her_own_answer_but_waits_out_the_people_otherwise() -> None:
    class Source:
        queued_duration = 2.0

    vcall = MeetCall.__new__(MeetCall)
    vcall.vsource = Source()
    vcall.vuser_speaking = False
    vnow = time.monotonic()
    vcall.vlast_bot_activity = vnow - 1
    vcall.vlast_human_speech = vnow - 1
    vcall.vlast_bot_reply_done = vnow - 0.9
    assert vcall.is_quiet(3.0), "her answer is still playing, but she holds the floor"
    vcall.vlast_bot_reply_done = vnow - 2
    assert not vcall.is_quiet(3.0), "a person spoke after her answer: wait for them"
    Source.queued_duration = 0
    vcall.vlast_human_speech = vnow - 4
    assert vcall.is_quiet(3.0)
    vcall.vuser_speaking = True
    assert not vcall.is_quiet(3.0)


def test_reply_latency_splits_the_wait_by_stage() -> None:
    from examples.meet_agent import reply_latency

    vstages = reply_latency({"speech_end": 10.0, "heard": 10.3, "decided": 11.2, "requested": 11.25}, 12.1)
    assert {vname: round(vvalue, 2) for vname, vvalue in vstages.items()} == {"heard": 0.3, "decide": 0.9, "voice": 0.85, "total": 2.1}
    assert reply_latency({"requested": 5.0}, 5.8) == {"voice": 0.8000000000000007} or round(reply_latency({"requested": 5.0}, 5.8)["voice"], 2) == 0.8
