from __future__ import annotations

import asyncio
import dataclasses
import enum
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from examples import small_agents
from examples.meet_memory import MeetMemory, background_brief

SCIENCE_FACT_DELAY_S = 8.0
SCIENCE_FACTS = (
    "A day on Venus is longer than its year: it turns once every 243 Earth days but orbits the Sun in 225.",
    "Octopuses have three hearts, and two of them stop beating while they swim.",
    "Honey found in Egyptian tombs was still edible after about 3,000 years.",
    "A teaspoon of neutron star material would weigh around a billion tonnes on Earth.",
    "Bananas are slightly radioactive because they contain potassium-40.",
    "Light from the Sun takes about 8 minutes and 20 seconds to reach Earth.",
    "Water can boil and freeze at the same time at its triple point, about 0.01 °C and 611 pascals.",
    "There are more possible chess games than atoms in the observable universe.",
)


class ToolWeight(enum.Enum):
    LIGHT = "light"
    HEAVY = "heavy"


class ToolContext(Protocol):
    vmemory: MeetMemory
    vdelegate_llm: Any

    def requester(self) -> str: ...

    def next_fact(self) -> str: ...


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


async def current_weather(_vcontext: ToolContext, varguments: dict[str, object]) -> str:
    return small_agents.get_current_weather.invoke(varguments)


async def science_fact(vcontext: ToolContext, _varguments: dict[str, object]) -> str:
    await asyncio.sleep(SCIENCE_FACT_DELAY_S)
    return vcontext.next_fact()


async def research(vcontext: ToolContext, varguments: dict[str, object]) -> str:
    vbrief = background_brief(str(varguments.get("question", "")), vcontext.requester(), vcontext.vmemory)
    return str((await vcontext.vdelegate_llm.ainvoke(vbrief)).content)


MEET_TOOLS = (
    MeetTool(
        "get_current_time",
        "Current date and time in an IANA timezone such as Asia/Tokyo.",
        {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": []},
        ToolWeight.LIGHT,
        current_time,
    ),
    MeetTool(
        "get_current_weather",
        "Current weather for a city (placeholder data).",
        {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
        ToolWeight.LIGHT,
        current_weather,
    ),
    MeetTool(
        "science_fact",
        "A random science fact. Call it whenever someone wants a fact; never tell one from your own knowledge.",
        {"type": "object", "properties": {}, "required": []},
        ToolWeight.HEAVY,
        science_fact,
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
