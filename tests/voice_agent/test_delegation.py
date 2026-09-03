from __future__ import annotations

import asyncio

import pytest

from voice_agent.agent.tasks.models import TaskStatus

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
