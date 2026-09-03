from __future__ import annotations

from typing import Protocol

from voice_agent.agent.actions.models import ActionRecord, ActionStatus


class ActionStore(Protocol):
    """Durable home for important action state.

    Deliberately separate from LangGraph state: an approved action must survive a process restart
    and a reconnected speech session, and must be answerable by idempotency key so a repeat of the
    same logical effect finds the original record.
    """

    async def put(self, vrecord: ActionRecord) -> ActionRecord: ...

    async def get(self, vaction_id: str) -> ActionRecord | None: ...

    async def find_by_idempotency_key(self, vkey: str) -> ActionRecord | None: ...

    async def list_for_conversation(self, vconversation_id: str) -> tuple[ActionRecord, ...]: ...


class InMemoryActionStore:
    def __init__(self) -> None:
        self._vrecords: dict[str, ActionRecord] = {}
        self._vby_key: dict[str, str] = {}

    async def put(self, vrecord: ActionRecord) -> ActionRecord:
        self._vrecords[vrecord.vaction_id] = vrecord
        vexisting = self._vby_key.get(vrecord.vidempotency_key)
        if vexisting is None or vexisting == vrecord.vaction_id:
            self._vby_key[vrecord.vidempotency_key] = vrecord.vaction_id
        elif vrecord.vstatus is ActionStatus.SUCCEEDED:
            self._vby_key[vrecord.vidempotency_key] = vrecord.vaction_id
        return vrecord

    async def get(self, vaction_id: str) -> ActionRecord | None:
        return self._vrecords.get(vaction_id)

    async def find_by_idempotency_key(self, vkey: str) -> ActionRecord | None:
        vaction_id = self._vby_key.get(vkey)
        vrecord = self._vrecords.get(vaction_id) if vaction_id else None
        if vrecord is not None and vrecord.vstatus is ActionStatus.SUCCEEDED:
            return vrecord
        vsucceeded = [
            vcandidate
            for vcandidate in self._vrecords.values()
            if vcandidate.vidempotency_key == vkey and vcandidate.vstatus is ActionStatus.SUCCEEDED
        ]
        return vsucceeded[0] if vsucceeded else vrecord

    async def list_for_conversation(self, vconversation_id: str) -> tuple[ActionRecord, ...]:
        return tuple(vrecord for vrecord in self._vrecords.values() if vrecord.vconversation_id == vconversation_id)
