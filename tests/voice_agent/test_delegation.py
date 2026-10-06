from __future__ import annotations

import asyncio
import contextlib

import pytest

from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor

pytestmark = pytest.mark.asyncio


async def test_quick_delegation_returns_the_result_in_the_tool_call(harness):
    vcall_id = await harness.vspeech.emit_tool_call("delegate_task", {"goal": "what is the time", "mode": "quick"})
    await harness.pump()

    vresults = [vresult for vresult in harness.vspeech.vsent_tool_results if vresult.vtool_call_id == vcall_id]
    assert len(vresults) == 1
    vpayload = vresults[0].vresult
    assert vpayload["status"] == TaskStatus.COMPLETED.value
    assert vpayload["summary"] == "done: what is the time"


async def test_background_delegation_completes_the_tool_call_immediately(harness):
    harness.vgates["slow work"] = asyncio.Event()
    vcall_id = await harness.vspeech.emit_tool_call("delegate_task", {"goal": "slow work", "mode": "background"})
    await harness.pump()

    vresults = [vresult for vresult in harness.vspeech.vsent_tool_results if vresult.vtool_call_id == vcall_id]
    assert len(vresults) == 1, "the original tool call must complete at once"
    assert vresults[0].vresult["status"] == "started"
    assert vresults[0].vresult["task_id"].startswith("task_")
    assert harness.vsession.vsupervisor.vrunning_count == 1

    harness.vgates["slow work"].set()
    await harness.pump(10)
    vtask_id = vresults[0].vresult["task_id"]
    assert harness.vsession.vregistry.require(vtask_id).vstatus is TaskStatus.COMPLETED
    assert len([vresult for vresult in harness.vspeech.vsent_tool_results if vresult.vtool_call_id == vcall_id]) == 1, (
        "a later result must never be delivered as a second answer to the original tool call"
    )


async def test_multiple_concurrent_tasks_are_tracked_independently(harness):
    for vgoal in ("alpha", "beta", "gamma"):
        harness.vgates[vgoal] = asyncio.Event()
        await harness.vspeech.emit_tool_call("delegate_task", {"goal": vgoal, "mode": "background"})
    await harness.pump()

    assert harness.vsession.vsupervisor.vrunning_count == 3
    harness.vgates["beta"].set()
    await harness.pump(8)

    vstatuses = {vrecord.vgoal: vrecord.vstatus for vrecord in harness.vsession.vregistry.all()}
    assert vstatuses["beta"] is TaskStatus.COMPLETED
    assert vstatuses["alpha"] is TaskStatus.RUNNING
    assert vstatuses["gamma"] is TaskStatus.RUNNING


async def test_task_cancellation_stops_the_work(harness):
    harness.vgates["long"] = asyncio.Event()
    vcall_id = await harness.vspeech.emit_tool_call("delegate_task", {"goal": "long", "mode": "background"})
    await harness.pump()
    vtask_id = next(vr.vresult["task_id"] for vr in harness.vspeech.vsent_tool_results if vr.vtool_call_id == vcall_id)

    await harness.vspeech.emit_tool_call("cancel_task", {"task_id": vtask_id})
    await harness.pump(6)

    assert harness.vsession.vregistry.require(vtask_id).vstatus is TaskStatus.CANCELLED
    assert harness.vsession.vsupervisor.vrunning_count == 0, "the asyncio task is gone, not just relabelled"
    harness.vgates["long"].set()
    await harness.drain_mailbox()
    await harness.vsession.vbridge.flush_queued()
    assert not any("Background work finished" in vupdate.vtext for vupdate in harness.vspeech.vsent_context), "a cancelled task never reports a result"


async def test_a_second_request_for_the_same_goal_supersedes_the_first(harness):
    harness.vgates["flight to Berlin"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "flight to Berlin", "mode": "background", "intent": "travel"})
    await harness.pump()
    vfirst = harness.vsession.vregistry.all()[0]

    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "flight to Berlin", "mode": "background", "intent": "travel"})
    await harness.pump(6)

    assert vfirst.vstatus is TaskStatus.SUPERSEDED
    assert vfirst.vsuperseded_by is not None
    vsecond = harness.vsession.vregistry.require(vfirst.vsuperseded_by)
    assert vsecond.vstatus is TaskStatus.RUNNING
    harness.vgates["flight to Berlin"].set()
    await harness.drain_mailbox()
    await harness.vsession.vbridge.flush_queued()
    vfinished = [vupdate for vupdate in harness.vspeech.vsent_context if "Background work finished" in vupdate.vtext]
    assert len(vfinished) == 1, "the superseded request is never told as well"


async def test_a_task_survives_later_conversational_turns(harness):
    harness.vgates["research"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "research", "mode": "background"})
    await harness.pump()
    vrecord = harness.vsession.vregistry.all()[0]

    for _ in range(3):
        await harness.vspeech.emit_user_speech_started()
        await harness.vspeech.emit_final_transcript("something else entirely")
        await harness.vspeech.emit_user_speech_stopped()
        await harness.pump()

    assert vrecord.vstatus is TaskStatus.RUNNING, "a new turn must not kill work the user asked for"
    harness.vgates["research"].set()
    await harness.pump(8)
    assert vrecord.vstatus is TaskStatus.COMPLETED


async def test_a_stale_result_is_withheld_after_the_epoch_moves_on(harness):
    harness.vgates["stale work"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "stale work", "mode": "background"})
    await harness.pump()

    harness.vsession.vruntime.bump_epoch("intent_changed")
    harness.vgates["stale work"].set()
    await harness.pump(10)
    await harness.drain_mailbox()

    vspoken = [vupdate for vupdate in harness.vspeech.vsent_context if "stale work" in vupdate.vtext]
    assert vspoken == [], "a result from a superseded epoch must not reach the speech model"


async def test_a_failing_task_reports_failure_rather_than_raising(harness):
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "fail: broken", "mode": "background"})
    await harness.pump(10)
    vrecord = harness.vsession.vregistry.all()[0]
    assert vrecord.vstatus is TaskStatus.FAILED
    assert "RuntimeError" in (vrecord.verror or "")


async def test_background_tool_calls_beyond_capacity_are_refused_and_still_answered(harness):
    harness.vsession.vsupervisor.vmax_active = 2
    vcalls = []
    for vgoal in ("first report", "second report", "third report"):
        harness.vgates[vgoal] = asyncio.Event()
        vcalls.append(await harness.vspeech.emit_tool_call("delegate_task", {"goal": vgoal, "mode": "background"}))
        await harness.pump()
    vresults = {vr.vtool_call_id: vr.vresult for vr in harness.vspeech.vsent_tool_results}
    assert [vresults[vcall]["status"] for vcall in vcalls] == ["started", "started", "rejected"]
    assert harness.vsession.vsupervisor.vrunning_count == 2


async def test_a_repeat_at_capacity_still_replaces_the_running_one(harness):
    harness.vsession.vsupervisor.vmax_active = 1
    harness.vgates["weekly report"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "weekly report", "mode": "background"})
    await harness.pump()
    vrepeat = await harness.vspeech.emit_tool_call("delegate_task", {"goal": "weekly report", "mode": "background"})
    await harness.pump()
    vresult = next(vr.vresult for vr in harness.vspeech.vsent_tool_results if vr.vtool_call_id == vrepeat)
    assert vresult["status"] == "started" and len(vresult["superseded"]) == 1


async def test_a_task_cancelled_while_its_attempt_fails_is_not_retried():
    vrelease = asyncio.Event()
    vattempts = []

    async def runner(vrecord: TaskRecord) -> TaskResult:
        vattempts.append(vrecord.vattempts)
        try:
            await vrelease.wait()
        except asyncio.CancelledError:
            raise RuntimeError("cleanup failed while being cancelled") from None
        return TaskResult(vtask_id=vrecord.vtask_id, vpayload={}, vsummary="done")

    vsupervisor = TaskSupervisor(vconversation_id="c", vsession_id="s", vregistry=TaskRegistry(), vrunner=runner)
    vrecord = vsupervisor.create_record(vspec=TaskSpec(vgoal="retryable work", vmode=TaskMode.BACKGROUND, vmax_attempts=3), vconversation_epoch=0)
    vsupervisor.start_background(vrecord)
    for _ in range(3):
        await asyncio.sleep(0)
    vsupervisor.cancel(vrecord.vtask_id)
    for _ in range(6):
        await asyncio.sleep(0)
    vrelease.set()
    await vsupervisor.aclose(vtimeout_s=0.1)
    assert vrecord.vstatus is TaskStatus.CANCELLED and vattempts == [1], "a cancelled task is not run again"


async def test_shutdown_names_a_task_that_ignores_cancellation():
    vstop = asyncio.Event()

    async def stubborn(vrecord: TaskRecord) -> TaskResult:
        while not vstop.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await vstop.wait()
        return TaskResult(vtask_id=vrecord.vtask_id, vpayload={}, vsummary="done")

    vsupervisor = TaskSupervisor(vconversation_id="c", vsession_id="s", vregistry=TaskRegistry(), vrunner=stubborn)
    vrecord = vsupervisor.create_record(vspec=TaskSpec(vgoal="never stops", vmode=TaskMode.BACKGROUND), vconversation_epoch=0)
    vsupervisor.start_background(vrecord)
    await asyncio.sleep(0)
    vorphans = await vsupervisor.aclose(vtimeout_s=0.05)
    vstop.set()
    assert vorphans == (f"voice-task-{vrecord.vtask_id}",), "an orphan is reported, not silently left running"
