from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Protocol

from voice_agent.agent.events import SemanticEvent


class SessionMailbox(Protocol):
    """The one channel from the agent plane back to the realtime session.

    Typed and per-conversation on purpose: a global untyped queue makes ownership and shutdown
    ambiguous. The asyncio implementation can be swapped for Redis Streams or NATS without any
    caller changing, because nothing here exposes the transport.
    """

    async def publish(self, vevent: SemanticEvent) -> None: ...

    def subscribe(self) -> AsyncIterator[SemanticEvent]: ...

    async def aclose(self) -> None: ...


class AsyncioSessionMailbox:
    def __init__(self, *, vconversation_id: str, vmax_pending: int = 256) -> None:
        self.vconversation_id = vconversation_id
        self._vqueue: asyncio.Queue[SemanticEvent | None] = asyncio.Queue(maxsize=vmax_pending)
        self._vclosed = False
        self.vdropped = 0

    @property
    def vpending(self) -> int:
        return self._vqueue.qsize()

    async def publish(self, vevent: SemanticEvent) -> None:
        if self._vclosed:
            return
        try:
            self._vqueue.put_nowait(vevent)
        except asyncio.QueueFull:
            self.vdropped += 1

    async def subscribe(self) -> AsyncIterator[SemanticEvent]:
        while True:
            vevent = await self._vqueue.get()
            if vevent is None:
                return
            yield vevent

    async def aclose(self) -> None:
        if self._vclosed:
            return
        self._vclosed = True
        await self._vqueue.put(None)
