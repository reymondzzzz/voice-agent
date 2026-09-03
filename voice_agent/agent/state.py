from __future__ import annotations

import dataclasses
import time
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundResult:
    vtask_id: str
    vgoal: str
    vsummary: str
    vpayload: dict[str, Any]
    vdelivered: bool = False
    vrecorded_at_s: float = dataclasses.field(default_factory=time.time)


@dataclasses.dataclass(frozen=True, slots=True)
class HiddenContextItem:
    """Knowledge the model may use but must not read out as if it had said it.

    Kept out of `messages` deliberately: raw tool logs in visible history make the assistant recite
    internals, and make later turns harder to reason about.
    """

    vtext: str
    vsource: str
    vrecorded_at_s: float = dataclasses.field(default_factory=time.time)


class ChatState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    conversation_id: str
    session_id: str
    conversation_epoch: int
    current_turn_id: str | None
    current_topic: str
    user_intent: str
    active_tasks: list[str]
    completed_tasks: list[str]
    background_results: list[BackgroundResult]
    hidden_context: list[HiddenContextItem]
    tool_results: list[dict[str, Any]]
    memory_refs: list[str]
    pending_actions: list[str]
    metadata: dict[str, Any]


def new_chat_state(*, vconversation_id: str, vsession_id: str) -> ChatState:
    return ChatState(
        messages=[],
        conversation_id=vconversation_id,
        session_id=vsession_id,
        conversation_epoch=0,
        current_turn_id=None,
        current_topic="",
        user_intent="",
        active_tasks=[],
        completed_tasks=[],
        background_results=[],
        hidden_context=[],
        tool_results=[],
        memory_refs=[],
        pending_actions=[],
        metadata={},
    )
