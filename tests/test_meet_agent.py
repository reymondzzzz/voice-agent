import asyncio
import collections
import time

import aiohttp
import numpy
import pytest
from livekit import rtc

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

    async def no_barge_in(_vpcm: bytes) -> None:
        pass

    vcall.watch_for_barge_in = no_barge_in
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
    vcall.vreply_seq, vcall.vopen_replies, vcall.vplayed_replies, vcall.vcut_replies, vcall.vplayout = 0, set(), set(), set(), asyncio.Condition()
    vcall.vawaiting = False
    vcall.spawn = asyncio.ensure_future
    vcall.vsource = type("Source", (), {"queued_duration": 0})()
    vcall.vreply_audio, vcall.vheard_by_reply = {}, {}
    vcall.vlast_bot_played = 0.0
    vspoken: list[str] = []

    async def quiet(_vpause_s: float) -> None:
        pass

    async def speak(vline: str) -> None:
        vspoken.append(vline)
        vcall.vreply_seq += 1
        (vcall.vcut_replies if len(vspoken) == 1 else vcall.vplayed_replies).add(vcall.vreply_seq)
        await vcall.note_playout()
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


def test_loudness_tells_speech_from_silence() -> None:
    from examples.meet_agent import loudness_dbfs

    vtone = (numpy.sin(numpy.arange(480) / 3) * 8000).astype(numpy.int16).tobytes()
    assert loudness_dbfs(vtone) > -20
    assert loudness_dbfs(bytes(960)) < -100


@pytest.mark.asyncio
async def test_speech_over_karen_cuts_her_off_locally_after_150ms_and_only_then() -> None:
    class Source:
        queued_duration = 2.0

    class Sink:
        vmuted = False
        vcleared = 0
        vtimeline_s = 0.0

        async def clear(self) -> None:
            self.vcleared += 1

        async def cut(self) -> float:
            await self.clear()
            return 0.0

    class Session:
        vinterrupts = 0

        def correlation(self):
            return Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)

        async def interrupt(self, _vrequest) -> None:
            self.vinterrupts += 1

    vcall = MeetCall.__new__(MeetCall)
    vcall.vsource, vcall.vsink, vcall.vsession = Source(), Sink(), Session()
    vcall.vloud_s, vcall.vfloor = 0.0, asyncio.Lock()
    vcall.vreply_seq, vcall.vopen_replies, vcall.vplayed_replies, vcall.vcut_replies, vcall.vplayout = 0, set(), set(), set(), asyncio.Condition()
    vcall.vopen_replies.add(1)
    vcall.vuser_speaking, vcall.vlast_human_speech = False, 0.0
    vcall.vreply_parts, vcall.vturn_reply, vcall.vreply_audio, vcall.vheard_by_reply = [], "", {}, {}
    vcall.publish = lambda **_vevent: None
    await vcall.vfloor.acquire()
    vspeech = (numpy.sin(numpy.arange(160) / 3) * 8000).astype(numpy.int16).tobytes()  # 10ms at 16kHz

    for _ in range(14):
        await vcall.watch_for_barge_in(vspeech)
    assert vcall.vsink.vcleared == 0, "140ms is not yet a barge-in"
    await vcall.watch_for_barge_in(vspeech)
    assert vcall.vsink.vcleared == 1 and vcall.vsession.vinterrupts == 1 and vcall.vcut_replies == {1}
    assert vcall.vuser_speaking, "until Qwen reports the end of this speech, the room is not quiet"

    Source.queued_duration = 0
    for _ in range(30):
        await vcall.watch_for_barge_in(vspeech)
    assert vcall.vsink.vcleared == 1, "nothing of Karen's is playing"


@pytest.mark.asyncio
async def test_a_second_bridge_track_replaces_the_first_pump():
    vcall = MeetCall.__new__(MeetCall)
    vcall._vtasks, vcall.vpump, vcall.vcaller_name = set(), None, ""
    vpumped: list[str] = []

    async def pump_audio(vtrack) -> None:
        vpumped.append(vtrack)
        await asyncio.sleep(3600)

    vcall.pump_audio = pump_audio
    vtrack = type("Track", (), {"kind": rtc.TrackKind.KIND_AUDIO})
    vbridge = type("Participant", (), {"name": "", "identity": "meet-bridge"})
    vcall.on_track_subscribed(vtrack(), None, vbridge())
    vfirst = vcall.vpump
    vcall.on_track_subscribed(vtrack(), None, vbridge())
    await asyncio.sleep(0)
    assert vfirst.cancelled() and not vcall.vpump.done() and len(vpumped) == 1
    vcall.vpump.cancel()
