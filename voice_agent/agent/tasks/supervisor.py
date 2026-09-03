from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable

from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus, new_task_record
from voice_agent.agent.tasks.registry import TaskRegistry

logger = logging.getLogger("voice_agent.tasks")

TaskRunner = Callable[[TaskRecord], Awaitable[TaskResult]]
TaskObserver = Callable[[TaskRecord], Awaitable[None]]


class TaskSupervisor:
    """Owns every background asyncio task for one conversation.

    Nothing else may spawn work: shutdown has to be able to account for all of it. Completion is
    reported through observers rather than returned, because a background task's result is a new
    asynchronous event and never the answer to the original tool call.
    """

    def __init__(
        self,
        *,
        vconversation_id: str,
        vsession_id: str,
        vregistry: TaskRegistry,
        vrunner: TaskRunner,
        von_started: TaskObserver | None = None,
        von_progress: TaskObserver | None = None,
        von_finished: TaskObserver | None = None,
    ) -> None:
        self.vconversation_id = vconversation_id
        self.vsession_id = vsession_id
        self.vregistry = vregistry
        self._vrunner = vrunner
        self._von_started = von_started
        self._von_progress = von_progress
        self._von_finished = von_finished
        self._vtasks: dict[str, asyncio.Task[None]] = {}
        self._vclosed = False

    @property
    def vrunning_count(self) -> int:
        return len([vtask for vtask in self._vtasks.values() if not vtask.done()])

    def create_record(
        self,
        *,
        vspec: TaskSpec,
        vconversation_epoch: int,
        vsource_turn_id: str | None = None,
        vsource_revision: int = 0,
        vvalid_while: Callable[[TaskRecord, int], bool] | None = None,
    ) -> TaskRecord:
        vrecord = new_task_record(
            vconversation_id=self.vconversation_id,
            vsession_id=self.vsession_id,
            vconversation_epoch=vconversation_epoch,
            vspec=vspec,
            vsource_turn_id=vsource_turn_id,
            vsource_revision=vsource_revision,
            vvalid_while=vvalid_while,
        )
        return self.vregistry.add(vrecord)

    def supersede_duplicates(self, vrecord: TaskRecord) -> tuple[TaskRecord, ...]:
        vreplaced = self.vregistry.find_supersedable(vrecord.vfingerprint, vrecord.vtask_id)
        for vold in vreplaced:
            self.cancel(vold.vtask_id, vsupersede_by=vrecord.vtask_id)
        return vreplaced

    async def run_quick(self, vrecord: TaskRecord) -> TaskRecord:
        await self._execute(vrecord)
        return vrecord

    def start_background(self, vrecord: TaskRecord) -> TaskRecord:
        if self._vclosed:
            raise RuntimeError("task supervisor is closed")
        vtask = asyncio.create_task(self._execute(vrecord), name=f"voice-task-{vrecord.vtask_id}")
        self._vtasks[vrecord.vtask_id] = vtask
        vtask.add_done_callback(lambda _: self._vtasks.pop(vrecord.vtask_id, None))
        return vrecord

    async def start(self, vrecord: TaskRecord) -> TaskRecord:
        if vrecord.vspec.vmode is TaskMode.QUICK:
            return await self.run_quick(vrecord)
        return self.start_background(vrecord)

    def cancel(self, vtask_id: str, *, vsupersede_by: str | None = None) -> TaskRecord | None:
        vrecord = self.vregistry.get(vtask_id)
        if vrecord is None or vrecord.vterminal:
            return vrecord
        vtask = self._vtasks.get(vtask_id)
        if vtask is not None and not vtask.done():
            vtask.cancel()
        if vsupersede_by is not None:
            self.vregistry.supersede(vtask_id, vsupersede_by)
        else:
            vrecord.transition_to(TaskStatus.CANCELLED)
        return vrecord

    async def report_progress(self, vrecord: TaskRecord, vprogress: str) -> None:
        vrecord.vprogress = vprogress
        vrecord.vupdated_at_s = time.time()
        if self._von_progress is not None:
            await self._von_progress(vrecord)

    async def _execute(self, vrecord: TaskRecord) -> None:
        if vrecord.vterminal:
            return
        vrecord.transition_to(TaskStatus.RUNNING)
        vrecord.vattempts += 1
        if self._von_started is not None:
            await self._von_started(vrecord)
        try:
            vresult = await self._vrunner(vrecord)
        except asyncio.CancelledError:
            if not vrecord.vterminal:
                vrecord.transition_to(TaskStatus.CANCELLED)
            if self._von_finished is not None:
                await self._von_finished(vrecord)
            raise
        except Exception as vexc:  # a task failure must reach the conversation, not the event loop
            if vrecord.vattempts < vrecord.vspec.vmax_attempts:
                logger.warning("task attempt failed, retrying", extra={**vrecord.log_fields(), "error": type(vexc).__name__})
                vrecord.vstatus = TaskStatus.PENDING
                await self._execute(vrecord)
                return
            vrecord.verror = f"{type(vexc).__name__}: {vexc}"
            if not vrecord.vterminal:
                vrecord.transition_to(TaskStatus.FAILED)
            logger.warning("task failed", extra={**vrecord.log_fields(), "error": vrecord.verror})
        else:
            vrecord.vresult = vresult
            if not vrecord.vterminal:
                vrecord.transition_to(TaskStatus.COMPLETED)
        if self._von_finished is not None:
            await self._von_finished(vrecord)

    async def aclose(self, *, vtimeout_s: float = 5.0) -> tuple[str, ...]:
        self._vclosed = True
        vpending = [vtask for vtask in self._vtasks.values() if not vtask.done()]
        for vtask in vpending:
            vtask.cancel()
        vorphans: tuple[str, ...] = ()
        if vpending:
            vdone, vnot_done = await asyncio.wait(vpending, timeout=vtimeout_s)
            for vtask in vdone:
                with contextlib.suppress(asyncio.CancelledError):
                    vtask.result()
            vorphans = tuple(vtask.get_name() for vtask in vnot_done)
        return vorphans
