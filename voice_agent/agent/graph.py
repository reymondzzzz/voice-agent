from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.messages import HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from voice_agent.agent.reasoning import ReasoningModel, ReasoningRequest
from voice_agent.agent.state import BackgroundResult, ChatState, HiddenContextItem
from voice_agent.agent.tasks.models import TaskMode

DelegateFn = Callable[[str, TaskMode, str], Awaitable[dict[str, Any]]]


def task_thread_config(vtask_id: str, vconversation_id: str) -> dict[str, Any]:
    """One LangGraph thread per task, one Store namespace per conversation.

    Concurrent background tasks sharing a thread_id would fight over the same checkpoint, so the
    task owns the thread and only long-lived memory is shared across them.
    """

    return {
        "configurable": {
            "thread_id": vtask_id,
            "checkpoint_ns": vconversation_id,
        },
        "metadata": {"conversation_id": vconversation_id, "task_id": vtask_id},
    }


def conversation_store_namespace(vconversation_id: str) -> tuple[str, str]:
    return ("conversation", vconversation_id)
IntentClassifier = Callable[[str], Awaitable[tuple[str, str]]]


@dataclasses.dataclass(frozen=True, slots=True)
class GraphInput:
    """One semantic step. Never audio.

    kind distinguishes a user utterance from a task result so the graph can fold a late result into
    context without treating it as something the user just said.
    """

    vkind: Literal["user_utterance", "task_result", "context"]
    vtext: str = ""
    vtask_id: str = ""
    vgoal: str = ""
    vpayload: dict[str, Any] = dataclasses.field(default_factory=dict)


async def default_classifier(vtext: str) -> tuple[str, str]:
    vlowered = vtext.casefold()
    if any(vword in vlowered for vword in ("flight", "book", "travel", "berlin")):
        return "travel", "travel planning"
    if any(vword in vlowered for vword in ("delete", "remove", "cancel subscription", "change")):
        return "mutation", "account changes"
    if any(vword in vlowered for vword in ("weather", "time", "temperature")):
        return "lookup", "quick facts"
    return "chitchat", "general"


def build_conversation_graph(
    *,
    vreasoning: ReasoningModel,
    vdelegate: DelegateFn,
    vclassifier: IntentClassifier | None = None,
) -> CompiledStateGraph:
    """The semantic conversation as a graph.

    Deliberately not asked to produce every spoken sentence: the realtime model owns speech, and
    this owns what is true, what is being worked on, and what the speech model should be told.
    """

    vclassify = vclassifier or default_classifier

    async def ingest_event(vstate: ChatState) -> dict[str, Any]:
        vinput: GraphInput = vstate["metadata"]["input"]
        if vinput.vkind == "user_utterance":
            return {"messages": [HumanMessage(content=vinput.vtext)]}
        if vinput.vkind == "task_result":
            return {
                "background_results": [
                    *vstate.get("background_results", []),
                    BackgroundResult(vtask_id=vinput.vtask_id, vgoal=vinput.vgoal, vsummary=vinput.vtext, vpayload=vinput.vpayload),
                ],
                "completed_tasks": [*vstate.get("completed_tasks", []), vinput.vtask_id],
                "active_tasks": [vid for vid in vstate.get("active_tasks", []) if vid != vinput.vtask_id],
            }
        return {"hidden_context": [*vstate.get("hidden_context", []), HiddenContextItem(vtext=vinput.vtext, vsource="system")]}

    async def classify_intent(vstate: ChatState) -> dict[str, Any]:
        vinput: GraphInput = vstate["metadata"]["input"]
        if vinput.vkind != "user_utterance":
            return {}
        vintent, vtopic = await vclassify(vinput.vtext)
        vprevious = vstate.get("user_intent", "")
        vepoch = vstate.get("conversation_epoch", 0)
        if vprevious and vintent != vprevious:
            vepoch += 1
        return {"user_intent": vintent, "current_topic": vtopic, "conversation_epoch": vepoch}

    async def decide_action(vstate: ChatState) -> dict[str, Any]:
        vinput: GraphInput = vstate["metadata"]["input"]
        if vinput.vkind != "user_utterance":
            return {"metadata": {**vstate["metadata"], "decision": "reconcile"}}
        vneeds_work = vstate.get("user_intent", "") in {"travel", "mutation"}
        return {"metadata": {**vstate["metadata"], "decision": "delegate" if vneeds_work else "respond"}}

    async def delegate(vstate: ChatState) -> dict[str, Any]:
        vinput: GraphInput = vstate["metadata"]["input"]
        vresponse = await vdelegate(vinput.vtext, TaskMode.BACKGROUND, vstate.get("user_intent", ""))
        vtask_id = str(vresponse.get("task_id", ""))
        return {
            "active_tasks": [*vstate.get("active_tasks", []), vtask_id] if vtask_id else vstate.get("active_tasks", []),
            "metadata": {**vstate["metadata"], "delegation": vresponse},
        }

    async def reconcile_result(vstate: ChatState) -> dict[str, Any]:
        vinput: GraphInput = vstate["metadata"]["input"]
        vresponse = await vreasoning.reason(
            ReasoningRequest(vgoal=f"Summarise for the caller: {vinput.vgoal}", vcontext=vinput.vtext)
        )
        return {"hidden_context": [*vstate.get("hidden_context", []), HiddenContextItem(vtext=vresponse.vtext, vsource="reasoning")]}

    async def prepare_realtime_instruction(vstate: ChatState) -> dict[str, Any]:
        vdecision = vstate["metadata"].get("decision", "respond")
        if vdecision == "delegate":
            vinstruction = "Tell the caller you are looking into it, and keep the conversation going."
        elif vdecision == "reconcile":
            vlatest = vstate.get("background_results", [])
            vinstruction = f"New result available: {vlatest[-1].vsummary}" if vlatest else "Continue."
        else:
            vinstruction = "Answer the caller directly."
        return {"metadata": {**vstate["metadata"], "instruction": vinstruction}}

    def route_after_decision(vstate: ChatState) -> str:
        vdecision = vstate["metadata"].get("decision", "respond")
        if vdecision == "delegate":
            return "delegate"
        if vdecision == "reconcile":
            return "reconcile_result"
        return "prepare_realtime_instruction"

    vbuilder = StateGraph(ChatState)
    vbuilder.add_node("ingest_event", ingest_event)
    vbuilder.add_node("classify_intent", classify_intent)
    vbuilder.add_node("decide_action", decide_action)
    vbuilder.add_node("delegate", delegate)
    vbuilder.add_node("reconcile_result", reconcile_result)
    vbuilder.add_node("prepare_realtime_instruction", prepare_realtime_instruction)

    vbuilder.add_edge(START, "ingest_event")
    vbuilder.add_edge("ingest_event", "classify_intent")
    vbuilder.add_edge("classify_intent", "decide_action")
    vbuilder.add_conditional_edges("decide_action", route_after_decision, ["delegate", "reconcile_result", "prepare_realtime_instruction"])
    vbuilder.add_edge("delegate", "prepare_realtime_instruction")
    vbuilder.add_edge("reconcile_result", "prepare_realtime_instruction")
    vbuilder.add_edge("prepare_realtime_instruction", END)
    return vbuilder.compile()
