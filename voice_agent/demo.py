from __future__ import annotations

import asyncio

from voice_agent.agent.actions.commands import CommandRegistry
from voice_agent.agent.actions.service import ActionService
from voice_agent.agent.actions.store import InMemoryActionStore
from voice_agent.agent.delivery.policy import DeliveryDecision
from voice_agent.agent.conversation.director import ConversationDirector
from voice_agent.agent.tasks.results import Fact, SemanticResult
from voice_agent.agent.conversation.routing import HeuristicRouter
from voice_agent.agent.tasks.models import TaskRecord, TaskResult
from voice_agent.realtime.fake import FakeRealtimeSpeechSession
from voice_agent.session import ConversationSession

GOAL_A = "research the Moshi architecture in detail"
GOAL_B = "explain what full duplex means"
GOAL_C = "research the PersonaPlex memory usage"

GATES: dict[str, asyncio.Event] = {}


def say(vwho: str, vtext: str) -> None:
    print(f"  {vwho:<12} {vtext}")


async def reasoning_runner(vrecord: TaskRecord) -> TaskResult:
    vgate = GATES.get(vrecord.vgoal)
    if vgate is not None:
        await vgate.wait()
    vresult = SemanticResult(
        vtask_id=vrecord.vtask_id,
        vtopic=vrecord.vtopic,
        vsummary=f"Findings for: {vrecord.vgoal}",
        vimportant_points=("it is full duplex", "it runs on Apple Silicon"),
        vfacts=(Fact(vclaim="frame rate is 12.5 Hz", vconfidence=0.95),),
    )
    return TaskResult(vtask_id=vrecord.vtask_id, vpayload=vresult.as_payload(), vsummary=vresult.vsummary)


async def main() -> None:
    vspeech = FakeRealtimeSpeechSession(vconversation_id="conv_demo", vsession_id="sess_demo", vepoch_provider=lambda: 0)
    vsession = ConversationSession(
        vsession=vspeech,
        vconversation_id="conv_demo",
        vsession_id="sess_demo",
        vtask_runner=reasoning_runner,
        vactions=ActionService(vregistry=CommandRegistry(), vstore=InMemoryActionStore()),
    )
    await vsession.start()
    vdirector = ConversationDirector(
        vrouter=HeuristicRouter(),
        vsupervisor=vsession.vsupervisor,
        vregistry=vsession.vregistry,
        vruntime=vsession.vruntime,
    )
    GATES[GOAL_A] = asyncio.Event()
    GATES[GOAL_C] = asyncio.Event()

    print("\n── the conversation keeps going while work runs ──\n")

    say("user", f'"{GOAL_A}"')
    vdecision, vtask_a = await vdirector.on_final_transcript(GOAL_A)
    await asyncio.sleep(0)
    say("system", f"route={vdecision.vaction.value} → task {vtask_a.vtask_id[:12]} started, mode={vdirector.response_mode().value}")
    say("assistant", '"sure, looking into that now"   ← spoken immediately, no waiting')

    say("user", f'"{GOAL_B}"')
    vdecision_b, vtask_b = await vdirector.on_final_transcript(GOAL_B)
    say("system", f"route={vdecision_b.vaction.value} → answered locally, no task, A still {vtask_a.vstatus.value}")
    say("assistant", '"full duplex means it listens while it talks"')

    say("user", f'"{GOAL_C}"')
    vdecision_c, vtask_c = await vdirector.on_final_transcript(GOAL_C)
    await asyncio.sleep(0)
    say("system", f"task {vtask_c.vtask_id[:12]} started — now {vsession.vsupervisor.vrunning_count} running concurrently")

    GATES[GOAL_A].set()
    for _ in range(12):
        await asyncio.sleep(0)
    vdelivery = vdirector.delivery_decision(vdirector.completion_event(vtask_a))
    say("system", f"A completed while talking about C → delivery={vdelivery.value}")
    if vdelivery is DeliveryDecision.STORE_SILENTLY:
        say("assistant", "(says nothing — it does not hijack the current topic)")

    say("user", '"what did you find about Moshi?"')
    vrecalled = vdirector.recall_for("what did you find about Moshi?")
    vdecision_recall = vdirector.delivery_decision(vdirector.completion_event(vrecalled), vexplicitly_requested=True)
    say("system", f"recall → {vrecalled.vtask_id[:12]} delivery={vdecision_recall.value} mode={vdirector.response_mode().value}")
    say("assistant", f'"{vrecalled.vresult.vsummary}"')
    vdirector.mark_delivered(vrecalled)

    GATES[GOAL_C].set()
    for _ in range(12):
        await asyncio.sleep(0)
    say("system", f"C completed later and stays queryable: {vdirector.recall_for('personaplex memory') is not None}")

    vorphans = await vsession.aclose(vtimeout_s=1.0)
    print(f"\n  shutdown clean, orphan tasks: {vorphans or 'none'}\n")


if __name__ == "__main__":
    asyncio.run(main())
