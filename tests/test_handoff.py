from __future__ import annotations

import pytest

import handoff
from handoff import MAX_HANDOFF_SUMMARY_CHARS, VoiceHandoffRefused, authorize_handoff


def test_authorizes_known_target_and_freezes_its_voice():
    vauth = authorize_handoff("boss", "alice", "  the q3 sales report  ")
    assert vauth.vsource_agent_id == "boss"
    assert vauth.vtarget_agent_id == "alice"
    assert vauth.vtarget_name == "Alice"
    assert vauth.vhandoff_summary == "the q3 sales report"
    assert vauth.vprofile.vprofile_id == "voice_alice"


def test_target_id_is_normalized():
    assert authorize_handoff("boss", "  ALICE ", "the report").vtarget_agent_id == "alice"


def test_boss_can_hand_off_to_either_specialist():
    vto_alice = authorize_handoff("boss", "alice", "the weather in London")
    vto_bob = authorize_handoff("boss", "bob", "the time in Tokyo")
    assert (vto_alice.vtarget_name, vto_bob.vtarget_name) == ("Alice", "Bob")
    assert vto_alice.vprofile.vvoice != vto_bob.vprofile.vvoice


def test_every_pair_of_personas_switches_voice():
    for vsource in ("boss", "alice", "bob"):
        for vtarget in ("boss", "alice", "bob"):
            if vsource == vtarget:
                continue
            vauth = authorize_handoff(vsource, vtarget, "handing over")
            assert vauth.vprofile.vvoice != authorize_handoff(vtarget, vsource, "back").vprofile.vvoice


@pytest.mark.parametrize(
    ("vtarget", "vsummary", "vreason"),
    [
        ("", "the report", "target_agent_id is required"),
        ("   ", "the report", "target_agent_id is required"),
        ("nobody", "the report", "target agent 'nobody' not found"),
        ("boss", "the report", "target agent is already active on this call"),
        ("alice", "", "handoff summary is required"),
        ("alice", "   ", "handoff summary is required"),
        ("alice", "bad\nsummary", "handoff summary contains control characters"),
    ],
)
def test_refusals(vtarget, vsummary, vreason):
    with pytest.raises(VoiceHandoffRefused) as vexc:
        authorize_handoff("boss", vtarget, vsummary)
    assert vexc.value.vreason == vreason


def test_refuses_oversized_summary():
    with pytest.raises(VoiceHandoffRefused) as vexc:
        authorize_handoff("boss", "alice", "x" * (MAX_HANDOFF_SUMMARY_CHARS + 1))
    assert vexc.value.vreason == f"handoff summary exceeds {MAX_HANDOFF_SUMMARY_CHARS} characters"


def test_accepts_summary_at_the_limit():
    vauth = authorize_handoff("boss", "alice", "x" * MAX_HANDOFF_SUMMARY_CHARS)
    assert len(vauth.vhandoff_summary) == MAX_HANDOFF_SUMMARY_CHARS


def test_refuses_a_second_handoff_while_one_is_pending():
    with pytest.raises(VoiceHandoffRefused) as vexc:
        authorize_handoff("boss", "alice", "the report", vhandoff_pending=True)
    assert vexc.value.vreason == "a handoff is already pending on this call"


def test_pending_handoff_is_taken_once():
    vpending = handoff.PendingHandoff()
    assert not vpending.armed()
    assert vpending.take() is None
    vpending.arm(authorize_handoff("boss", "alice", "the report"))
    assert vpending.armed()
    assert vpending.take().vtarget_agent_id == "alice"
    assert not vpending.armed()
    assert vpending.take() is None
