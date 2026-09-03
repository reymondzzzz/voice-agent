import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

logger = logging.getLogger("voice")


class VoiceTtsPrefetcher:
    def __init__(
        self,
        vopen_segment: Callable[[str], Awaitable[Any]],
        vclose_stream: Callable[[Any], Awaitable[None]],
        vdepth: int,
    ) -> None:
        if vdepth < 1:
            raise ValueError("voice tts prefetch depth must be positive")
        self._vopen_segment = vopen_segment
        self._vclose_stream = vclose_stream
        self._vdepth = vdepth
        self._vpending: deque[tuple[int, asyncio.Future]] = deque()
        self._vnext_index = 0

    def prefetch(self, vsegments: Sequence[str], vallowed: bool) -> None:
        if not vallowed:
            return
        while len(self._vpending) < self._vdepth and self._vnext_index < len(vsegments):
            vindex = self._vnext_index
            self._vpending.append((vindex, asyncio.ensure_future(self._vopen_segment(vsegments[vindex]))))
            self._vnext_index += 1

    async def take(self, vsegment_index: int, vsegment: str) -> Any:
        if self._vpending and self._vpending[0][0] == vsegment_index:
            return await self._vpending.popleft()[1]
        self._vnext_index = max(self._vnext_index, vsegment_index + 1)
        return await self._vopen_segment(vsegment)

    async def discard(self) -> None:
        while self._vpending:
            await self._discard_one(self._vpending.popleft()[1])

    async def _discard_one(self, vopening: asyncio.Future) -> None:
        vopening.cancel()
        try:
            vstream = await vopening
        except asyncio.CancelledError:
            logger.debug("voice prefetched tts open cancelled before it produced audio")
            return
        except Exception as exc:  # quality: allow-broad-except: a prefetch that never became audio must not replace the caller's failure
            logger.debug("voice prefetched tts open failed %s", type(exc).__name__, exc_info=exc)
            return
        try:
            await self._vclose_stream(vstream)
        except Exception as exc:  # quality: allow-broad-except: closing an unused prefetch must not replace the caller's failure
            logger.warning("voice prefetched tts close failed %s", type(exc).__name__, exc_info=exc)
