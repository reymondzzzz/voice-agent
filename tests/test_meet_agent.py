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
async def test_everything_waiting_is_told_in_one_turn_and_a_talked_over_turn_is_finished_whole() -> None:
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
        if len(vspoken) == 2:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    vworker.cancel()

    assert len(vspoken) == 2
    assert "honey keeps" in vspoken[0] and "it holds" in vspoken[0]
    assert "cut off" in vspoken[1] and "honey keeps" in vspoken[1] and "it holds" in vspoken[1]
    assert not vcall.vpending
