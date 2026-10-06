import pytest

from examples import meet_agent, meet_workspace
from examples.meet_agent import MeetCall, Turn
from examples.meet_delivery import PendingResult, ResultQueue
from voice_agent.correlation import Correlation
from voice_agent.realtime import events


class Session:
    def __init__(self) -> None:
        self.vresults: list[tuple[object, bool]] = []

    def correlation(self):
        return Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)

    async def send_tool_result(self, vresult, *, vrespond: bool = True) -> None:
        self.vresults.append((vresult.vresult["result"], vrespond))


def call_with(vname: str, varguments: dict) -> events.RealtimeToolCallRequested:
    vcorrelation = Correlation.create(vconversation_id="c", vsession_id="s", vconversation_epoch=0, vturn_id=None)
    return events.RealtimeToolCallRequested(vcorrelation=vcorrelation, vtool_call_id="call_1", vtool_name=vname, varguments=varguments)


def bare_call() -> MeetCall:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vturn, vcall.vqueue = Turn(), ResultQueue()
    vcall.vsession, vcall.vreply_parts, vcall.vverdict_leaked = Session(), [], False
    vcall.remember = lambda _vturn: None
    vcall.requester = lambda: "Kirill"
    vcall.spawn = lambda vcoro: vcoro.close()
    vcall.vstarted = []
    vcall.start_background = lambda vtool, varguments: vcall.vstarted.append(vtool.vname) or "Started in the background; the answer arrives later."
    return vcall


@pytest.mark.asyncio
async def test_a_light_tool_runs_inline_and_is_answered_at_once():
    vcall = bare_call()
    await vcall.on_tool_call(call_with("get_current_time", {"timezone": "Europe/London"}))
    vresult, vrespond = vcall.vsession.vresults[0]
    assert "Europe/London" in vresult and "call the tool again" in vresult and vcall.vstarted == []
    assert not vrespond and vcall.vturn.vneeds_followup, "answered by one follow-up when the response ends"


@pytest.mark.asyncio
async def test_a_heavy_tool_always_goes_to_the_background():
    vcall = bare_call()
    await vcall.on_tool_call(call_with("research", {"question": "does the deadline hold?"}))
    assert vcall.vstarted == ["research"]
    assert vcall.vsession.vresults[0][0].startswith("Started in the background")


@pytest.mark.asyncio
async def test_a_light_tool_that_raises_answers_with_the_error_and_the_call_goes_on(monkeypatch):
    def broken(_vkey):
        raise KeyError("tracker offline")

    monkeypatch.setattr(meet_workspace, "get_task", broken)
    vcall = bare_call()
    await vcall.on_tool_call(call_with("get_task", {"key": "PAY-101"}))
    vresult, _ = vcall.vsession.vresults[0]
    assert vresult.startswith("Error: KeyError") and vcall.vturn.vneeds_followup


@pytest.mark.asyncio
@pytest.mark.parametrize(("vminutes", "vexpected"), [(None, 30), ("45 минут", 30), ("1.5", 1), (-15, 1), (90, 90)])
async def test_a_meeting_length_the_model_mangles_still_finds_a_slot(monkeypatch, vminutes, vexpected):
    vseen = []
    monkeypatch.setattr(meet_workspace, "find_free_slot", lambda _vpeople, vlength, *_: vseen.append(vlength) or "slot")
    vcall = bare_call()
    await vcall.on_tool_call(call_with("find_free_slot", {"people": ["Anna"], "minutes": vminutes}))
    assert vseen == [vexpected]


@pytest.mark.asyncio
async def test_results_looked_up_before_a_reconnect_are_told_on_the_new_session(monkeypatch):
    class NewSession:
        def __init__(self, **_vkwargs) -> None:
            pass

        async def start(self) -> None:
            pass

    monkeypatch.setenv("DASHSCOPE_API_KEY", "test")
    monkeypatch.setattr(meet_agent, "QwenOmniSession", NewSession)
    vcall = bare_call()
    vcall.vsession, vcall.vroom, vcall.vsink, vcall.vrouting = None, type("Room", (), {"name": "r"})(), None, None
    vcall.vheard_speakers, vcall.instructions, vcall.end_turn = set(), lambda: "", lambda: None
    vcall.vturn.vresults = [PendingResult("Kirill", "get_task(PAY-101)", "PAY-101: в работе")]
    await vcall.open_session()
    assert vcall.vturn.vresults == [] and [vresult.vanswer for vresult in vcall.vqueue.take_all()] == ["PAY-101: в работе"]
