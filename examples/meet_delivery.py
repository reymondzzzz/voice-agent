from __future__ import annotations

import asyncio
import collections
import dataclasses
import logging

DELIVERY_ATTEMPTS = 2

logger = logging.getLogger("meet-agent")


@dataclasses.dataclass
class PendingResult:
    vrequester: str
    vgoal: str
    vanswer: str
    vattempts: int = 0
    # What she got out of the reply that carried this before she was cut off.
    vheard: str = ""
    # A quick tool's name and call number: a later call of it by the same person makes this value stale.
    vtool: str = ""
    vcall: int = 0
    # Cut off DELIVERY_ATTEMPTS times: it is offered once instead of told, then let go.
    voffered: bool = False


class ResultQueue:
    """Results waiting to be told, and the rules for what is still worth telling."""

    def __init__(self) -> None:
        self.vwaiting: collections.deque[PendingResult] = collections.deque()
        self.vadded = asyncio.Event()
        self.vcalls = 0
        self.vlatest_call: dict[tuple[str, str], int] = {}

    def record_call(self, vrequester: str, vtool: str) -> int:
        self.vcalls += 1
        self.vlatest_call[(vrequester, vtool)] = self.vcalls
        return self.vcalls

    def is_stale(self, vresult: PendingResult) -> bool:
        # A time or schedule value the same person has looked up again since: the newer one is what they want.
        return bool(vresult.vtool) and self.vlatest_call.get((vresult.vrequester, vresult.vtool), 0) > vresult.vcall

    def drop_stale(self) -> None:
        for vresult in [vresult for vresult in self.vwaiting if self.is_stale(vresult)]:
            logger.info("dropping %s for %s: looked up again since", vresult.vgoal, vresult.vrequester)
            self.vwaiting.remove(vresult)

    def add(self, vresults: list[PendingResult]) -> None:
        self.vwaiting.extend(vresults)
        self.vadded.set()

    def take_all(self) -> list[PendingResult]:
        self.drop_stale()
        vresults = list(self.vwaiting)
        self.vwaiting.clear()
        return vresults

    def take_fresh(self) -> list[PendingResult]:
        # A result already cut off once stays for its own "so, about ..." retelling: folded into the next answer,
        # which also started a search, the model skipped the talked-over weather and it counted as told.
        self.drop_stale()
        vfresh = [vresult for vresult in self.vwaiting if not vresult.vattempts]
        self.vwaiting = collections.deque(vresult for vresult in self.vwaiting if vresult.vattempts)
        return vfresh

    def retry(self, vresults: list[PendingResult], vheard: str) -> None:
        # Not heard: back to the front. Talked over DELIVERY_ATTEMPTS times it is offered once instead of told, since
        # telling it a third time is pushing and dropping it silently loses it; an offer talked over is let go.
        vretry = []
        for vresult in vresults:
            if vresult.voffered or self.is_stale(vresult):
                logger.info("letting go of %s for %s", vresult.vgoal, vresult.vrequester)
                continue
            vresult.voffered = vresult.vattempts >= DELIVERY_ATTEMPTS
            vresult.vheard = vheard
            vretry.append(vresult)
        if vretry:
            logger.info("%d result(s) not heard; will come back to them", len(vretry))
            self.vwaiting.extendleft(reversed(vretry))
            self.vadded.set()
