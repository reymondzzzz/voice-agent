from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from voice_agent.agent.actions.service import ActionService
from voice_agent.agent.delivery.mailbox import AsyncioSessionMailbox
from voice_agent.agent.delivery.policy import DeliveryPolicy
from voice_agent.agent.events import (
    BackgroundTaskCancelled,
    BackgroundTaskCompleted,
    BackgroundTaskFailed,
    BackgroundTaskProgress,
    BackgroundTaskStarted,
    BackgroundTaskSuperseded,
    Criticality,
)
from voice_agent.agent.conversation.runtime import ConversationRuntime
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.agent.delegation import DelegationAPI
from voice_agent.correlation import Correlation
from voice_agent.realtime.bridge import RealtimeAgentBridge
from voice_agent.realtime.session import RealtimeSpeechSession

logger = logging.getLogger("voice_agent.session")

TaskRunner = Callable[[TaskRecord], Awaitable[TaskResult]]


class ConversationSession:
    """Owns one conversation: its runtime state, its tasks, its mailbox and its bridge.

    Everything asynchronous is owned here so shutdown can account for it. Task completion is
    published to the mailbox rather than returned, and staleness is checked at publish time so a
    result the conversation has moved past never reaches the speech model as news.
    """

    def __init__(
        self,
        *,
        vsession: RealtimeSpeechSession,
        vconversation_id: str,
        vsession_id: str,
        vtask_runner: TaskRunner,
        vactions: ActionService,
        vuser_id: str = "user",
        vpolicy: DeliveryPolicy | None = None,
    ) -> None:
        self.vruntime = ConversationRuntime(vconversation_id=vconversation_id, vsession_id=vsession_id)
        self.vmailbox = AsyncioSessionMailbox(vconversation_id=vconversation_id)
        self.vregistry = TaskRegistry()
        self.vsupervisor = TaskSupervisor(
            vconversation_id=vconversation_id,
            vsession_id=vsession_id,
            vregistry=self.vregistry,
            vrunner=vtask_runner,
            von_started=self._on_task_started,
            von_progress=self._on_task_progress,
            von_finished=self._on_task_finished,
        )
        self.vdelegation = DelegationAPI(vsupervisor=self.vsupervisor, vregistry=self.vregistry, vactions=vactions)
        self.vbridge = RealtimeAgentBridge(
            vsession=vsession,
            vmailbox=self.vmailbox,
            vdelegation=self.vdelegation,
            vruntime=self.vruntime,
            vuser_id=vuser_id,
            vpolicy=vpolicy,
        )
        self.vspeech = vsession
        self._vgroup_task: asyncio.Task[None] | None = None

    def correlation(self, *, vturn_id: str | None = None) -> Correlation:
        return Correlation.create(
            vconversation_id=self.vruntime.vconversation_id,
            vsession_id=self.vruntime.vsession_id,
            vconversation_epoch=self.vruntime.vconversation_epoch,
            vturn_id=vturn_id if vturn_id is not None else self.vruntime.vcurrent_turn_id,
        )

    def _task_correlation(self, vrecord: TaskRecord) -> Correlation:
        return Correlation.create(
            vconversation_id=vrecord.vconversation_id,
            vsession_id=vrecord.vsession_id,
            vconversation_epoch=vrecord.vconversation_epoch,
            vturn_id=vrecord.vsource_turn_id,
        )

    async def _on_task_started(self, vrecord: TaskRecord) -> None:
        if vrecord.vspec.vmode is TaskMode.QUICK:
            return
        await self.vmailbox.publish(
            BackgroundTaskStarted(vcorrelation=self._task_correlation(vrecord), vtask_id=vrecord.vtask_id, vgoal=vrecord.vgoal, vcriticality=Criticality.LOW)
        )

    async def _on_task_progress(self, vrecord: TaskRecord) -> None:
        await self.vmailbox.publish(
            BackgroundTaskProgress(vcorrelation=self._task_correlation(vrecord), vtask_id=vrecord.vtask_id, vprogress=vrecord.vprogress, vcriticality=Criticality.LOW)
        )

    async def _on_task_finished(self, vrecord: TaskRecord) -> None:
        if vrecord.vspec.vmode is TaskMode.QUICK:
            return
        vcorrelation = self._task_correlation(vrecord)
        if vrecord.vstatus is TaskStatus.SUPERSEDED:
            await self.vmailbox.publish(
                BackgroundTaskSuperseded(vcorrelation=vcorrelation, vtask_id=vrecord.vtask_id, vsuperseded_by=vrecord.vsuperseded_by or "", vgoal=vrecord.vgoal, vcriticality=Criticality.LOW)
            )
            return
        if vrecord.vstatus is TaskStatus.CANCELLED:
            await self.vmailbox.publish(
                BackgroundTaskCancelled(vcorrelation=vcorrelation, vtask_id=vrecord.vtask_id, vgoal=vrecord.vgoal, vcriticality=Criticality.LOW)
            )
            return
        if not self.vregistry.accepts_result(vrecord.vtask_id, self.vruntime.vconversation_epoch):
            logger.info("stale task result withheld", extra={**vrecord.log_fields(), "current_epoch": self.vruntime.vconversation_epoch})
            return
        if vrecord.vstatus is TaskStatus.FAILED:
            await self.vmailbox.publish(
                BackgroundTaskFailed(vcorrelation=vcorrelation, vtask_id=vrecord.vtask_id, verror=vrecord.verror or "", vgoal=vrecord.vgoal)
            )
            return
        await self.vmailbox.publish(
            BackgroundTaskCompleted(vcorrelation=vcorrelation, vtask_id=vrecord.vtask_id, vresult=vrecord.vresult, vgoal=vrecord.vgoal)
        )

    async def start(self) -> None:
        await self.vspeech.start()
        self._vgroup_task = asyncio.create_task(self.vbridge.run(), name="conversation-bridge")

    async def aclose(self, *, vtimeout_s: float = 5.0) -> tuple[str, ...]:
        vorphans = await self.vsupervisor.aclose(vtimeout_s=vtimeout_s)
        await self.vmailbox.aclose()
        await self.vspeech.close()
        if self._vgroup_task is not None:
            self._vgroup_task.cancel()
            try:
                await self._vgroup_task
            except (asyncio.CancelledError, BaseExceptionGroup):
                pass
        await self.vbridge.aclose()
        return vorphans
