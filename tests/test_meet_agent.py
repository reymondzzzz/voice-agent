import asyncio
import collections

import aiohttp
import pytest

from examples.meet_addressing import MeetAddressing
from examples.meet_agent import MeetCall, PendingResult


class ClosingSession:
    async def send_audio(self, _vchunk) -> None:
        raise aiohttp.ClientConnectionResetError("Cannot write to closing transport")


class OpenSession:
    def __init__(self) -> None:
        self.vframes = 0

    async def send_audio(self, _vchunk) -> None:
        self.vframes += 1


@pytest.mark.asyncio
async def test_a_closing_session_costs_a_frame_not_the_audio_pump() -> None:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vsession = ClosingSession()
    await vcall.forward_frame(b"\x00\x00" * 480)
    vnext = OpenSession()
    vcall.vsession = vnext
    await vcall.forward_frame(b"\x00\x00" * 480)
    assert vnext.vframes == 1


@pytest.mark.asyncio
async def test_results_are_told_one_at_a_time_and_a_talked_over_one_is_finished_first() -> None:
    vcall = MeetCall.__new__(MeetCall)
    vcall.vaddressing = MeetAddressing("Karen", lambda _vprompt: asyncio.sleep(0, ""))
    vcall.vpending = collections.deque([PendingResult("Carl", "a science fact", "honey keeps"), PendingResult("Anna", "the deadline", "it holds")])
    vcall.vpending_added = asyncio.Event()
    vcall.vplayed = asyncio.Event()
    vcall.vinterrupted = False
    vspoken: list[str] = []

    async def quiet(_vpause_s: float) -> None:
        pass

    async def respond(vline: str) -> None:
        vspoken.append(vline)
        vcall.vinterrupted = len(vspoken) == 1
        vcall.vplayed.set()

    vcall.wait_until_quiet = quiet
    vcall.respond = respond
    vcall.vpending_added.set()
    vworker = asyncio.create_task(vcall.deliver_pending())
    for _ in range(50):
        if len(vspoken) == 3:
            break
        await asyncio.sleep(0.01)
    vworker.cancel()

    assert [vline.split("]")[0] for vline in vspoken] == [
        "[background result for Carl, who asked: a science fact",
        "[you were cut off while telling Carl the background result about 'a science fact'",
        "[background result for Anna, who asked: the deadline",
    ]
