from __future__ import annotations

import logging

from voice_agent.agent.tasks.models import TaskRecord, TaskStatus

logger = logging.getLogger("voice_agent.tasks")


class TaskRegistry:
    """Ownership of task records for one conversation.

    Superseding is fingerprint-based: a second request for the same goal replaces the first rather
    than racing it. Stale results are rejected here, at the single point that knows both the task's
    epoch and the conversation's, so no caller has to remember the rule.
    """

    def __init__(self) -> None:
        self._vrecords: dict[str, TaskRecord] = {}

    def add(self, vrecord: TaskRecord) -> TaskRecord:
        self._vrecords[vrecord.vtask_id] = vrecord
        return vrecord

    def get(self, vtask_id: str) -> TaskRecord | None:
        return self._vrecords.get(vtask_id)

    def require(self, vtask_id: str) -> TaskRecord:
        vrecord = self._vrecords.get(vtask_id)
        if vrecord is None:
            raise KeyError(f"unknown task {vtask_id}")
        return vrecord

    def all(self) -> tuple[TaskRecord, ...]:
        return tuple(self._vrecords.values())

    def active(self) -> tuple[TaskRecord, ...]:
        return tuple(vrecord for vrecord in self._vrecords.values() if not vrecord.vterminal)

    def completed(self) -> tuple[TaskRecord, ...]:
        return tuple(vrecord for vrecord in self._vrecords.values() if vrecord.vstatus is TaskStatus.COMPLETED)

    def by_fingerprint(self, vfingerprint: str) -> tuple[TaskRecord, ...]:
        return tuple(vrecord for vrecord in self._vrecords.values() if vrecord.vfingerprint == vfingerprint)

    def find_supersedable(self, vfingerprint: str, vexclude_task_id: str) -> tuple[TaskRecord, ...]:
        return tuple(
            vrecord
            for vrecord in self.by_fingerprint(vfingerprint)
            if vrecord.vtask_id != vexclude_task_id and not vrecord.vterminal
        )

    def supersede(self, vtask_id: str, vsuperseded_by: str) -> TaskRecord:
        vrecord = self.require(vtask_id)
        vrecord.transition_to(TaskStatus.SUPERSEDED)
        vrecord.vsuperseded_by = vsuperseded_by
        logger.info("task superseded", extra={"task_id": vtask_id, "superseded_by": vsuperseded_by})
        return vrecord

    def accepts_result(self, vtask_id: str, vcurrent_epoch: int) -> bool:
        vrecord = self.get(vtask_id)
        if vrecord is None:
            return False
        if vrecord.vstatus in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}:
            return False
        return vrecord.is_still_wanted(vcurrent_epoch)
