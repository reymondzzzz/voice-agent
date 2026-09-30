import asyncio

import pytest

from examples.meet_addressing import MeetAddressing
from examples.meet_agent import MeetCall, is_spoken_verdict, parse_yes
from examples.meet_memory import MeetTurn
from voice_agent.correlation import Correlation
from voice_agent.realtime import events

CORRELATION = Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)


class Session:
    def __init__(self) -> None:
        self.vresponses = 0
        self.vresults: list[bool] = []
        self.vinterrupts = 0

    def correlation(self):
        return CORRELATION

    async def request_response(self, _vrequest) -> None:
        self.vresponses += 1

    async def send_tool_result(self, _vresult, *, vrespond: bool = True) -> None:
        self.vresults.append(vrespond)

    async def interrupt(self, _vrequest) -> None:
        self.vinterrupts += 1


class Sink:
    vmuted = False
    vfirst_audio_at = None

    async def clear(self) -> None:
        pass


def bare_call() -> MeetCall:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vsession, vcall.vsink = Session(), Sink()
    vcall.vreply_parts, vcall.vroute_parts, vcall.vrouting = [], [], None
    vcall.vneeds_followup, vcall.vturn_tools, vcall.vturn_spoke, vcall.vverdict_leaked, vcall.vdiscard_next = False, 0, False, False, False
    vcall.vturn_done, vcall.vfloor = asyncio.Event(), asyncio.Lock()
    vcall.vuser_speaking, vcall.vlast_bot_activity, vcall.vlast_response_at = False, 0.0, 0.0
    vcall.vtrace = {}
    vcall.remember = lambda *_vargs, **_vkwargs: None
    vcall.vstarted = []
    vcall.start_background = lambda vtool, _vargs: vcall.vstarted.append(vtool.vname) or "Started in the background; the answer arrives later."
    return vcall


def tool_call(vname: str, varguments: dict, vid: str) -> events.RealtimeToolCallRequested:
    return events.RealtimeToolCallRequested(vcorrelation=CORRELATION, vtool_call_id=vid, vtool_name=vname, varguments=varguments)


def test_parsers():
    assert is_spoken_verdict("RESPOND") and is_spoken_verdict("Respond.") and is_spoken_verdict("IGN")
    assert not is_spoken_verdict("Кирилл, в Лондоне сейчас дождь.") and not is_spoken_verdict("Re")
    assert parse_yes("YES") and parse_yes("yes.") and not parse_yes("NO") and not parse_yes("")


@pytest.mark.asyncio
async def test_two_tools_in_one_response_get_one_follow_up_not_two():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_tool_call(tool_call("get_current_weather", {"city": "London"}, "a"))
    await vcall.on_tool_call(tool_call("science_fact", {}, "b"))
    assert vcall.vsession.vresults == [False, False] and vcall.vsession.vresponses == 0
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vfloor.locked()
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and not vcall.vfloor.locked() and vcall.vturn_done.is_set()


@pytest.mark.asyncio
async def test_a_spoken_verdict_is_muted_and_the_answer_asked_for_again():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="RESPOND"))
    assert vcall.vsink.vmuted and vcall.vsession.vinterrupts == 0
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert not vcall.vsink.vmuted and vcall.vsession.vresponses == 1 and vcall.vfloor.locked()


@pytest.mark.asyncio
async def test_a_spoken_verdict_does_not_cancel_the_tool_call_after_it():
    vcall = bare_call()
    await vcall.vfloor.acquire()
    await vcall.on_event(events.AssistantTranscript(vcorrelation=CORRELATION, vtext="RESPOND"))
    await vcall.on_tool_call(tool_call("science_fact", {}, "a"))
    assert vcall.vsession.vinterrupts == 0 and vcall.vstarted == ["science_fact"]
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vfloor.locked()
    await vcall.on_event(events.AssistantSpeechStopped(vcorrelation=CORRELATION))
    assert vcall.vsession.vresponses == 1 and vcall.vturn_done.is_set()


@pytest.mark.asyncio
async def test_a_request_cut_off_before_any_answer_is_asked_again_once():
    vcall = bare_call()
    vcall.vaddressing = MeetAddressing("Karen", lambda _vprompt: asyncio.sleep(0, "IGNORE"))
    vcall.publish = lambda **_vevent: None
    vspoken: list[str] = []

    async def speak(vline: str) -> None:
        vspoken.append(vline)
        vcall.vturn_done.clear()
        vcall.vturn_spoke = len(vspoken) > 1
        vcall.vturn_tools = 0
        vcall.end_turn()

    async def quiet(_vpause_s: float) -> None:
        pass

    async def no_promise() -> None:
        pass

    vcall.speak, vcall.wait_until_quiet, vcall.check_promise = speak, quiet, no_promise
    await vcall.consider(MeetTurn(0.0, "Kirill", "Карен, какая погода в Лондоне?"), "", {})
    assert len(vspoken) == 2
    assert vspoken[1].startswith("[Kirill asked you this a moment ago and you have not answered yet]")


@pytest.mark.asyncio
async def test_a_reply_that_promised_work_without_a_tool_is_corrected():
    vcall = bare_call()
    vspoken: list[str] = []

    async def route(_vprompt: str) -> str:
        return "YES"

    async def speak(vline: str) -> None:
        vspoken.append(vline)

    vcall.route, vcall.speak = route, speak
    await vcall.check_promise()
    assert vspoken and vspoken[0].startswith("[internal] Your last reply promised")
