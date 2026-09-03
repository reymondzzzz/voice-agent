from __future__ import annotations

import dataclasses
import logging
import time
from typing import Protocol

from voice_agent.agent.actions.models import ActionRecord

logger = logging.getLogger("voice_agent.audit")


@dataclasses.dataclass(frozen=True, slots=True)
class AuditEntry:
    vaction_id: str
    vphase: str
    vactor: str
    vrecorded_at_s: float
    vfields: dict[str, object]


class AuditSink(Protocol):
    async def record(self, ventry: AuditEntry) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.ventries: list[AuditEntry] = []

    async def record(self, ventry: AuditEntry) -> None:
        self.ventries.append(ventry)
        logger.info("action audit", extra={"action_id": ventry.vaction_id, "phase": ventry.vphase, "actor": ventry.vactor})

    def for_action(self, vaction_id: str) -> tuple[AuditEntry, ...]:
        return tuple(ventry for ventry in self.ventries if ventry.vaction_id == vaction_id)


def audit_entry(vrecord: ActionRecord, vphase: str, vactor: str) -> AuditEntry:
    return AuditEntry(
        vaction_id=vrecord.vaction_id,
        vphase=vphase,
        vactor=vactor,
        vrecorded_at_s=time.time(),
        vfields={
            **vrecord.log_fields(),
            "resolved_target": vrecord.vproposal.vresolved_target,
            "requires_confirmation": vrecord.vproposal.vrequires_confirmation,
            "summary": vrecord.vproposal.vsummary,
        },
    )
