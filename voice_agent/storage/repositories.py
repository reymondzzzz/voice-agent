from __future__ import annotations

import dataclasses
import time
from typing import Protocol

from voice_agent.agent.tasks.models import TaskRecord, TaskStatus


@dataclasses.dataclass(frozen=True, slots=True)
class ConversationRow:
    vconversation_id: str
    vcreated_at_s: float
    vcurrent_topic: str = ""
    vconversation_epoch: int = 0


class ConversationRepository(Protocol):
    async def upsert(self, vrow: ConversationRow) -> ConversationRow: ...

    async def get(self, vconversation_id: str) -> ConversationRow | None: ...


class TaskRepository(Protocol):
    """Durable task state, separate from the asyncio task that happens to be running it.

    A task must be answerable after the process that started it has gone: the supervisor owns the
    coroutine, this owns the fact that the work exists.
    """

    async def put(self, vrecord: TaskRecord) -> TaskRecord: ...

    async def get(self, vtask_id: str) -> TaskRecord | None: ...

    async def list_for_conversation(self, vconversation_id: str) -> tuple[TaskRecord, ...]: ...

    async def resumable(self) -> tuple[TaskRecord, ...]: ...


class EventLog(Protocol):
    async def append(self, vconversation_id: str, vevent_type: str, vpayload: dict[str, object]) -> None: ...


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._vrows: dict[str, ConversationRow] = {}

    async def upsert(self, vrow: ConversationRow) -> ConversationRow:
        self._vrows[vrow.vconversation_id] = vrow
        return vrow

    async def get(self, vconversation_id: str) -> ConversationRow | None:
        return self._vrows.get(vconversation_id)


class InMemoryTaskRepository:
    def __init__(self) -> None:
        self._vrecords: dict[str, TaskRecord] = {}

    async def put(self, vrecord: TaskRecord) -> TaskRecord:
        self._vrecords[vrecord.vtask_id] = vrecord
        return vrecord

    async def get(self, vtask_id: str) -> TaskRecord | None:
        return self._vrecords.get(vtask_id)

    async def list_for_conversation(self, vconversation_id: str) -> tuple[TaskRecord, ...]:
        return tuple(vr for vr in self._vrecords.values() if vr.vconversation_id == vconversation_id)

    async def resumable(self) -> tuple[TaskRecord, ...]:
        return tuple(vr for vr in self._vrecords.values() if vr.vstatus in {TaskStatus.PENDING, TaskStatus.RUNNING})


class InMemoryEventLog:
    def __init__(self) -> None:
        self.ventries: list[tuple[float, str, str, dict[str, object]]] = []

    async def append(self, vconversation_id: str, vevent_type: str, vpayload: dict[str, object]) -> None:
        self.ventries.append((time.time(), vconversation_id, vevent_type, vpayload))
