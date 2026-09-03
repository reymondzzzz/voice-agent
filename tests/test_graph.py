from __future__ import annotations

import json

from langchain_core.language_models.fake_chat_models import FakeListChatModel

import handoff
from graph import build_call_agent_tool, build_persona_graph
from personas import resolve_persona


class _ToolBindingFakeChatModel(FakeListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def _call_agent_tool(vpending: handoff.PendingHandoff):
    return build_call_agent_tool(resolve_persona("boss"), vpending)


def test_tool_arms_the_pending_handoff_and_reports_the_target_voice():
    vpending = handoff.PendingHandoff()
    vraw = _call_agent_tool(vpending).invoke({"target_agent_id": "alice", "handoff_summary": "the q3 sales report"})
    assert json.loads(vraw) == {
        "authorized": True,
        "target_agent_id": "alice",
        "target_name": "Alice",
        "handoff_summary": "the q3 sales report",
        "voice_profile_id": "voice_alice",
    }
    assert vpending.armed()


def test_a_refused_handoff_leaves_nothing_pending():
    vpending = handoff.PendingHandoff()
    vraw = _call_agent_tool(vpending).invoke({"target_agent_id": "nobody", "handoff_summary": "the report"})
    assert vraw == "Error: target agent 'nobody' not found"
    assert not vpending.armed()


def test_the_tool_refuses_while_a_handoff_is_already_pending():
    vpending = handoff.PendingHandoff()
    vtool = _call_agent_tool(vpending)
    vtool.invoke({"target_agent_id": "alice", "handoff_summary": "the report"})
    vraw = vtool.invoke({"target_agent_id": "alice", "handoff_summary": "the report"})
    assert vraw == "Error: a handoff is already pending on this call"


def test_persona_graph_compiles_with_the_handoff_tool():
    vpending = handoff.PendingHandoff()
    vgraph = build_persona_graph(
        resolve_persona("boss"),
        _ToolBindingFakeChatModel(responses=["ok"]),
        [_call_agent_tool(vpending)],
    )
    assert "tools" in vgraph.get_graph().nodes
