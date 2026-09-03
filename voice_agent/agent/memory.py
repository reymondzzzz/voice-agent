from __future__ import annotations

import dataclasses
import time
from typing import Protocol


@dataclasses.dataclass(frozen=True, slots=True)
class MemoryItem:
    vkey: str
    vtext: str
    vconversation_id: str
    vrecorded_at_s: float = dataclasses.field(default_factory=time.time)
    vmetadata: dict[str, object] = dataclasses.field(default_factory=dict)


class LongTermMemory(Protocol):
    """Durable knowledge across conversations, addressed by key rather than searched.

    Vector retrieval is deliberately not built here: the repository has no embedding stack, and a
    key-addressed store is enough to define the boundary a real one would slot into.
    """

    async def put(self, vitem: MemoryItem) -> MemoryItem: ...

    async def get(self, vkey: str) -> MemoryItem | None: ...

    async def list_for_conversation(self, vconversation_id: str) -> tuple[MemoryItem, ...]: ...


class InMemoryLongTermMemory:
    def __init__(self) -> None:
        self._vitems: dict[str, MemoryItem] = {}

    async def put(self, vitem: MemoryItem) -> MemoryItem:
        self._vitems[vitem.vkey] = vitem
        return vitem

    async def get(self, vkey: str) -> MemoryItem | None:
        return self._vitems.get(vkey)

    async def list_for_conversation(self, vconversation_id: str) -> tuple[MemoryItem, ...]:
        return tuple(vitem for vitem in self._vitems.values() if vitem.vconversation_id == vconversation_id)
