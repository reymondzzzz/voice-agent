from __future__ import annotations

from typing import Protocol


class AudioSink(Protocol):
    """Where a speech session's output audio goes.

    Audio output is a sink, not an event: keeping it off the semantic bus is what guarantees
    LangGraph never sees a PCM frame, and lets the transport change without the agent plane
    noticing.
    """

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None: ...

    async def flush(self) -> None: ...

    async def clear(self) -> None: ...


class NullAudioSink:
    def __init__(self) -> None:
        self.vwritten_bytes = 0
        self.vflushes = 0
        self.vclears = 0

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        self.vwritten_bytes += len(vpcm)

    async def flush(self) -> None:
        self.vflushes += 1

    async def clear(self) -> None:
        self.vclears += 1
