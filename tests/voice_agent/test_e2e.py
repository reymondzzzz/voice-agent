from __future__ import annotations

import asyncio

import pytest

from tests.voice_agent.fake_domain import DeleteProjectHandler, FakeProjectRepository
from voice_agent.agent.actions.models import ActionStatus
from voice_agent.agent.tasks.models import TaskStatus
from voice_agent.agent.tools.delegation import DelegationContext
from voice_agent.realtime.events import ContextScope

pytestmark = pytest.mark.asyncio


async def test_berlin_flight_end_to_end(harness):
    """The full shape from the brief, with the result landing only once the floor is free."""

    harness.vgates["Find the best flight to Berlin tomorrow"] = asyncio.Event()

    # 1. the caller speaks and the model delegates in the background
    await harness.vspeech.emit_user_speech_started()
    await harness.vspeech.emit_final_transcript("Find me a flight to Berlin tomorrow.")
    vcall_id = await harness.vspeech.emit_tool_call(
        "delegate_task",
        {"goal": "Find the best flight to Berlin tomorrow", "mode": "background", "intent": "travel"},
    )
    await harness.pump()

    # 2. the tool call completes immediately so the model can keep talking
    vtool_result = next(vr for vr in harness.vspeech.vsent_tool_results if vr.vtool_call_id == vcall_id)
    assert vtool_result.vresult["status"] == "started"
    vtask_id = vtool_result.vresult["task_id"]

    # 3. the model carries on speaking while the work runs
    await harness.vspeech.emit_assistant_speech_started()
    await harness.vspeech.emit_assistant_speech_stopped()
    await harness.pump()

    # 4. the caller starts talking again, and the work finishes mid-sentence
    await harness.vspeech.emit_user_speech_started()
    await harness.pump()
    harness.vgates["Find the best flight to Berlin tomorrow"].set()
    await harness.pump(12)

    assert harness.vsession.vregistry.require(vtask_id).vstatus is TaskStatus.COMPLETED
    vspoken_while_talking = [vu for vu in harness.vspeech.vsent_context if "Berlin" in vu.vtext]
    assert vspoken_while_talking == [], "the result must wait while the caller is speaking"
    assert len(harness.vsession.vbridge.vqueued) == 1

    # 5. the caller stops, and only now is the result surfaced
    await harness.vspeech.emit_user_speech_stopped()
    await harness.pump(12)

    vdelivered = [vu for vu in harness.vspeech.vsent_context if "Berlin" in vu.vtext]
    assert len(vdelivered) == 1
    assert vdelivered[0].vscope is ContextScope.BACKGROUND, "delivered as context, not as fake assistant speech"
    assert harness.vspeech.vresponse_requests, "the model is asked to surface it in its own words"
    assert harness.vsession.vbridge.vqueued == []
    assert len([vr for vr in harness.vspeech.vsent_tool_results if vr.vtool_call_id == vcall_id]) == 1


async def test_background_work_prepares_a_destructive_action_but_never_commits_it(harness):
    """A planning graph reaching its final node must not delete anything on its own."""

    vrepository = FakeProjectRepository()
    vhandler = DeleteProjectHandler(vrepository)
    harness.vregistry.register(vhandler)

    vcontext = DelegationContext(
        vconversation_id="conv_test", vsession_id="sess_test", vuser_id="user_1", vconversation_epoch=0, vturn_id="turn_1"
    )
    vproposal = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=vcontext, vfrom_background=True
    )

    assert vproposal["status"] == ActionStatus.AWAITING_CONFIRMATION.value
    assert vproposal["requires_confirmation"] is True
    assert vhandler.vexecutions == 0
    assert vrepository.vprojects["proj_prod"].vdeleted is False
    assert vrepository.vcommits == 0

    vconfirmed = await harness.vsession.vdelegation.confirm_action(vaction_id=vproposal["action_id"])
    assert vconfirmed["succeeded"] is True
    assert vrepository.vprojects["proj_prod"].vdeleted is True


async def test_reconnecting_the_speech_session_does_not_repeat_a_committed_action(harness):
    vrepository = FakeProjectRepository()
    vhandler = DeleteProjectHandler(vrepository)
    harness.vregistry.register(vhandler)
    vcontext = DelegationContext(
        vconversation_id="conv_test", vsession_id="sess_test", vuser_id="user_1", vconversation_epoch=0, vturn_id="turn_1"
    )

    vproposal = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=vcontext
    )
    await harness.vsession.vdelegation.confirm_action(vaction_id=vproposal["action_id"])

    # the session reconnects and the model repeats the same request from its own history
    vrepeat = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=vcontext
    )
    assert vrepeat["status"] == "already_executed"
    assert vhandler.vexecutions == 1
    assert vrepository.vcommits == 1
