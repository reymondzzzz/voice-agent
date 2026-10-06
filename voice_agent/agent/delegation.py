from __future__ import annotations

import dataclasses
import logging
from typing import Any

from voice_agent.agent.actions.models import ActionStatus, FailureCode
from voice_agent.agent.actions.service import ActionService
from voice_agent.agent.tasks.models import TaskMode, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor

logger = logging.getLogger("voice_agent.delegation")

REALTIME_TOOL_NAMES = (
    "delegate_task",
    "cancel_task",
    "get_task_status",
    "request_action",
    "confirm_action",
    "cancel_action",
    "get_action_status",
)

REALTIME_TOOL_SCHEMAS: tuple[dict[str, Any], ...] = (
    {
        "name": "delegate_task",
        "description": (
            "Ask the agent layer to do work. Use mode='quick' only for something the caller must wait to hear, "
            "and mode='background' for anything slower, which lets you keep talking."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "mode": {"type": "string", "enum": ["quick", "background"]},
                "intent": {"type": "string"},
            },
            "required": ["goal", "mode"],
        },
    },
    {"name": "cancel_task", "description": "Cancel work the caller no longer wants.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {"name": "get_task_status", "description": "Check delegated work.", "parameters": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]}},
    {
        "name": "request_action",
        "description": (
            "Propose an operation that changes something. This never performs the change: it returns a resolved "
            "summary, and an important action must then be confirmed by the caller."
        ),
        "parameters": {
            "type": "object",
            "properties": {"action_type": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["action_type", "arguments"],
        },
    },
    {"name": "confirm_action", "description": "Confirm one exact prepared action by id after the caller agreed to its summary.", "parameters": {"type": "object", "properties": {"action_id": {"type": "string"}}, "required": ["action_id"]}},
    {"name": "cancel_action", "description": "Cancel a prepared action.", "parameters": {"type": "object", "properties": {"action_id": {"type": "string"}}, "required": ["action_id"]}},
    {"name": "get_action_status", "description": "Check a prepared action.", "parameters": {"type": "object", "properties": {"action_id": {"type": "string"}}, "required": ["action_id"]}},
)


@dataclasses.dataclass(frozen=True, slots=True)
class DelegationContext:
    vconversation_id: str
    vsession_id: str
    vuser_id: str
    vconversation_epoch: int
    vturn_id: str | None


class DelegationAPI:
    """The whole surface the speech model may touch.

    It never exposes a database, an HTTP client or a domain tool. request_action deliberately stops
    at a proposal so that a model cannot reach an effect in one step, and background delegation
    returns immediately so the original tool call completes while the work continues.
    """

    def __init__(
        self,
        *,
        vsupervisor: TaskSupervisor,
        vregistry: TaskRegistry,
        vactions: ActionService,
    ) -> None:
        self.vsupervisor = vsupervisor
        self.vregistry = vregistry
        self.vactions = vactions

    async def delegate_task(self, *, vgoal: str, vmode: str, vintent: str, vcontext: DelegationContext) -> dict[str, Any]:
        vtask_mode = TaskMode(vmode)
        vspec = TaskSpec(vgoal=vgoal, vmode=vtask_mode, vintent=vintent)
        vrecord = self.vsupervisor.create_record(
            vspec=vspec,
            vconversation_epoch=vcontext.vconversation_epoch,
            vsource_turn_id=vcontext.vturn_id,
        )
        vsuperseded = self.vsupervisor.supersede_duplicates(vrecord)

        if vtask_mode is TaskMode.QUICK:
            await self.vsupervisor.run_quick(vrecord)
            return {
                "status": vrecord.vstatus.value,
                "task_id": vrecord.vtask_id,
                "result": vrecord.vresult.vpayload if vrecord.vresult else {},
                "summary": vrecord.vresult.vsummary if vrecord.vresult else (vrecord.verror or ""),
            }

        self.vsupervisor.start_background(vrecord)
        return {
            "status": "started",
            "task_id": vrecord.vtask_id,
            "superseded": [vold.vtask_id for vold in vsuperseded],
            "note": "work continues in the background; its result will arrive as a separate event",
        }

    async def cancel_task(self, *, vtask_id: str) -> dict[str, Any]:
        vrecord = self.vsupervisor.cancel(vtask_id)
        if vrecord is None:
            return {"status": "not_found", "task_id": vtask_id}
        return {"status": vrecord.vstatus.value, "task_id": vtask_id}

    async def get_task_status(self, *, vtask_id: str) -> dict[str, Any]:
        vrecord = self.vregistry.get(vtask_id)
        if vrecord is None:
            return {"status": "not_found", "task_id": vtask_id}
        return {
            "status": vrecord.vstatus.value,
            "task_id": vtask_id,
            "goal": vrecord.vgoal,
            "progress": vrecord.vprogress,
            "result": vrecord.vresult.vpayload if vrecord.vstatus is TaskStatus.COMPLETED and vrecord.vresult else {},
        }

    async def request_action(self, *, vaction_type: str, varguments: dict[str, Any], vcontext: DelegationContext, vfrom_background: bool = False) -> dict[str, Any]:
        vrecord, voutcome = await self.vactions.prepare(
            vaction_type=vaction_type,
            varguments=varguments,
            vconversation_id=vcontext.vconversation_id,
            vsession_id=vcontext.vsession_id,
            vuser_id=vcontext.vuser_id,
            vconversation_epoch=vcontext.vconversation_epoch,
            vsource_turn_id=vcontext.vturn_id,
            vfrom_background=vfrom_background,
        )
        if voutcome is not None and voutcome.vfailure is FailureCode.ALREADY_EXECUTED:
            return {"status": "already_executed", "action_id": vrecord.vaction_id, "result": voutcome.vresult}
        return {
            "status": vrecord.vstatus.value,
            "action_id": vrecord.vaction_id,
            "summary": vrecord.vproposal.vsummary,
            "impact": vrecord.vproposal.vimpact,
            "requires_confirmation": vrecord.vproposal.vrequires_confirmation,
            "expires_at": vrecord.vproposal.vexpires_at_s,
        }

    async def confirm_action(self, *, vaction_id: str) -> dict[str, Any]:
        vrecord, vfailure = await self.vactions.confirm(vaction_id)
        if vrecord is None or vfailure is not None:
            return {"status": "refused", "action_id": vaction_id, "reason": vfailure.vfailure.value if vfailure and vfailure.vfailure else "unknown"}
        vcommitted, voutcome = await self.vactions.commit(vaction_id)
        return {
            "status": vcommitted.vstatus.value if vcommitted else ActionStatus.FAILED.value,
            "action_id": vaction_id,
            "succeeded": voutcome.vsucceeded,
            "result": voutcome.vresult,
            "failure": voutcome.vfailure.value if voutcome.vfailure else None,
            "message": voutcome.vmessage,
        }

    async def cancel_action(self, *, vaction_id: str) -> dict[str, Any]:
        vrecord = await self.vactions.cancel(vaction_id)
        if vrecord is None:
            return {"status": "not_found", "action_id": vaction_id}
        return {"status": vrecord.vstatus.value, "action_id": vaction_id}

    async def get_action_status(self, *, vaction_id: str) -> dict[str, Any]:
        vrecord = await self.vactions.status(vaction_id)
        if vrecord is None:
            return {"status": "not_found", "action_id": vaction_id}
        return {
            "status": vrecord.vstatus.value,
            "action_id": vaction_id,
            "summary": vrecord.vproposal.vsummary,
            "succeeded": vrecord.voutcome.vsucceeded if vrecord.voutcome else None,
        }
