from __future__ import annotations

import asyncio
import dataclasses
import datetime
import enum
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from examples import meet_workspace, small_agents
from examples.meet_memory import MeetMemory, background_brief

# A search across a company's documents is not instant; this keeps it a background task, as it would be.
DOCUMENT_SEARCH_DELAY_S = 5.0


class ToolWeight(enum.Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class ToolContext(Protocol):
    vmemory: MeetMemory
    vdelegate_llm: Any

    def requester(self) -> str: ...


ToolRun = Callable[[ToolContext, dict[str, object]], Awaitable[str]]


@dataclasses.dataclass(frozen=True)
class MeetTool:
    """A tool Karen can call. Its weight, not the model, decides how it runs.

    A light tool runs inside the reply and its result is answered at once. A heavy tool always runs in the
    background: the model only learns that it started, and the result is told when it arrives.
    """

    vname: str
    vdescription: str
    vparameters: dict[str, object]
    vweight: ToolWeight
    vrun: ToolRun

    def schema(self) -> dict[str, object]:
        vdescription = self.vdescription
        if self.vweight is ToolWeight.HEAVY:
            vdescription += " Runs in the background: it returns at once and the answer is told when it is ready."
        return {"type": "function", "name": self.vname, "description": vdescription, "parameters": self.vparameters}

    def goal(self, varguments: dict[str, object]) -> str:
        return str(varguments.get("question") or self.vname.replace("_", " "))


async def current_time(_vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return small_agents.get_current_time.invoke(varguments)


async def list_tasks(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return meet_workspace.list_tasks(str(varguments.get("assignee", "")), str(varguments.get("status", "")), vcontext.requester())


async def get_task(_vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return meet_workspace.get_task(str(varguments.get("key", "")))


async def get_schedule(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return meet_workspace.get_schedule(str(varguments.get("person", "")), str(varguments.get("day", "")), vcontext.requester(), datetime.date.today())


async def find_free_slot(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    vpeople = varguments.get("people")
    vpeople = vpeople if isinstance(vpeople, list) else []
    try:
        vminutes = max(int(float(str(varguments.get("minutes") or 30))), 1)
    except ValueError:
        vminutes = 30
    return meet_workspace.find_free_slot([str(vperson) for vperson in vpeople], vminutes, str(varguments.get("day", "")), vcontext.requester(), datetime.datetime.now())


async def who_is(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return meet_workspace.who_is(str(varguments.get("person_or_topic", "")), vcontext.requester())


async def search_documents(_vcontext: ToolContext, varguments: dict[str, object]) -> str:
    await asyncio.sleep(DOCUMENT_SEARCH_DELAY_S)
    return meet_workspace.search_documents(str(varguments.get("query", "")))


async def research(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    vbrief = background_brief(str(varguments.get("question", "")), vcontext.requester(), vcontext.vmemory)
    return str((await vcontext.vdelegate_llm.ainvoke(vbrief)).content)


DAY_PARAMETER = {"type": "string", "description": "today, tomorrow, or a date as YYYY-MM-DD"}

MEET_TOOLS = (
    MeetTool(
        "list_tasks",
        "The payments team's task tracker for the migration to the new payment provider: who does what, status and due date. "
        "Filter by assignee (a name, or 'me' for whoever is asking) and status (to do, in progress, in review, blocked, done).",
        {"type": "object", "properties": {"assignee": {"type": "string"}, "status": {"type": "string"}}, "required": []},
        ToolWeight.LIGHT,
        list_tasks,
    ),
    MeetTool(
        "get_task",
        "One task from the tracker by its key, such as PAY-101.",
        {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
        ToolWeight.LIGHT,
        get_task,
    ),
    MeetTool(
        "get_schedule",
        "What is in one team member's calendar on a day.",
        {"type": "object", "properties": {"person": {"type": "string"}, "day": DAY_PARAMETER}, "required": ["person"]},
        ToolWeight.LIGHT,
        get_schedule,
    ),
    MeetTool(
        "find_free_slot",
        "The first time on a day when all the given team members are free, for a meeting of the given length.",
        {
            "type": "object",
            "properties": {"people": {"type": "array", "items": {"type": "string"}}, "minutes": {"type": "integer"}, "day": DAY_PARAMETER},
            "required": ["people"],
        },
        ToolWeight.LIGHT,
        find_free_slot,
    ),
    MeetTool(
        "who_is",
        "Who someone on the team is, or who owns an area (webhooks, the provider contract, testing, ...). "
        "Pass 'everyone' for the whole team with each person's area.",
        {"type": "object", "properties": {"person_or_topic": {"type": "string"}}, "required": ["person_or_topic"]},
        ToolWeight.LIGHT,
        who_is,
    ),
    MeetTool(
        "search_documents",
        "Search the team's documents: decisions (ADRs), runbooks, meeting notes and policies.",
        {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        ToolWeight.HEAVY,
        search_documents,
    ),
    MeetTool(
        "get_current_time",
        "Current date and time in an IANA timezone such as Asia/Tokyo.",
        {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": []},
        ToolWeight.LIGHT,
        current_time,
    ),
    MeetTool(
        "research",
        "Research, analysis, drafting or careful checking of anything the meeting has not already settled.",
        {
            "type": "object",
            "properties": {"question": {"type": "string", "description": "Self-contained question, naming the facts it depends on"}},
            "required": ["question"],
        },
        ToolWeight.HEAVY,
        research,
    ),
)
MEET_TOOLS_BY_NAME = {vtool.vname: vtool for vtool in MEET_TOOLS}
