import asyncio

import pytest

from examples.meet_agent import MeetCall
from examples.meet_tools import MEET_TOOLS_BY_NAME, ToolWeight
from voice_agent.correlation import Correlation
from voice_agent.realtime import events


def test_the_weight_is_the_tools_not_the_models():
    assert MEET_TOOLS_BY_NAME["get_current_time"].vweight is ToolWeight.LIGHT
    assert MEET_TOOLS_BY_NAME["get_current_weather"].vweight is ToolWeight.LIGHT
    assert MEET_TOOLS_BY_NAME["research"].vweight is ToolWeight.HEAVY
    assert MEET_TOOLS_BY_NAME["science_fact"].vweight is ToolWeight.HEAVY
    assert "Runs in the background" in MEET_TOOLS_BY_NAME["research"].schema()["description"]
    assert "Runs in the background" not in MEET_TOOLS_BY_NAME["get_current_time"].schema()["description"]


def test_a_heavy_tool_names_its_goal_by_its_question():
    assert MEET_TOOLS_BY_NAME["research"].goal({"question": "does the deadline hold?"}) == "does the deadline hold?"
    assert MEET_TOOLS_BY_NAME["science_fact"].goal({}) == "science fact"


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
    vcall.vsession, vcall.vreply_parts, vcall.vneeds_followup, vcall.vturn_tools, vcall.vverdict_leaked = Session(), [], False, 0, False
    vcall.remember = lambda _vturn: None
    vcall.vturn_results, vcall.requester = [], lambda: "Kirill"
    vcall.play_filler = lambda _vkind: asyncio.sleep(0)
    vcall.vstarted = []
    vcall.start_background = lambda vtool, varguments: vcall.vstarted.append(vtool.vname) or "Started in the background; the answer arrives later."
    return vcall


@pytest.mark.asyncio
async def test_a_light_tool_runs_inline_and_is_answered_at_once():
    vcall = bare_call()
    await vcall.on_tool_call(call_with("get_current_time", {"timezone": "Europe/London"}))
    vresult, vrespond = vcall.vsession.vresults[0]
    assert "Europe/London" in vresult and "call the tool again" in vresult and vcall.vstarted == []
    assert not vrespond and vcall.vneeds_followup, "answered by one follow-up when the response ends"


@pytest.mark.asyncio
async def test_a_heavy_tool_always_goes_to_the_background():
    vcall = bare_call()
    await vcall.on_tool_call(call_with("research", {"question": "does the deadline hold?"}))
    assert vcall.vstarted == ["research"]
    assert vcall.vsession.vresults[0][0].startswith("Started in the background")
