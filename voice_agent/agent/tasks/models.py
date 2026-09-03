from __future__ import annotations

import dataclasses
import enum
import hashlib
import time
from collections.abc import Callable

from voice_agent.correlation import TASK_ID_PREFIX, new_id


class TaskStatus(enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


TERMINAL_STATUSES = frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED})

LEGAL_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.PENDING: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED, TaskStatus.FAILED}),
    TaskStatus.RUNNING: frozenset({TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}),
    TaskStatus.COMPLETED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
    TaskStatus.SUPERSEDED: frozenset(),
}


class TaskMode(enum.Enum):
    QUICK = "quick"
    BACKGROUND = "background"


class TaskTransitionError(RuntimeError):
    pass


def fingerprint_goal(vgoal: str, vintent: str) -> str:
    vgoal_normalized = " ".join(vgoal.casefold().split())
    vintent_normalized = " ".join(vintent.casefold().split())
    return hashlib.sha256(f"{vintent_normalized}|{vgoal_normalized}".encode()).hexdigest()[:16]


@dataclasses.dataclass(frozen=True, slots=True)
class TaskSpec:
    vgoal: str
    vmode: TaskMode
    vintent: str = ""
    vcontext: dict[str, object] = dataclasses.field(default_factory=dict)
    vmax_attempts: int = 1

    @property
    def vfingerprint(self) -> str:
        return fingerprint_goal(self.vgoal, self.vintent)


@dataclasses.dataclass(frozen=True, slots=True)
class TaskResult:
    vtask_id: str
    vpayload: dict[str, object]
    vsummary: str = ""


@dataclasses.dataclass(slots=True)
class TaskRecord:
    """A unit of delegated work whose lifetime is deliberately not bound to a turn.

    vsource_turn_id records where the request came from, but staleness is judged against
    vconversation_epoch and vvalid_while, never against the current turn. A user asking a follow-up
    question must not silently kill work they asked for.
    """

    vtask_id: str
    vconversation_id: str
    vsession_id: str
    vsource_turn_id: str | None
    vsource_revision: int
    vconversation_epoch: int
    vspec: TaskSpec
    vstatus: TaskStatus = TaskStatus.PENDING
    vcreated_at_s: float = dataclasses.field(default_factory=time.time)
    vupdated_at_s: float = dataclasses.field(default_factory=time.time)
    vattempts: int = 0
    vresult: TaskResult | None = None
    verror: str | None = None
    vsuperseded_by: str | None = None
    vprogress: str = ""
    vvalid_while: Callable[["TaskRecord", int], bool] | None = None

    @property
    def vgoal(self) -> str:
        return self.vspec.vgoal

    @property
    def vintent(self) -> str:
        return self.vspec.vintent

    @property
    def vfingerprint(self) -> str:
        return self.vspec.vfingerprint

    @property
    def vterminal(self) -> bool:
        return self.vstatus in TERMINAL_STATUSES

    def transition_to(self, vnext: TaskStatus) -> None:
        if vnext not in LEGAL_TRANSITIONS[self.vstatus]:
            raise TaskTransitionError(f"task {self.vtask_id} cannot move {self.vstatus.value} -> {vnext.value}")
        self.vstatus = vnext
        self.vupdated_at_s = time.time()

    def is_still_wanted(self, vcurrent_epoch: int) -> bool:
        if self.vstatus in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}:
            return False
        if self.vvalid_while is not None:
            return bool(self.vvalid_while(self, vcurrent_epoch))
        return self.vconversation_epoch >= vcurrent_epoch

    def log_fields(self) -> dict[str, object]:
        return {
            "task_id": self.vtask_id,
            "conversation_id": self.vconversation_id,
            "session_id": self.vsession_id,
            "turn_id": self.vsource_turn_id,
            "conversation_epoch": self.vconversation_epoch,
            "status": self.vstatus.value,
        }


def new_task_record(
    *,
    vconversation_id: str,
    vsession_id: str,
    vconversation_epoch: int,
    vspec: TaskSpec,
    vsource_turn_id: str | None = None,
    vsource_revision: int = 0,
    vvalid_while: Callable[[TaskRecord, int], bool] | None = None,
) -> TaskRecord:
    return TaskRecord(
        vtask_id=new_id(TASK_ID_PREFIX),
        vconversation_id=vconversation_id,
        vsession_id=vsession_id,
        vsource_turn_id=vsource_turn_id,
        vsource_revision=vsource_revision,
        vconversation_epoch=vconversation_epoch,
        vspec=vspec,
        vvalid_while=vvalid_while,
    )
