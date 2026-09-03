from __future__ import annotations

import pytest

from examples import small_agents
from voice_agent.pipeline import voice_contracts, voice_return_intent


def test_boss_can_hand_off_to_either_specialist():
    valice = small_agents.authorize_handoff("boss", "alice", "the weather in London")
    vbob = small_agents.authorize_handoff("boss", "bob", "the time in Tokyo")
    assert (valice.vtarget_name, vbob.vtarget_name) == ("Alice", "Bob")


def test_specialists_can_hand_back_to_boss():
    assert small_agents.authorize_handoff("alice", "boss", "done with the weather").vtarget_name == "Boss"
    assert small_agents.authorize_handoff("bob", "boss", "done with the time").vtarget_name == "Boss"


def test_target_id_is_normalized():
    assert small_agents.authorize_handoff("boss", "  ALICE ", "the weather").vtarget_agent_id == "alice"


@pytest.mark.parametrize(
    ("vsource", "vtarget", "vsummary", "vreason"),
    [
        ("boss", "nobody", "x", "target agent 'nobody' not found"),
        ("boss", "", "x", "target agent '' not found"),
        ("boss", "boss", "x", "target agent is already active on this call"),
        ("boss", "alice", "", "handoff summary is required"),
        ("boss", "alice", "bad\nsummary", "handoff summary contains control characters"),
    ],
)
def test_handoff_refusals(vsource, vtarget, vsummary, vreason):
    with pytest.raises(small_agents.HandoffRefused) as vexc:
        small_agents.authorize_handoff(vsource, vtarget, vsummary)
    assert vexc.value.vreason == vreason


def test_refuses_oversized_summary():
    with pytest.raises(small_agents.HandoffRefused, match="exceeds"):
        small_agents.authorize_handoff("boss", "alice", "x" * (small_agents.MAX_HANDOFF_SUMMARY_CHARS + 1))


def test_refuses_a_second_handoff_while_one_is_pending():
    with pytest.raises(small_agents.HandoffRefused, match="already pending"):
        small_agents.authorize_handoff("boss", "alice", "the weather", vhandoff_pending=True)


def test_pending_handoff_is_taken_once():
    vpending = small_agents.PendingHandoff()
    assert not vpending.armed()
    vpending.arm(small_agents.authorize_handoff("boss", "alice", "the weather"))
    assert vpending.armed()
    assert vpending.take().vtarget_agent_id == "alice"
    assert vpending.take() is None


def test_call_agent_tool_arms_the_pending_handoff():
    vpending = small_agents.PendingHandoff()
    vtool = small_agents.build_call_agent_tool(small_agents.EXAMPLE_AGENTS["boss"], vpending)
    vreply = vtool.invoke({"target_agent_id": "alice", "handoff_summary": "the weather in London"})
    assert "Alice" in vreply
    assert vpending.armed()


def test_call_agent_tool_refusal_leaves_nothing_pending():
    vpending = small_agents.PendingHandoff()
    vtool = small_agents.build_call_agent_tool(small_agents.EXAMPLE_AGENTS["boss"], vpending)
    assert vtool.invoke({"target_agent_id": "nobody", "handoff_summary": "x"}).startswith("Error: ")
    assert not vpending.armed()


def test_each_agent_gets_only_its_own_tool():
    assert [vt.name for vt in small_agents.AGENT_TOOLS["boss"]] == []
    assert [vt.name for vt in small_agents.AGENT_TOOLS["alice"]] == ["get_current_weather"]
    assert [vt.name for vt in small_agents.AGENT_TOOLS["bob"]] == ["get_current_time"]


def test_weather_is_placeholder_and_survives_unknown_cities():
    assert "(placeholder data)" in small_agents.get_current_weather.invoke({"city": "London"})
    assert "degrees Celsius" in small_agents.get_current_weather.invoke({"city": "Narnia"})


def test_time_reports_the_requested_timezone_and_refuses_a_fake_one():
    assert small_agents.get_current_time.invoke({"timezone": "Europe/London"}).endswith("in Europe/London")
    assert small_agents.get_current_time.invoke({"timezone": ""}).endswith("in UTC")
    assert small_agents.get_current_time.invoke({"timezone": "Mars/Olympus"}) == "Error: unknown timezone 'Mars/Olympus'"


def test_every_example_profile_uses_the_pinned_tts_model():
    for vprofile in small_agents.EXAMPLE_VOICE_PROFILES.values():
        assert vprofile.vtts_model == voice_contracts.VOICE_DEFAULT_TTS_MODEL
        assert vprofile.vtts_provider == "openrouter"


def test_each_agent_is_distinguishable_by_speed_because_the_provider_offers_one_voice():
    vspeeds = [small_agents.resolve_example_profile(va.vprofile_id).vtts_speed for va in small_agents.EXAMPLE_AGENTS.values()]
    assert len(set(vspeeds)) == len(vspeeds)


def test_unknown_profile_falls_back_instead_of_raising():
    assert small_agents.resolve_example_profile("voice_missing").vprofile_id == "voice_boss"


@pytest.mark.parametrize("vphrase", ["Boss, come back", "back to boss", "вернись к боссу", "vuelve a boss"])
def test_the_moved_return_intent_recognizes_a_handback(vphrase):
    assert voice_return_intent.classify_voice_return_command(vphrase) is voice_return_intent.VoiceReturnIntent.RETURN_TO_ORIGINATOR


def test_ordinary_speech_is_not_a_handback():
    assert voice_return_intent.classify_voice_return_command("tell boss later") is voice_return_intent.VoiceReturnIntent.AMBIGUOUS
