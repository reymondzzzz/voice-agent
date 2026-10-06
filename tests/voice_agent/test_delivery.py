from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.asyncio


async def test_assistant_interruption_clears_the_speaking_flag(harness):
    await harness.vspeech.emit_assistant_speech_started()
    await harness.pump()
    assert harness.vsession.vruntime.vassistant_speaking is True

    await harness.vspeech.emit_interrupted()
    await harness.pump()
    assert harness.vsession.vruntime.vassistant_speaking is False


async def test_session_shutdown_leaves_no_orphan_tasks(harness):
    harness.vgates["never finishes"] = asyncio.Event()
    await harness.vspeech.emit_tool_call("delegate_task", {"goal": "never finishes", "mode": "background"})
    await harness.pump()
    assert harness.vsession.vsupervisor.vrunning_count == 1

    vorphans = await harness.vsession.aclose(vtimeout_s=1.0)
    assert vorphans == (), f"tasks left running after shutdown: {vorphans}"
    assert harness.vsession.vsupervisor.vrunning_count == 0
