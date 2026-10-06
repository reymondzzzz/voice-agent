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


async def test_committing_twice_returns_the_original_result(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(
        vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT
    )
    await harness.vsession.vdelegation.confirm_action(vaction_id=vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])

    assert voutcome.vfailure is FailureCode.ALREADY_EXECUTED
    assert voutcome.vsucceeded is True
    assert delete_handler.vexecutions == 1, "a repeated commit must not perform the effect twice"


class GatedDeleteHandler(DeleteProjectHandler):
    """Holds the effect open until released, so a second caller arrives while the first is executing."""

    def __init__(self, vrepository: FakeProjectRepository) -> None:
        super().__init__(vrepository)
        self.vgate = asyncio.Event()
        self.vstarted = asyncio.Event()

    async def execute(self, vcommand, vproposal):
        self.vstarted.set()
        await self.vgate.wait()
        return await super().execute(vcommand, vproposal)


class RaisingDeleteHandler(DeleteProjectHandler):
    async def execute(self, vcommand, vproposal):
        raise RuntimeError("provider timed out")


@pytest.fixture
def gated_handler(harness, repository) -> GatedDeleteHandler:
    vhandler = GatedDeleteHandler(repository)
    harness.vregistry.register(vhandler)
    return vhandler


async def confirmed_and_executing(harness, gated_handler) -> tuple[str, asyncio.Task]:
    vprepared = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    await harness.vsession.vdelegation.vactions.confirm(vprepared["action_id"])
    vfirst = asyncio.ensure_future(harness.vsession.vdelegation.vactions.commit(vprepared["action_id"]))
    await gated_handler.vstarted.wait()
    return vprepared["action_id"], vfirst


async def test_simultaneous_commits_execute_the_effect_once(harness, gated_handler):
    vaction_id, vfirst = await confirmed_and_executing(harness, gated_handler)
    vsecond = asyncio.ensure_future(harness.vsession.vdelegation.vactions.commit(vaction_id))
    await asyncio.sleep(0)
    gated_handler.vgate.set()
    vresults = await asyncio.gather(vfirst, vsecond)
    assert gated_handler.vexecutions == 1, "the second commit arrived mid-execution and must not run the effect again"
    assert all(voutcome.vsucceeded for _, voutcome in vresults)


async def test_asking_again_before_confirming_does_not_make_a_second_proposal(harness, delete_handler):
    vfirst = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    vsecond = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=dataclasses.replace(CONTEXT, vturn_id="turn_2"))
    assert vsecond["action_id"] == vfirst["action_id"]
    await harness.vsession.vdelegation.confirm_action(vaction_id=vfirst["action_id"])
    await harness.vsession.vdelegation.confirm_action(vaction_id=vsecond["action_id"])
    assert delete_handler.vexecutions == 1


async def test_asking_again_while_it_executes_joins_the_running_action(harness, gated_handler):
    vaction_id, vfirst = await confirmed_and_executing(harness, gated_handler)
    vrepeat = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    assert vrepeat["action_id"] == vaction_id and vrepeat["status"] == ActionStatus.EXECUTING.value
    gated_handler.vgate.set()
    await vfirst
    assert gated_handler.vexecutions == 1


async def test_cancelling_a_running_action_reports_it_running_and_lets_it_finish_once(harness, gated_handler):
    vaction_id, vfirst = await confirmed_and_executing(harness, gated_handler)
    vcancelled = await harness.vsession.vdelegation.cancel_action(vaction_id=vaction_id)
    assert vcancelled["status"] == ActionStatus.EXECUTING.value
    gated_handler.vgate.set()
    vrecord, _ = await vfirst
    assert vrecord.vstatus is ActionStatus.SUCCEEDED and gated_handler.vexecutions == 1


async def test_a_handler_that_raises_leaves_the_action_failed_not_executing(harness, repository):
    harness.vregistry.register(RaisingDeleteHandler(repository))
    vprepared = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "staging"}, vcontext=CONTEXT)
    await harness.vsession.vdelegation.vactions.confirm(vprepared["action_id"])
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])
    assert voutcome.vsucceeded is False and vrecord.vstatus is ActionStatus.FAILED and voutcome.vfailure is FailureCode.EXTERNAL_SERVICE_FAILED


async def test_an_expired_proposal_committed_directly_never_executes(harness, delete_handler):
    vprepared = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    vrecord = await harness.vsession.vdelegation.vactions.vstore.get(vprepared["action_id"])
    vrecord.vproposal = dataclasses.replace(vrecord.vproposal, vexpires_at_s=0.0)
    vrecord, voutcome = await harness.vsession.vdelegation.vactions.commit(vprepared["action_id"])
    assert voutcome.vsucceeded is False and voutcome.vfailure is FailureCode.CONFIRMATION_EXPIRED and vrecord.vstatus is ActionStatus.EXPIRED
    assert delete_handler.vexecutions == 0


async def test_after_a_proposal_expires_asking_again_makes_a_fresh_one(harness, delete_handler):
    vfirst = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    vrecord = await harness.vsession.vdelegation.vactions.vstore.get(vfirst["action_id"])
    vrecord.vproposal = dataclasses.replace(vrecord.vproposal, vexpires_at_s=0.0)
    vsecond = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    vthird = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    assert vsecond["action_id"] != vfirst["action_id"] and vthird["action_id"] == vsecond["action_id"], "the live second proposal is found, not shadowed by the expired first"


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


@pytest.mark.parametrize("vrefusal", ["unknown", "unconfirmed", "cancelled"])
async def test_a_refused_commit_reports_failure_and_never_executes(harness, delete_handler, vrefusal):
    vprepared = await harness.vsession.vdelegation.request_action(vaction_type="delete_project", varguments={"name": "production"}, vcontext=CONTEXT)
    vaction_id = "act_missing" if vrefusal == "unknown" else vprepared["action_id"]
    if vrefusal == "cancelled":
        await harness.vsession.vdelegation.cancel_action(vaction_id=vaction_id)
    _, voutcome = await harness.vsession.vdelegation.vactions.commit(vaction_id)
    assert voutcome.vsucceeded is False, "a refusal reported as success makes the model say it is done"
    assert delete_handler.vexecutions == 0
