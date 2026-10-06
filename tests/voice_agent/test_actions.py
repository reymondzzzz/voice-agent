from __future__ import annotations

import asyncio
import dataclasses

import pytest

from tests.voice_agent.fake_domain import DeleteProjectHandler, FakeProjectRepository, MarkNotificationReadHandler
from voice_agent.agent.actions.models import ActionStatus, FailureCode
from voice_agent.agent.delegation import DelegationContext

pytestmark = pytest.mark.asyncio

CONTEXT = DelegationContext(vconversation_id="conv_test", vsession_id="sess_test", vuser_id="user_1", vconversation_epoch=0, vturn_id="turn_1")


@pytest.fixture
def repository() -> FakeProjectRepository:
    return FakeProjectRepository()


@pytest.fixture
def delete_handler(harness, repository) -> DeleteProjectHandler:
    vhandler = DeleteProjectHandler(repository)
    harness.vregistry.register(vhandler)
    return vhandler


@pytest.fixture
def read_handler(harness) -> MarkNotificationReadHandler:
    vhandler = MarkNotificationReadHandler()
    harness.vregistry.register(vhandler)
    return vhandler


async def test_irreversible_action_only_prepares_and_never_executes(harness, delete_handler, repository):
    vresult = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    assert vresult["requires_confirmation"] is True
    assert vresult["status"] == ActionStatus.AWAITING_CONFIRMATION.value
    assert delete_handler.vexecutions == 0
    assert repository.vprojects["proj_prod"].vdeleted is False


async def test_confirmation_summary_names_the_resolved_entity_and_impact(harness, delete_handler):
    vresult = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    assert "production" in vresult["summary"]
    assert "42" in vresult["summary"]
    assert vresult["impact"] == {"resources_deleted": 42}


async def test_confirming_executes_exactly_once(harness, delete_handler, repository):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    vconfirmed = await harness.vsession.vdelegation.confirm_action(vaction_id=vprepared["action_id"])
    assert vconfirmed["succeeded"] is True
    assert delete_handler.vexecutions == 1
    assert repository.vprojects["proj_prod"].vdeleted is True


async def test_committing_twice_returns_the_original_result(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.confirm_action(vaction_id=vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vfailure is FailureCode.ALREADY_EXECUTED
    assert voutcome.vsucceeded is True
    assert delete_handler.vexecutions == 1, "a repeated commit must not perform the effect twice"


async def test_simultaneous_commits_execute_the_effect_once(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.vactions.confirm(vprepared["action_id"])
    vresults = await asyncio.gather(
        harness.vsession.vdelegation.vactions.commit(vprepared["action_id"]),
        harness.vsession.vdelegation.vactions.commit(vprepared["action_id"]),
    )
    assert delete_handler.vexecutions == 1
    assert sum(1 for _, voutcome in vresults if voutcome.vsucceeded) == 2


async def test_repeating_the_same_logical_request_finds_the_executed_action(harness, delete_handler):
    vfirst = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.confirm_action(vaction_id=vfirst["action_id"])

    vsecond = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    assert vsecond["status"] == "already_executed"
    assert delete_handler.vexecutions == 1


async def test_duplicate_realtime_tool_call_does_not_prepare_twice(harness, delete_handler):
    vcall_id = await harness.vspeech.emit_tool_call("request_action", {"action_type": "delete_project", "arguments": {"name": "production"}})
    await harness.pump()
    await harness.vspeech.emit_tool_call(
        "request_action", {"action_type": "delete_project", "arguments": {"name": "production"}}, vtool_call_id=vcall_id
    )
    await harness.pump()

    vactions = await harness.vstore.list_for_conversation("conv_test")
    assert len(vactions) == 1
    assert delete_handler.vexecutions == 0


async def test_cancelled_action_cannot_be_committed(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.cancel_action(vaction_id=vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vsucceeded is False
    assert voutcome.vfailure is FailureCode.ACTION_CANCELLED
    assert delete_handler.vexecutions == 0


async def test_superseded_action_cannot_be_committed(harness, delete_handler):
    vfirst = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.vactions.supersede(vfirst["action_id"], "act_newer")
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vfirst["action_id"])

    assert voutcome.vfailure is FailureCode.ACTION_CANCELLED
    assert delete_handler.vexecutions == 0


async def test_committing_without_confirmation_is_refused(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vfailure is FailureCode.CONFIRMATION_REQUIRED
    assert voutcome.vmessage.startswith("Delete the production project")
    assert delete_handler.vexecutions == 0


async def test_expired_confirmation_is_refused(harness, repository):
    vhandler = DeleteProjectHandler(repository)
    vhandler.vmetadata = dataclasses.replace(vhandler.vmetadata, vconfirmation_ttl_s=0.0)
    harness.vregistry.register(vhandler)

    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    vresult = await harness.vsession.vdelegation.confirm_action(vaction_id=vprepared["action_id"])

    assert vresult["status"] == "refused"
    assert vresult["reason"] == FailureCode.CONFIRMATION_EXPIRED.value
    assert vhandler.vexecutions == 0


async def test_stale_version_conflicts_instead_of_overwriting(harness, delete_handler, repository):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    repository.vprojects["proj_prod"].vversion += 1  # somebody else edited it after planning

    await harness.vsession.vdelegation.vactions.confirm(vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vfailure is FailureCode.CONFLICT
    assert vrecord.vstatus is ActionStatus.CONFLICTED
    assert repository.vprojects["proj_prod"].vdeleted is False


async def test_a_failing_handler_rolls_the_transaction_back(harness, repository):
    vhandler = DeleteProjectHandler(repository, vfail_with=RuntimeError("provider exploded"))
    harness.vregistry.register(vhandler)
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "staging"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.vactions.confirm(vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vsucceeded is False
    assert vrecord.vstatus is ActionStatus.FAILED
    assert repository.vrollbacks == 1
    assert repository.vprojects["proj_stage"].vdeleted is False, "a half-applied write must not survive"
    assert repository.vaudit_rows == []


async def test_a_reversible_write_needs_no_confirmation(harness, read_handler):
    vresult = await harness.vsession.vdelegation.request_action(
        vaction_type="mark_notification_read", varguments={"notification_id": "notif_1"}, vcontext=CONTEXT
    )
    assert vresult["requires_confirmation"] is False
    assert vresult["status"] == ActionStatus.APPROVED.value

    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vresult["action_id"])
    assert voutcome.vsucceeded is True
    assert read_handler.vread == ["notif_1"]


async def test_the_action_is_audited_end_to_end(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.confirm_action(vaction_id=vprepared["action_id"])

    vphases = [ventry.vphase for ventry in harness.vaudit.for_action(vprepared["action_id"])]
    assert vphases == ["prepared", "confirmed", "executed"]
    ventry = harness.vaudit.for_action(vprepared["action_id"])[0]
    assert ventry.vfields["resolved_target"] == {"project_id": "proj_prod", "name": "production"}
    assert ventry.vfields["idempotency_key"]
