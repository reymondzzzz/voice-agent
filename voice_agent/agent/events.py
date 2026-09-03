from __future__ import annotations

import dataclasses
import enum

from voice_agent.agent.actions.models import ActionOutcome, ActionProposal
from voice_agent.agent.tasks.models import TaskResult
from voice_agent.correlation import Correlation


class Criticality(enum.Enum):
    """How urgently a result deserves the floor.

    CORRECTION is the only level allowed to interrupt the assistant, because interrupting for a
    merely interesting result is worse than waiting.
    """

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CORRECTION = "correction"


@dataclasses.dataclass(frozen=True, slots=True)
class SemanticEventBase:
    vcorrelation: Correlation
    vtask_id: str | None = None
    vcriticality: Criticality = Criticality.NORMAL


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskStarted(SemanticEventBase):
    vgoal: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskProgress(SemanticEventBase):
    vprogress: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskCompleted(SemanticEventBase):
    vresult: TaskResult | None = None
    vgoal: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskFailed(SemanticEventBase):
    verror: str = ""
    vgoal: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskCancelled(SemanticEventBase):
    vgoal: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class BackgroundTaskSuperseded(SemanticEventBase):
    vsuperseded_by: str = ""
    vgoal: str = ""


@dataclasses.dataclass(frozen=True, slots=True)
class ContextUpdate(SemanticEventBase):
    vtext: str = ""
    vsource: str = "system"


@dataclasses.dataclass(frozen=True, slots=True)
class ConversationSuperseded(SemanticEventBase):
    vprevious_epoch: int = 0
    vreason: str = "intent_changed"


@dataclasses.dataclass(frozen=True, slots=True)
class ActionAwaitingConfirmation(SemanticEventBase):
    vproposal: ActionProposal | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class ActionCompleted(SemanticEventBase):
    vaction_id: str = ""
    voutcome: ActionOutcome | None = None


SemanticEvent = (
    BackgroundTaskStarted
    | BackgroundTaskProgress
    | BackgroundTaskCompleted
    | BackgroundTaskFailed
    | BackgroundTaskCancelled
    | BackgroundTaskSuperseded
    | ContextUpdate
    | ConversationSuperseded
    | ActionAwaitingConfirmation
    | ActionCompleted
)
