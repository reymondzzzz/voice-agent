from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from voice_agent.agent.delivery.mailbox import AsyncioSessionMailbox
from voice_agent.agent.delivery.policy import DeliveryContext, DeliveryDecision, DeliveryPolicy
from voice_agent.agent.events import (
    ActionAwaitingConfirmation,
    BackgroundTaskCompleted,
    BackgroundTaskFailed,
    SemanticEvent,
)
from voice_agent.agent.conversation.runtime import ConversationRuntime
from voice_agent.agent.delegation import DelegationAPI, DelegationContext
from voice_agent.realtime import events
from voice_agent.realtime.session import RealtimeSpeechSession

logger = logging.getLogger("voice_agent.bridge")

TranscriptRouter = Callable[[str], Awaitable[tuple[str, dict[str, object]] | None]]


class RealtimeAgentBridge:
    """Translates between the audio plane and the semantic plane, and owns nothing else.

    All orchestration lives here rather than in transport callbacks, so the whole agent can be
    driven by a fake session in tests. The bridge never executes business logic itself: it routes
    tool calls to the delegation API and results back.
    """

    def __init__(
        self,
        *,
        vsession: RealtimeSpeechSession,
        vmailbox: AsyncioSessionMailbox,
        vdelegation: DelegationAPI,
        vruntime: ConversationRuntime,
        vuser_id: str = "user",
        vpolicy: DeliveryPolicy | None = None,
        vtranscript_router: TranscriptRouter | None = None,
        von_final_transcript: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.vsession = vsession
        self.vmailbox = vmailbox
        self.vdelegation = vdelegation
        self.vruntime = vruntime
        self.vuser_id = vuser_id
        self.vpolicy = vpolicy or DeliveryPolicy()
        self.vtranscript_router = vtranscript_router
        self.von_final_transcript = von_final_transcript
        self.vqueued: list[SemanticEvent] = []
        self.vdelivered: list[SemanticEvent] = []
        self._vtasks: set[asyncio.Task[None]] = set()

    def delegation_context(self) -> DelegationContext:
        return DelegationContext(
            vconversation_id=self.vruntime.vconversation_id,
            vsession_id=self.vruntime.vsession_id,
            vuser_id=self.vuser_id,
            vconversation_epoch=self.vruntime.vconversation_epoch,
            vturn_id=self.vruntime.vcurrent_turn_id,
        )

    async def run(self) -> None:
        async with asyncio.TaskGroup() as vgroup:
            vgroup.create_task(self._consume_realtime(), name="bridge-realtime")
            vgroup.create_task(self._consume_mailbox(), name="bridge-mailbox")

    async def _consume_realtime(self) -> None:
        async for vevent in self.vsession.events():
            await self.handle_realtime_event(vevent)

    async def _consume_mailbox(self) -> None:
        async for vevent in self.vmailbox.subscribe():
            await self.handle_semantic_event(vevent)

    async def handle_realtime_event(self, vevent: events.RealtimeEvent) -> None:
        if self.vruntime.seen(vevent.vcorrelation.vevent_id):
            logger.debug("duplicate realtime event ignored", extra=vevent.vcorrelation.log_fields())
            return

        match vevent:
            case events.UserSpeechStarted():
                self.vruntime.vuser_speaking = True
                self.vruntime.vcurrent_turn_id = vevent.vcorrelation.vturn_id
            case events.UserSpeechStopped():
                self.vruntime.vuser_speaking = False
                self.vruntime.vlast_user_speech_ended_s = vevent.vcorrelation.vtimestamp_s
                await self.flush_queued()
            case events.UserTranscriptFinal():
                await self._on_final_transcript(vevent)
            case events.AssistantSpeechStarted():
                self.vruntime.vassistant_speaking = True
            case events.AssistantSpeechStopped():
                self.vruntime.vassistant_speaking = False
                await self.flush_queued()
            case events.RealtimeInterrupted():
                self.vruntime.vassistant_speaking = False
            case events.RealtimeToolCallRequested():
                await self._on_tool_call(vevent)
            case events.RealtimeSessionError():
                logger.warning("realtime session error", extra={**vevent.vcorrelation.log_fields(), "message": vevent.vmessage})

    async def _on_final_transcript(self, vevent: events.UserTranscriptFinal) -> None:
        if self.von_final_transcript is not None:
            await self.von_final_transcript(vevent.vtext)
        if self.vsession.vcapabilities.requires_transcript_router() and self.vtranscript_router is not None:
            vrouted = await self.vtranscript_router(vevent.vtext)
            if vrouted is not None:
                vtool_name, varguments = vrouted
                await self._dispatch_tool(vtool_name, varguments, vtool_call_id=f"routed:{vevent.vcorrelation.vevent_id}", vcorrelation=vevent.vcorrelation)

    async def _on_tool_call(self, vevent: events.RealtimeToolCallRequested) -> None:
        if self.vruntime.tool_call_seen(vevent.vtool_call_id):
            logger.info("duplicate tool call ignored", extra={**vevent.vcorrelation.log_fields(), "tool_call_id": vevent.vtool_call_id})
            return
        await self._dispatch_tool(vevent.vtool_name, vevent.varguments, vtool_call_id=vevent.vtool_call_id, vcorrelation=vevent.vcorrelation)

    async def _dispatch_tool(self, vtool_name: str, varguments: dict[str, object], *, vtool_call_id: str, vcorrelation) -> dict[str, object]:
        vcontext = self.delegation_context()
        try:
            match vtool_name:
                case "delegate_task":
                    vresult = await self.vdelegation.delegate_task(
                        vgoal=str(varguments.get("goal", "")),
                        vmode=str(varguments.get("mode", "background")),
                        vintent=str(varguments.get("intent", "")),
                        vcontext=vcontext,
                    )
                case "cancel_task":
                    vresult = await self.vdelegation.cancel_task(vtask_id=str(varguments.get("task_id", "")))
                case "get_task_status":
                    vresult = await self.vdelegation.get_task_status(vtask_id=str(varguments.get("task_id", "")))
                case "request_action":
                    vresult = await self.vdelegation.request_action(
                        vaction_type=str(varguments.get("action_type", "")),
                        varguments=dict(varguments.get("arguments", {}) or {}),
                        vcontext=vcontext,
                    )
                case "confirm_action":
                    vresult = await self.vdelegation.confirm_action(vaction_id=str(varguments.get("action_id", "")))
                case "cancel_action":
                    vresult = await self.vdelegation.cancel_action(vaction_id=str(varguments.get("action_id", "")))
                case "get_action_status":
                    vresult = await self.vdelegation.get_action_status(vaction_id=str(varguments.get("action_id", "")))
                case _:
                    vresult = {"status": "unknown_tool", "tool": vtool_name}
        except Exception as vexc:  # a tool failure is an answer to the model, never a dead session
            logger.warning("tool dispatch failed", extra={"tool": vtool_name, "error": f"{type(vexc).__name__}: {vexc}"})
            vresult = {"status": "error", "tool": vtool_name, "message": f"{type(vexc).__name__}"}

        await self._return_tool_result(vtool_call_id, vresult, vcorrelation)
        return vresult

    async def _return_tool_result(self, vtool_call_id: str, vresult: dict[str, object], vcorrelation) -> None:
        if self.vsession.vcapabilities.supports_tool_results:
            await self.vsession.send_tool_result(events.ToolResultPayload(vtool_call_id=vtool_call_id, vresult=vresult, vcorrelation=vcorrelation))
            return
        await self.vsession.add_context(
            events.SessionContextUpdate(
                vscope=events.ContextScope.BACKGROUND,
                vtext=f"tool {vtool_call_id} returned: {vresult}",
                vcorrelation=vcorrelation,
            )
        )

    async def handle_semantic_event(self, vevent: SemanticEvent) -> None:
        vcontext = DeliveryContext(
            vuser_speaking=self.vruntime.vuser_speaking,
            vassistant_speaking=self.vruntime.vassistant_speaking,
            vconversation_epoch=self.vruntime.vconversation_epoch,
            vcurrent_intent=self.vruntime.vcurrent_intent,
            vidle_seconds=self.vruntime.vidle_seconds,
        )
        vdecision = self.vpolicy.decide(vevent, vcontext)
        logger.info(
            "delivery decision",
            extra={**vevent.vcorrelation.log_fields(), "task_id": vevent.vtask_id, "decision": vdecision.value, "event_type": type(vevent).__name__},
        )
        await self.apply_decision(vevent, vdecision)

    async def apply_decision(self, vevent: SemanticEvent, vdecision: DeliveryDecision) -> None:
        match vdecision:
            case DeliveryDecision.DROP:
                return
            case DeliveryDecision.QUEUE:
                self.vqueued.append(vevent)
            case DeliveryDecision.STORE_SILENTLY:
                await self._inject_context(vevent)
            case DeliveryDecision.SPEAK_NOW:
                await self._inject_context(vevent)
                await self._request_response(vevent)
            case DeliveryDecision.INTERRUPT:
                await self.vsession.interrupt(events.InterruptRequest(vcorrelation=vevent.vcorrelation, vreason="correction"))
                self.vruntime.vassistant_speaking = False
                await self._inject_context(vevent)
                await self._request_response(vevent)

    async def _inject_context(self, vevent: SemanticEvent) -> None:
        if not self.vsession.vcapabilities.context_injection:
            return
        await self.vsession.add_context(
            events.SessionContextUpdate(
                vscope=events.ContextScope.BACKGROUND,
                vtext=describe_semantic_event(vevent),
                vcorrelation=vevent.vcorrelation,
                vmetadata={"task_id": vevent.vtask_id, "criticality": vevent.vcriticality.value},
            )
        )
        self.vdelivered.append(vevent)

    async def _request_response(self, vevent: SemanticEvent) -> None:
        if not self.vsession.vcapabilities.supports_manual_response_trigger:
            return
        await self.vsession.request_response(
            events.ResponseRequest(
                vcorrelation=vevent.vcorrelation,
                vinstructions="Share this new information naturally if it is still relevant to the caller.",
            )
        )

    async def flush_queued(self) -> None:
        if not self.vqueued or self.vruntime.vuser_speaking or self.vruntime.vassistant_speaking:
            return
        vpending, self.vqueued = self.vqueued, []
        for vevent in vpending:
            await self._inject_context(vevent)
        if vpending:
            await self._request_response(vpending[-1])

    async def aclose(self) -> None:
        for vtask in self._vtasks:
            vtask.cancel()
        for vtask in self._vtasks:
            with contextlib.suppress(asyncio.CancelledError):
                await vtask


def describe_semantic_event(vevent: SemanticEvent) -> str:
    match vevent:
        case BackgroundTaskCompleted():
            vsummary = vevent.vresult.vsummary if vevent.vresult else ""
            return f"Background work finished — {vevent.vgoal}: {vsummary}"
        case BackgroundTaskFailed():
            return f"Background work failed — {vevent.vgoal}: {vevent.verror}"
        case ActionAwaitingConfirmation():
            return f"Awaiting confirmation: {vevent.vproposal.vsummary if vevent.vproposal else ''}"
        case _:
            return f"{type(vevent).__name__}"
