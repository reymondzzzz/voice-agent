from __future__ import annotations

import dataclasses
import itertools
import time
import uuid

from voice_agent.pipeline import voice_contracts


CONVERSATION_ID_PREFIX = "conv_"
SESSION_ID_PREFIX = "sess_"
TURN_ID_PREFIX = "turn_"
EVENT_ID_PREFIX = "evt_"
TASK_ID_PREFIX = "task_"

_EVENT_SEQUENCE = itertools.count()


def new_id(vprefix: str) -> str:
    return vprefix + uuid.uuid4().hex


@dataclasses.dataclass(frozen=True, slots=True)
class Correlation:
    """Identity carried by every event on both planes.

    vconversation_epoch is the ordering authority for stale-result rejection: a task authorized in
    an older epoch may still complete, but the delivery layer decides whether it is still wanted.
    It is deliberately separate from vturn_id, because a task legitimately outlives the turn that
    started it.
    """

    vconversation_id: str
    vsession_id: str
    vconversation_epoch: int
    vevent_id: str
    vtimestamp_s: float
    vturn_id: str | None = None
    vsequence: int = 0

    @classmethod
    def create(
        cls,
        *,
        vconversation_id: str,
        vsession_id: str,
        vconversation_epoch: int,
        vturn_id: str | None = None,
    ) -> Correlation:
        return cls(
            vconversation_id=vconversation_id,
            vsession_id=vsession_id,
            vconversation_epoch=vconversation_epoch,
            vevent_id=new_id(EVENT_ID_PREFIX),
            vtimestamp_s=time.time(),
            vturn_id=vturn_id,
            vsequence=next(_EVENT_SEQUENCE),
        )

    def derive(self, *, vturn_id: str | None = None, vconversation_epoch: int | None = None) -> Correlation:
        return dataclasses.replace(
            self,
            vevent_id=new_id(EVENT_ID_PREFIX),
            vtimestamp_s=time.time(),
            vsequence=next(_EVENT_SEQUENCE),
            vturn_id=self.vturn_id if vturn_id is None else vturn_id,
            vconversation_epoch=self.vconversation_epoch if vconversation_epoch is None else vconversation_epoch,
        )

    def log_fields(self) -> dict[str, str | int | None]:
        return {
            "conversation_id": self.vconversation_id,
            "session_id": self.vsession_id,
            "conversation_epoch": self.vconversation_epoch,
            "turn_id": self.vturn_id,
            "event_id": self.vevent_id,
        }


CORRELATION_LOG_FIELDS = ("conversation_id", "session_id", "conversation_epoch", "turn_id", "event_id", "task_id", "event_type")

assert set(voice_contracts.VOICE_CORRELATION_ID_FIELDS) & {"conversation_id", "turn_id"}, "flexus correlation contract changed"
