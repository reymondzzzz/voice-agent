from __future__ import annotations

import dataclasses
from typing import Protocol


@dataclasses.dataclass(frozen=True, slots=True)
class ReasoningRequest:
    vgoal: str
    vcontext: str = ""
    vmax_tokens: int = 512


@dataclasses.dataclass(frozen=True, slots=True)
class ReasoningResponse:
    vtext: str
    vmodel: str
    vusage: dict[str, object] = dataclasses.field(default_factory=dict)


class ReasoningModel(Protocol):
    """Heavy thinking, deliberately separate from the speech model.

    The realtime model handles conversation; anything that needs planning, synthesis or
    verification goes here, so the speech backend can be swapped without touching reasoning and a
    remote frontier model can be used without it ever seeing audio.
    """

    @property
    def vmodel_name(self) -> str: ...

    async def reason(self, vrequest: ReasoningRequest) -> ReasoningResponse: ...


class ScriptedReasoningModel:
    def __init__(self, vresponses: list[str] | None = None, *, vmodel_name: str = "scripted") -> None:
        self._vresponses = list(vresponses or [])
        self._vmodel_name = vmodel_name
        self.vrequests: list[ReasoningRequest] = []

    @property
    def vmodel_name(self) -> str:
        return self._vmodel_name

    async def reason(self, vrequest: ReasoningRequest) -> ReasoningResponse:
        self.vrequests.append(vrequest)
        vtext = self._vresponses.pop(0) if self._vresponses else f"considered: {vrequest.vgoal}"
        return ReasoningResponse(vtext=vtext, vmodel=self._vmodel_name)
