from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import time

import numpy
from livekit import rtc


def loudness_dbfs(vpcm: bytes) -> float:
    vsamples = numpy.frombuffer(vpcm, dtype="<i2").astype(numpy.float32) / 32768.0
    if not vsamples.size:
        return -120.0
    return float(20 * numpy.log10(numpy.sqrt(numpy.mean(vsamples * vsamples)) + 1e-6))


def heard_part(vtext: str, vheard_s: float, vspoken_s: float) -> str:
    # Speech runs at a roughly even pace, so the share of her audio that played is about the share of her words.
    if vspoken_s <= 0 or vheard_s <= 0:
        return ""
    vcut = vtext[: round(len(vtext) * min(1.0, vheard_s / vspoken_s))]
    return vcut if vcut == vtext or " " not in vcut else vcut.rsplit(" ", 1)[0]


class RoomAudioSink:
    """Qwen's voice, into the LiveKit room the Meet bridge plays back."""

    def __init__(self, vsource: rtc.AudioSource) -> None:
        self.vsource = vsource
        self.vmuted = False
        self.vfirst_audio_at: float | None = None
        self.vqueued_at_first_audio = 0.0
        # Seconds of her audio handed to the room so far; what is still queued at a cut was never heard.
        self.vtimeline_s = 0.0
        self._vstray = b""

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        if self.vmuted:
            return
        if self.vfirst_audio_at is None:
            self.vfirst_audio_at = time.monotonic()
            self.vqueued_at_first_audio = self.vsource.queued_duration
        # Deltas can end mid-sample; the odd byte belongs to the next delta.
        vpcm = self._vstray + vpcm
        vwhole = len(vpcm) - len(vpcm) % 2
        self._vstray = vpcm[vwhole:]
        if vwhole:
            self.vtimeline_s += vwhole / 2 / vsample_rate_hz
            await self.vsource.capture_frame(rtc.AudioFrame(vpcm[:vwhole], vsample_rate_hz, 1, vwhole // 2))

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        self._vstray = b""
        self.vsource.clear_queue()

    async def cut(self) -> float:
        # Where on her timeline the room stopped hearing her.
        vplayed_to = max(0.0, self.vtimeline_s - self.vsource.queued_duration)
        await self.clear()
        return vplayed_to


@dataclasses.dataclass(frozen=True)
class Cut:
    vreply: int
    vheard: str
    vheard_s: float
    vspoken_s: float


class PlayoutLedger:
    """Which of her replies the room actually heard.

    Each reply she speaks gets a number and ends up played out or cut. A result counts as told only when the reply
    that carried it played out: a shared "played" flag let an earlier "сейчас гляну" mark the answer after it as
    heard. A follow-up takes over the reply it continues, so results riding on a reply that only called a tool are
    heard when the follow-up saying them plays out.
    """

    def __init__(self) -> None:
        self.vlast = 0
        self.vopen: set[int] = set()
        self.vplayed: set[int] = set()
        self.vcut: set[int] = set()
        # Per reply: where its audio starts and ends on the sink's timeline, and what it said.
        self.vaudio: dict[int, tuple[float, float | None, str]] = {}
        self.vheard: dict[int, str] = {}
        self.vmoved: dict[int, int] = {}
        self.vchanged = asyncio.Event()

    @property
    def vnext(self) -> int:
        # The number the reply asked for next will get: what results handed to that request wait on.
        return self.vlast + 1

    def start(self, vtimeline_s: float) -> int:
        self.vlast += 1
        self.vaudio[self.vlast] = (vtimeline_s, None, "")
        if self.vlast not in self.vcut:
            self.vopen.add(self.vlast)
        return self.vlast

    def finish(self, vtimeline_s: float, vtext: str) -> None:
        if self.vlast in self.vaudio:
            self.vaudio[self.vlast] = (self.vaudio[self.vlast][0], vtimeline_s, vtext)
        if not vtext:
            # Nothing of it will ever play, so it can be neither heard nor the reply a cut interrupts.
            self.vopen.discard(self.vlast)

    def continue_in_next(self) -> None:
        self.vmoved[self.vlast] = self.vnext

    def played(self, vreply: int) -> None:
        if vreply in self.vopen:
            self.vopen.discard(vreply)
            self.vplayed.add(vreply)
            self.changed()

    def unheard(self, vreply: int) -> None:
        self.vopen.discard(vreply)
        self.vcut.add(vreply)
        self.changed()

    def spend(self) -> None:
        # A request that will never get a reply of its own: its number is used up, and nothing on it was heard.
        self.vlast += 1
        self.unheard(self.vlast)

    def cut(self, vplayed_to: float, vtimeline_s: float, vspeaking: str) -> Cut | None:
        # A reply whose audio had all played before the cut was heard in full even if the next one was queued behind
        # it: counting it as cut repeated a weather answer that had just been said. The first unfinished reply is the
        # one interrupted, cut at the words heard; any after it were not heard at all.
        vinterrupted = None
        for vreply in sorted(self.vopen):
            vstart, vend, vtext = self.vaudio.get(vreply, (vplayed_to, None, ""))
            if vend is not None and vend <= vplayed_to:
                self.vplayed.add(vreply)
                continue
            self.vcut.add(vreply)
            if vinterrupted is None:
                vspoken_s = (vend if vend is not None else vtimeline_s) - vstart
                self.vheard[vreply] = heard_part(vtext or vspeaking, vplayed_to - vstart, vspoken_s)
                vinterrupted = Cut(vreply, self.vheard[vreply], max(0.0, vplayed_to - vstart), vspoken_s)
        self.vopen.clear()
        self.changed()
        return vinterrupted

    def carrier_of(self, vreply: int) -> int:
        while vreply in self.vmoved:
            vreply = self.vmoved[vreply]
        return vreply

    async def outcome(self, vreply: int, vtimeout_s: float) -> tuple[bool, str]:
        """Whether the reply carrying something played out, once it is known, and the words heard if it was cut.
        A reply never settled within the timeout produced no audio: not heard."""

        vdeadline = time.monotonic() + vtimeout_s
        while (vcarrier := self.carrier_of(vreply)) not in self.vplayed | self.vcut and time.monotonic() < vdeadline:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.vchanged.wait(), vdeadline - time.monotonic())
        vcarrier = self.carrier_of(vreply)
        return vcarrier in self.vplayed and vcarrier not in self.vcut, self.vheard.get(vcarrier, "")

    def changed(self) -> None:
        # Every waiter holds the event it waited on; a fresh one is armed for the next change.
        self.vchanged.set()
        self.vchanged = asyncio.Event()
