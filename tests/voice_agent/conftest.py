from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable

import pytest_asyncio

from voice_agent.agent.actions.audit import InMemoryAuditSink
from voice_agent.agent.actions.commands import CommandRegistry
from voice_agent.agent.actions.policy import ActionPolicy
from voice_agent.agent.actions.service import ActionService
from voice_agent.agent.actions.store import InMemoryActionStore
from voice_agent.agent.tasks.models import TaskRecord, TaskResult
from voice_agent.realtime.fake import FakeRealtimeSpeechSession
from voice_agent.session import ConversationSession


@dataclasses.dataclass
class Harness:
    vsession: ConversationSession
    vspeech: FakeRealtimeSpeechSession
    vaudit: InMemoryAuditSink
    vregistry: CommandRegistry
    vstore: InMemoryActionStore
    vgates: dict[str, asyncio.Event]
    vrun_log: list[str]

    async def pump(self, vcycles: int = 6) -> None:
        """Let the bridge and mailbox loops make progress without wall-clock sleeps."""
        for _ in range(vcycles):
            await asyncio.sleep(0)

    def vbridge_queued(self, _unused=None):
        return list(self.vsession.vbridge.vqueued)

    async def drain_mailbox(self, vlimit: int = 32) -> None:
        for _ in range(vlimit):
            if self.vsession.vmailbox.vpending == 0:
                await self.pump(2)
                if self.vsession.vmailbox.vpending == 0:
                    return
            await self.pump(2)


def make_runner(vgates: dict[str, asyncio.Event], vrun_log: list[str]) -> Callable[[TaskRecord], Awaitable[TaskResult]]:
    async def run(vrecord: TaskRecord) -> TaskResult:
        vrun_log.append(vrecord.vtask_id)
        vgate = vgates.get(vrecord.vgoal)
        if vgate is not None:
            await vgate.wait()
        if vrecord.vgoal.startswith("fail:"):
            raise RuntimeError("task blew up")
        return TaskResult(vtask_id=vrecord.vtask_id, vpayload={"goal": vrecord.vgoal}, vsummary=f"done: {vrecord.vgoal}")

    return run


@pytest_asyncio.fixture
async def harness() -> Harness:
    vgates: dict[str, asyncio.Event] = {}
    vrun_log: list[str] = []
    vregistry = CommandRegistry()
    vstore = InMemoryActionStore()
    vaudit = InMemoryAuditSink()
    vactions = ActionService(vregistry=vregistry, vstore=vstore, vpolicy=ActionPolicy(), vaudit=vaudit)

    vruntime_epoch = {"value": 0}
    vspeech = FakeRealtimeSpeechSession(
        vconversation_id="conv_test",
        vsession_id="sess_test",
        vepoch_provider=lambda: vruntime_epoch["value"],
    )
    vsession = ConversationSession(
        vsession=vspeech,
        vconversation_id="conv_test",
        vsession_id="sess_test",
        vtask_runner=make_runner(vgates, vrun_log),
        vactions=vactions,
    )
    vspeech._vepoch_provider = lambda: vsession.vruntime.vconversation_epoch
    await vsession.start()
    vharness = Harness(
        vsession=vsession,
        vspeech=vspeech,
        vaudit=vaudit,
        vregistry=vregistry,
        vstore=vstore,
        vgates=vgates,
        vrun_log=vrun_log,
    )
    try:
        yield vharness
    finally:
        await vsession.aclose(vtimeout_s=1.0)
