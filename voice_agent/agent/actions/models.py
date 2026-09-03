from __future__ import annotations

import dataclasses
import enum
import hashlib
import json
import time

from voice_agent.correlation import new_id

ACTION_ID_PREFIX = "act_"
DEFAULT_CONFIRMATION_TTL_S = 120.0


class EffectLevel(enum.Enum):
    READ = "read"
    WRITE_REVERSIBLE = "write_reversible"
    WRITE_IMPORTANT = "write_important"
    IRREVERSIBLE = "irreversible"


class ActionStatus(enum.Enum):
    DRAFT = "draft"
    VALIDATED = "validated"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    APPROVED = "approved"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CONFLICTED = "conflicted"
    SUPERSEDED = "superseded"


ACTION_TERMINAL_STATUSES = frozenset(
    {
        ActionStatus.SUCCEEDED,
        ActionStatus.FAILED,
        ActionStatus.CANCELLED,
        ActionStatus.EXPIRED,
        ActionStatus.SUPERSEDED,
    }
)

ACTION_LEGAL_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.DRAFT: frozenset({ActionStatus.VALIDATED, ActionStatus.FAILED, ActionStatus.CANCELLED}),
    ActionStatus.VALIDATED: frozenset(
        {ActionStatus.AWAITING_CONFIRMATION, ActionStatus.APPROVED, ActionStatus.CANCELLED, ActionStatus.SUPERSEDED, ActionStatus.EXPIRED}
    ),
    ActionStatus.AWAITING_CONFIRMATION: frozenset(
        {ActionStatus.APPROVED, ActionStatus.CANCELLED, ActionStatus.EXPIRED, ActionStatus.SUPERSEDED}
    ),
    ActionStatus.APPROVED: frozenset({ActionStatus.EXECUTING, ActionStatus.CANCELLED, ActionStatus.EXPIRED, ActionStatus.SUPERSEDED}),
    ActionStatus.EXECUTING: frozenset({ActionStatus.SUCCEEDED, ActionStatus.FAILED, ActionStatus.CONFLICTED}),
    ActionStatus.CONFLICTED: frozenset({ActionStatus.SUPERSEDED, ActionStatus.CANCELLED, ActionStatus.FAILED}),
    ActionStatus.SUCCEEDED: frozenset(),
    ActionStatus.FAILED: frozenset(),
    ActionStatus.CANCELLED: frozenset(),
    ActionStatus.EXPIRED: frozenset(),
    ActionStatus.SUPERSEDED: frozenset(),
}


class FailureCode(enum.Enum):
    VALIDATION_FAILED = "validation_failed"
    PERMISSION_DENIED = "permission_denied"
    CONFIRMATION_REQUIRED = "confirmation_required"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    ACTION_CANCELLED = "action_cancelled"
    CONFLICT = "conflict"
    NOT_FOUND = "not_found"
    ALREADY_EXECUTED = "already_executed"
    EXTERNAL_SERVICE_FAILED = "external_service_failed"
    PARTIALLY_COMPLETED = "partially_completed"


class ExternalEffectStatus(enum.Enum):
    LOCAL_COMMITTED = "local_committed"
    EXTERNAL_PENDING = "external_pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATION_REQUIRED = "compensation_required"


class ActionTransitionError(RuntimeError):
    pass


def idempotency_key(vconversation_id: str, vaction_type: str, vresolved_target: dict[str, object], vparameters: dict[str, object]) -> str:
    """Logical identity of the effect, not of the request.

    Derived from the resolved target so that a reconnect, a model retry or a duplicate tool-call
    event all produce the same key and therefore the same single effect. A random uuid here would
    defeat the entire purpose.
    """

    vcanonical = json.dumps(
        {"conversation_id": vconversation_id, "action_type": vaction_type, "target": vresolved_target, "parameters": vparameters},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(vcanonical.encode("utf-8")).hexdigest()[:32]


@dataclasses.dataclass(frozen=True, slots=True)
class Command:
    """Base for typed domain commands.

    A command is what the domain layer executes. LangGraph chooses which command to request; it
    never writes storage itself, and the command is built during prepare so commit cannot be
    influenced by anything the model says afterwards.
    """


@dataclasses.dataclass(frozen=True, slots=True)
class ActionMetadata:
    vaction_type: str
    veffect_level: EffectLevel
    vrequires_confirmation: bool = False
    vidempotent: bool = True
    vsupports_dry_run: bool = False
    vsupports_rollback: bool = False
    vrequires_fresh_state: bool = False
    vconfirmation_ttl_s: float = DEFAULT_CONFIRMATION_TTL_S
    vaudit_required: bool = True


@dataclasses.dataclass(frozen=True, slots=True)
class ActionProposal:
    """The exact operation a confirmation approves.

    Commit reads this record rather than re-reading model arguments, so the model cannot alter the
    effect between the sentence the user agreed to and the write that follows.
    """

    vaction_id: str
    vaction_type: str
    vresolved_target: dict[str, object]
    vparameters: dict[str, object]
    vimpact: dict[str, object]
    vsummary: str
    vrequires_confirmation: bool
    veffect_level: EffectLevel
    vidempotency_key: str
    vexpires_at_s: float
    vprecondition: dict[str, object] = dataclasses.field(default_factory=dict)

    def is_expired(self, vnow_s: float | None = None) -> bool:
        return (vnow_s if vnow_s is not None else time.time()) >= self.vexpires_at_s


@dataclasses.dataclass(frozen=True, slots=True)
class ActionOutcome:
    vsucceeded: bool
    vresult: dict[str, object] = dataclasses.field(default_factory=dict)
    vfailure: FailureCode | None = None
    vmessage: str = ""
    vexternal_status: ExternalEffectStatus | None = None


@dataclasses.dataclass(slots=True)
class ActionRecord:
    vaction_id: str
    vconversation_id: str
    vsession_id: str
    vuser_id: str
    vaction_type: str
    vproposal: ActionProposal
    veffect_level: EffectLevel
    vstatus: ActionStatus
    vidempotency_key: str
    vsource_turn_id: str | None
    vconversation_epoch: int
    vcreated_at_s: float = dataclasses.field(default_factory=time.time)
    vupdated_at_s: float = dataclasses.field(default_factory=time.time)
    vapproved_at_s: float | None = None
    vexecuted_at_s: float | None = None
    voutcome: ActionOutcome | None = None
    vsuperseded_by: str | None = None
    vcommand: Command | None = None

    @property
    def vterminal(self) -> bool:
        return self.vstatus in ACTION_TERMINAL_STATUSES

    def transition_to(self, vnext: ActionStatus) -> None:
        if vnext not in ACTION_LEGAL_TRANSITIONS[self.vstatus]:
            raise ActionTransitionError(f"action {self.vaction_id} cannot move {self.vstatus.value} -> {vnext.value}")
        self.vstatus = vnext
        self.vupdated_at_s = time.time()

    def log_fields(self) -> dict[str, object]:
        return {
            "action_id": self.vaction_id,
            "conversation_id": self.vconversation_id,
            "session_id": self.vsession_id,
            "turn_id": self.vsource_turn_id,
            "conversation_epoch": self.vconversation_epoch,
            "action_type": self.vaction_type,
            "effect_level": self.veffect_level.value,
            "status": self.vstatus.value,
            "idempotency_key": self.vidempotency_key,
        }


def new_action_id() -> str:
    return new_id(ACTION_ID_PREFIX)
