from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_result_is_queued_while_the_user_is_speaking(harness):
    harness.vgates["berlin"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "berlin", "mode": "background"})
    await harness.pump()

    await harness.vspeech.emit_user_speech_started()
    await harness.pump()
    assert harness.vsession.vruntime.vuser_speaking is True

    harness.vgates["berlin"].set()
    await harness.pump(10)

    assert any("berlin" in vevent.vgoal for vevent in harness.vbridge_queued(harness)), "result should be waiting, not spoken"
    assert not any("Background work finished" in vupdate.vtext for vupdate in harness.vspeech.vsent_context)


async def test_queued_result_is_delivered_once_the_user_stops(harness):
    harness.vgates["berlin"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "berlin", "mode": "background"})
    await harness.pump()
    await harness.vspeech.emit_user_speech_started()
    await harness.pump()
    harness.vgates["berlin"].set()
    await harness.pump(10)

    await harness.vspeech.emit_user_speech_stopped()
    await harness.pump(10)

    vspoken = [vupdate for vupdate in harness.vspeech.vsent_context if "berlin" in vupdate.vtext]
    assert vspoken, "the result must be injected once the floor is free"
    assert harness.vspeech.vresponse_requests, "the model must be asked to surface it"
    assert vspoken[0].vscope.value == "background", "a result is context, never fake assistant speech"


async def test_assistant_interruption_clears_the_speaking_flag(harness):
    await harness.vspeech.emit_assistant_speech_started()
    await harness.pump()
    assert harness.vsession.vruntime.vassistant_speaking is True

    await harness.vspeech.emit_interrupted()
    await harness.pump()
    assert harness.vsession.vruntime.vassistant_speaking is False


async def test_duplicate_realtime_events_are_ignored(harness):
    vcall_id = await harness.vspeech.emit_tool_call("delegate_task", {"goal": "dedupe me", "mode": "quick"})
    await harness.pump()
    vbefore = len(harness.vspeech.vsent_tool_results)

    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "dedupe me", "mode": "quick"}, vtool_call_id=vcall_id)
    await harness.pump()

    assert len(harness.vspeech.vsent_tool_results) == vbefore, "a repeated tool call id must not run the work twice"


async def test_session_shutdown_leaves_no_orphan_tasks(harness):
    harness.vgates["never finishes"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "never finishes", "mode": "background"})
    await harness.pump()
    assert harness.vsession.vsupervisor.vrunning_count == 1

    vorphans = await harness.vsession.aclose(vtimeout_s=1.0)
    assert vorphans == (), f"tasks left running after shutdown: {vorphans}"
    assert harness.vsession.vsupervisor.vrunning_count == 0
