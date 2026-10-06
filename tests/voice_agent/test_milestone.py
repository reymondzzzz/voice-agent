from __future__ import annotations

import asyncio

import pytest

from voice_agent.agent.delivery.policy import DeliveryDecision
from voice_agent.agent.conversation.director import ConversationDirector
from voice_agent.agent.conversation.routing import HeuristicRouter, RouteAction
from voice_agent.agent.conversation.speech_policy import ResponseMode
from voice_agent.agent.tasks.models import TaskStatus

pytestmark = pytest.mark.asyncio

GOAL_A = "research the Moshi architecture in detail"
GOAL_B = "explain what full duplex means"
GOAL_C = "research the PersonaPlex memory usage"


def director_for(harness) -> ConversationDirector:
    return ConversationDirector(
        vrouter=HeuristicRouter(),
        vsupervisor=harness.vsession.vsupervisor,
        vregistry=harness.vsession.vregistry,
        vruntime=harness.vsession.vruntime,
    )


async def test_the_milestone_interaction(harness):
    """User starts A, talks about B, starts C, A finishes quietly, then asks for A and gets it."""

    vdirector = director_for(harness)
    harness.vgates[GOAL_A] = asyncio.Event()
    harness.vgates[GOAL_C] = asyncio.Event()

    # T0/T1 — "Research topic A" starts a background task
    vdecision_a, vtask_a = await vdirector.on_final_transcript(GOAL_A)
    await harness.pump()
    assert vdecision_a.vaction is RouteAction.DELEGATE
    assert vtask_a is not None and vtask_a.vstatus is TaskStatus.RUNNING

    # T2 — while A runs the assistant is told not to fabricate its result
    assert vdirector.response_mode(vlast_text=GOAL_A) is ResponseMode.BACKGROUND_PENDING
    assert "do not fabricate" in vdirector.speech_policy().render().casefold()

    # T3 — the user changes subject and B is answered locally, with no new task
    vdecision_b, vtask_b = await vdirector.on_final_transcript(GOAL_B)
    assert vdecision_b.vaction is RouteAction.LOCAL
    assert vtask_b is None
    assert vtask_a.vstatus is TaskStatus.RUNNING, "changing topic must not kill A"

    # T4 — a second research request runs concurrently with A
    vdecision_c, vtask_c = await vdirector.on_final_transcript(GOAL_C)
    await harness.pump()
    assert vtask_c is not None and vtask_c.vtask_id != vtask_a.vtask_id
    assert harness.vsession.vsupervisor.vrunning_count == 2

    # T6/T7 — A finishes while the conversation is about C, so it is stored rather than spoken
    harness.vgates[GOAL_A].set()
    await harness.pump(10)
    assert vtask_a.vstatus is TaskStatus.COMPLETED
    vdecision = vdirector.delivery_decision(vdirector.completion_event(vtask_a))
    assert vdecision is DeliveryDecision.STORE_SILENTLY, "A must not interrupt a conversation about C"

    # T8/T9 — the user asks for A and it is available at once
    vrecalled = vdirector.recall_for("what did you find about Moshi?")
    assert vrecalled is not None and vrecalled.vtask_id == vtask_a.vtask_id
    assert vdirector.delivery_decision(vdirector.completion_event(vrecalled), vexplicitly_requested=True) is DeliveryDecision.SPEAK_NOW
    vdirector.mark_delivered(vrecalled)

    # C finishes later and stays queryable
    harness.vgates[GOAL_C].set()
    await harness.pump(10)
    assert vtask_c.vstatus is TaskStatus.COMPLETED
    assert vdirector.recall_for("what about PersonaPlex memory") is not None


async def test_speculation_starts_before_end_of_turn(harness):
    vdirector = director_for(harness)
    harness.vgates[GOAL_A] = asyncio.Event()

    assert await vdirector.on_partial_transcript("research the") is None, "too little to act on"
    vcandidate = await vdirector.on_partial_transcript(GOAL_A)
    assert vcandidate is not None and vcandidate.vstatus is TaskStatus.CANDIDATE
    assert harness.vsession.vsupervisor.vrunning_count == 0, "a candidate must not run until reconciled"

    vdecision, vfinal = await vdirector.on_final_transcript(GOAL_A)
    await harness.pump()
    assert vfinal is not None and vfinal.vtask_id == vcandidate.vtask_id, "same meaning promotes the candidate"
    assert vfinal.vstatus is TaskStatus.RUNNING


async def test_changed_wording_supersedes_the_speculative_task(harness):
    vdirector = director_for(harness)
    harness.vgates[GOAL_A] = asyncio.Event()
    harness.vgates["research the Mini-Omni architecture instead"] = asyncio.Event()

    vcandidate = await vdirector.on_partial_transcript(GOAL_A)
    assert vcandidate is not None

    vdecision, vfinal = await vdirector.on_final_transcript("research the Mini-Omni architecture instead")
    await harness.pump(4)
    assert vfinal is not None and vfinal.vtask_id != vcandidate.vtask_id
    assert vcandidate.vstatus in {TaskStatus.CANCELLED, TaskStatus.SUPERSEDED}, "a wrong guess must be discarded"


async def test_backpressure_rejects_runaway_task_creation(harness):
    vdirector = director_for(harness)
    harness.vsession.vsupervisor.vmax_active = 2
    for vindex in range(4):
        vgoal = f"research topic number {vindex} thoroughly"
        harness.vgates[vgoal] = asyncio.Event()
        await vdirector.on_final_transcript(vgoal)
        await harness.pump(2)

    vactive = [vr for vr in harness.vsession.vregistry.all() if not vr.vterminal]
    assert len(vactive) <= 4
    assert harness.vsession.vsupervisor.vrunning_count <= 4
