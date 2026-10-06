from __future__ import annotations

import asyncio
import logging
import time
from typing import cast

from voice_agent.agent.actions.audit import AuditSink, audit_entry
from voice_agent.agent.actions.commands import CommandRegistry
from voice_agent.agent.actions.models import (
    ActionOutcome,
    ActionProposal,
    ActionRecord,
    ActionStatus,
    Command,
    FailureCode,
    new_action_id,
)
from voice_agent.agent.actions.models import idempotency_key as build_idempotency_key
from voice_agent.agent.actions.policy import ActionPolicy
from voice_agent.agent.actions.store import ActionStore

logger = logging.getLogger("voice_agent.actions")


class ActionService:
    """The only path from a model's intent to a side effect.

    Every important write goes prepare -> (confirm) -> commit. Commit executes the command that
    prepare built and stored, so nothing said between the confirmation and the write can change
    what happens.
    """

    def __init__(
        self,
        *,
        vregistry: CommandRegistry,
        vstore: ActionStore,
        vpolicy: ActionPolicy | None = None,
        vaudit: AuditSink | None = None,
    ) -> None:
        self.vregistry = vregistry
        self.vstore = vstore
        self.vpolicy = vpolicy or ActionPolicy()
        self.vaudit = vaudit
        self._vlocks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, vaction_id: str) -> asyncio.Lock:
        vlock = self._vlocks.get(vaction_id)
        if vlock is None:
            vlock = asyncio.Lock()
            self._vlocks[vaction_id] = vlock
        return vlock

    async def _audit(self, vrecord: ActionRecord, vphase: str, vactor: str) -> None:
        if self.vaudit is not None and self.vregistry.require(vrecord.vaction_type).vmetadata.vaudit_required:
            await self.vaudit.record(audit_entry(vrecord, vphase, vactor))

    async def prepare(
        self,
        *,
        vaction_type: str,
        varguments: dict[str, object],
        vconversation_id: str,
        vsession_id: str,
        vuser_id: str,
        vconversation_epoch: int,
        vsource_turn_id: str | None = None,
        vfrom_background: bool = False,
    ) -> tuple[ActionRecord, ActionOutcome | None]:
        vhandler = self.vregistry.require(vaction_type)
        vprepared = await vhandler.prepare(varguments)
        vkey = build_idempotency_key(vconversation_id, vaction_type, vprepared.vresolved_target, vprepared.vparameters)

        vexisting = await self.vstore.find_by_idempotency_key(vkey)
        if vexisting is not None and vexisting.vstatus is ActionStatus.SUCCEEDED:
            logger.info("action already executed", extra=vexisting.log_fields())
            return vexisting, ActionOutcome(
                vsucceeded=True,
                vresult=vexisting.voutcome.vresult if vexisting.voutcome else {},
                vfailure=FailureCode.ALREADY_EXECUTED,
                vmessage="this action was already executed",
            )

        vmetadata = vhandler.vmetadata
        vdecision = self.vpolicy.decide(vmetadata, vfrom_background=vfrom_background)
        vproposal = ActionProposal(
            vaction_id=new_action_id(),
            vaction_type=vaction_type,
            vresolved_target=vprepared.vresolved_target,
            vparameters=vprepared.vparameters,
            vimpact=vprepared.vimpact,
            vsummary=vprepared.vsummary,
            vrequires_confirmation=vdecision.vrequires_confirmation,
            veffect_level=vmetadata.veffect_level,
            vidempotency_key=vkey,
            vexpires_at_s=time.time() + vmetadata.vconfirmation_ttl_s,
            vprecondition=vprepared.vprecondition,
        )
        vrecord = ActionRecord(
            vaction_id=vproposal.vaction_id,
            vconversation_id=vconversation_id,
            vsession_id=vsession_id,
            vuser_id=vuser_id,
            vaction_type=vaction_type,
            vproposal=vproposal,
            veffect_level=vmetadata.veffect_level,
            vstatus=ActionStatus.DRAFT,
            vidempotency_key=vkey,
            vsource_turn_id=vsource_turn_id,
            vconversation_epoch=vconversation_epoch,
            vcommand=vprepared.vcommand,
        )
        vrecord.transition_to(ActionStatus.VALIDATED)
        vrecord.transition_to(ActionStatus.AWAITING_CONFIRMATION if vdecision.vrequires_confirmation else ActionStatus.APPROVED)
        if vrecord.vstatus is ActionStatus.APPROVED:
            vrecord.vapproved_at_s = time.time()
        await self.vstore.put(vrecord)
        await self._audit(vrecord, "prepared", "langgraph")
        logger.info("action prepared", extra=vrecord.log_fields())
        return vrecord, None

    async def confirm(self, vaction_id: str, *, vactor: str = "user") -> tuple[ActionRecord | None, ActionOutcome | None]:
        vrecord = await self.vstore.get(vaction_id)
        if vrecord is None:
            return None, ActionOutcome(False, vfailure=FailureCode.NOT_FOUND, vmessage="unknown action")
        if vrecord.vstatus is ActionStatus.APPROVED:
            return vrecord, None
        if vrecord.vstatus is not ActionStatus.AWAITING_CONFIRMATION:
            return vrecord, ActionOutcome(False, vfailure=FailureCode.ACTION_CANCELLED, vmessage=f"action is {vrecord.vstatus.value}")
        if vrecord.vproposal.is_expired():
            vrecord.transition_to(ActionStatus.EXPIRED)
            await self.vstore.put(vrecord)
            await self._audit(vrecord, "expired", vactor)
            return vrecord, ActionOutcome(False, vfailure=FailureCode.CONFIRMATION_EXPIRED, vmessage="confirmation window expired")
        vrecord.transition_to(ActionStatus.APPROVED)
        vrecord.vapproved_at_s = time.time()
        await self.vstore.put(vrecord)
        await self._audit(vrecord, "confirmed", vactor)
        return vrecord, None

    async def commit(self, vaction_id: str, *, vactor: str = "langgraph") -> tuple[ActionRecord | None, ActionOutcome]:
        vrecord = await self.vstore.get(vaction_id)
        if vrecord is None:
            return None, ActionOutcome(False, vfailure=FailureCode.NOT_FOUND, vmessage="unknown action")

        async with self._lock_for(vaction_id):
            vrecord = await self.vstore.get(vaction_id) or vrecord
            if vrecord.vstatus is ActionStatus.SUCCEEDED and vrecord.voutcome is not None:
                return vrecord, ActionOutcome(
                    vsucceeded=True,
                    vresult=vrecord.voutcome.vresult,
                    vfailure=FailureCode.ALREADY_EXECUTED,
                    vmessage="already executed",
                )
            if vrecord.vstatus is ActionStatus.AWAITING_CONFIRMATION:
                if vrecord.vproposal.is_expired():
                    vrecord.transition_to(ActionStatus.EXPIRED)
                    await self.vstore.put(vrecord)
                    return vrecord, ActionOutcome(False, vfailure=FailureCode.CONFIRMATION_EXPIRED, vmessage="confirmation expired")
                return vrecord, ActionOutcome(False, vfailure=FailureCode.CONFIRMATION_REQUIRED, vmessage=vrecord.vproposal.vsummary)
            if vrecord.vstatus in {ActionStatus.CANCELLED, ActionStatus.SUPERSEDED, ActionStatus.EXPIRED}:
                return vrecord, ActionOutcome(False, vfailure=FailureCode.ACTION_CANCELLED, vmessage=f"action is {vrecord.vstatus.value}")
            if vrecord.vstatus is not ActionStatus.APPROVED:
                return vrecord, ActionOutcome(False, vfailure=FailureCode.VALIDATION_FAILED, vmessage=f"action is {vrecord.vstatus.value}")

            vhandler = self.vregistry.require(vrecord.vaction_type)
            vrecord.transition_to(ActionStatus.EXECUTING)
            await self.vstore.put(vrecord)
            try:
                voutcome = await vhandler.execute(cast(Command, vrecord.vcommand), vrecord.vproposal)
            except Exception as vexc:  # an unexpected handler failure must not leave the action EXECUTING forever
                voutcome = ActionOutcome(False, vfailure=FailureCode.EXTERNAL_SERVICE_FAILED, vmessage=f"{type(vexc).__name__}: {vexc}")
                logger.warning("action handler raised", extra={**vrecord.log_fields(), "error": voutcome.vmessage})

            vrecord.voutcome = voutcome
            vrecord.vexecuted_at_s = time.time()
            if voutcome.vsucceeded:
                vrecord.transition_to(ActionStatus.SUCCEEDED)
            elif voutcome.vfailure is FailureCode.CONFLICT:
                vrecord.transition_to(ActionStatus.CONFLICTED)
            else:
                vrecord.transition_to(ActionStatus.FAILED)
            await self.vstore.put(vrecord)
            await self._audit(vrecord, "executed", vactor)
            logger.info("action committed", extra={**vrecord.log_fields(), "succeeded": voutcome.vsucceeded})
            return vrecord, voutcome

    async def cancel(self, vaction_id: str, *, vactor: str = "user") -> ActionRecord | None:
        vrecord = await self.vstore.get(vaction_id)
        if vrecord is None or vrecord.vterminal:
            return vrecord
        vrecord.transition_to(ActionStatus.CANCELLED)
        await self.vstore.put(vrecord)
        await self._audit(vrecord, "cancelled", vactor)
        return vrecord

    async def supersede(self, vaction_id: str, vsuperseded_by: str) -> ActionRecord | None:
        vrecord = await self.vstore.get(vaction_id)
        if vrecord is None or vrecord.vterminal:
            return vrecord
        vrecord.transition_to(ActionStatus.SUPERSEDED)
        vrecord.vsuperseded_by = vsuperseded_by
        await self.vstore.put(vrecord)
        return vrecord

    async def status(self, vaction_id: str) -> ActionRecord | None:
        return await self.vstore.get(vaction_id)
