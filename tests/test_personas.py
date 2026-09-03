from __future__ import annotations

import pytest

from personas import (
    PERSONAS,
    VOICE_GLOBAL_PROFILE_ID,
    VOICE_PROFILES,
    PersonaUnknown,
    resolve_persona,
    resolve_voice_profile,
)


def test_every_persona_points_at_a_registered_profile():
    for vpersona in PERSONAS.values():
        assert vpersona.vprofile_id in VOICE_PROFILES


def test_every_persona_has_a_distinct_voice():
    vvoices = [resolve_voice_profile(vpersona.vprofile_id).vvoice for vpersona in PERSONAS.values()]
    assert len(set(vvoices)) == len(vvoices)


def test_persona_keys_match_their_agent_ids():
    for vagent_id, vpersona in PERSONAS.items():
        assert vpersona.vagent_id == vagent_id


def test_unknown_profile_falls_back_to_the_global_default():
    assert resolve_voice_profile("voice_missing").vprofile_id == VOICE_GLOBAL_PROFILE_ID


def test_unknown_persona_raises():
    with pytest.raises(PersonaUnknown):
        resolve_persona("nobody")
