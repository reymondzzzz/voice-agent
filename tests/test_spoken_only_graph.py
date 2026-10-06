from __future__ import annotations

import pytest
from langchain_core.messages import AIMessageChunk, ToolMessage

from examples.spoken_only_graph import SpokenOnlyGraph, is_tool_result


class _FakeGraph:
    def __init__(self, vitems: list) -> None:
        self._vitems = vitems
        self.vseen_kwargs: dict = {}

    def astream(self, vstate, vconfig=None, *, stream_mode=None, subgraphs=None, context=None):
        self.vseen_kwargs = {"stream_mode": stream_mode, "subgraphs": subgraphs}
        return self._iter()

    async def _iter(self):
        for vitem in self._vitems:
            yield vitem

    def get_graph(self):
        return "delegated"


def _tool(vtext: str):
    return (ToolMessage(content=vtext, tool_call_id="1"), {"langgraph_node": "tools"})


def _spoken(vtext: str):
    return (AIMessageChunk(content=vtext), {"langgraph_node": "model"})


@pytest.mark.parametrize(
    ("vitem", "vexpected"),
    [
        (_tool("Friday 04 September 2026, 02:16 in Asia/Tokyo"), True),
        (_spoken("It's 2:16 AM in Tokyo."), False),
        (("ns", _tool("x")), True),
        (("ns", _spoken("x")), False),
        (("ns", "messages", _tool("x")), True),
        (AIMessageChunk(content="bare"), False),
    ],
)
def test_tool_results_are_recognized_in_every_stream_shape(vitem, vexpected):
    assert is_tool_result(vitem) is vexpected


@pytest.mark.asyncio
async def test_tool_results_never_reach_the_consumer():
    vgraph = SpokenOnlyGraph(
        _FakeGraph([_spoken("It's "), _tool("Friday 04 September 2026, 02:16 in Asia/Tokyo"), _spoken("2:16 AM.")])
    )
    vtexts = [vtoken.content async for vtoken, _ in vgraph.astream({"messages": []}, stream_mode="messages")]
    assert vtexts == ["It's ", "2:16 AM."]


@pytest.mark.asyncio
async def test_streaming_kwargs_are_passed_through_untouched():
    vinner = _FakeGraph([_spoken("hi")])
    vgraph = SpokenOnlyGraph(vinner)
    async for _ in vgraph.astream({"messages": []}, stream_mode="messages", subgraphs=True):
        pass
    assert vinner.vseen_kwargs == {"stream_mode": "messages", "subgraphs": True}


def test_an_unsupported_kwarg_still_raises_at_call_time_so_the_adapter_can_fall_back():
    vgraph = SpokenOnlyGraph(_FakeGraph([]))
    with pytest.raises(TypeError):
        vgraph.astream({"messages": []}, nonexistent_kwarg=1)
