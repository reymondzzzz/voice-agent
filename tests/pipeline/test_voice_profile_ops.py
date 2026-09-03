import pytest

from voice_agent.pipeline import voice_contracts
from voice_agent.pipeline import voice_profile_ops


class _Connection:
    def __init__(self, vrow) -> None:
        self.vrow = vrow

    async def fetchrow(self, vsql, *vargs):
        assert "FOR SHARE OF ai" in vsql
        assert vargs == ("agent-1", "group-1")
        return self.vrow


@pytest.mark.parametrize(
    ("vagent", "vblueprint", "vworkspace", "vprofile_id", "vsource"),
    [
        ("voice_sidra", "voice_boss", "voice_default", "voice_sidra", "agent_instance"),
        (None, "voice_boss", "voice_default", "voice_boss", "blueprint"),
        (None, None, "voice_sidra", "voice_sidra", "workspace"),
        (None, None, None, "voice_default", "global"),
    ],
)
@pytest.mark.asyncio
async def test_resolve_voice_profile_uses_durable_precedence(vagent, vblueprint, vworkspace, vprofile_id, vsource) -> None:
    vresolved = await voice_profile_ops.resolve_agent_voice_profile(
        _Connection(
            {
                "agent_voice_profile_id": vagent,
                "blueprint_voice_profile_id": vblueprint,
                "ws_voice_profile_id": vworkspace,
            }
        ),
        "group-1",
        "agent-1",
    )

    assert vresolved.vprofile.vprofile_id == vprofile_id
    assert vresolved.vsource == vsource
    assert vresolved.vprofile.vtts_provider == "openrouter"
    assert vresolved.vprofile.vtts_model == voice_contracts.VOICE_DEFAULT_TTS_MODEL
    assert vresolved.vprofile.vtts_speed == 1.0


@pytest.mark.asyncio
async def test_resolve_voice_profile_skips_unregistered_or_invalid_candidates() -> None:
    vresolved = await voice_profile_ops.resolve_agent_voice_profile(
        _Connection(
            {
                "agent_voice_profile_id": "voice_unregistered",
                "blueprint_voice_profile_id": "voice_not_real",
                "ws_voice_profile_id": "not-a-profile",
            }
        ),
        "group-1",
        "agent-1",
    )

    assert vresolved.vprofile.vprofile_id == "voice_default"
    assert vresolved.vsource == "global"


def test_registered_profile_api_validates_setter_values() -> None:
    assert voice_profile_ops.normalize_voice_profile_override("") is None
    assert voice_profile_ops.resolve_registered_voice_profile("voice_sidra").vtts_voice_id == "alloy"
    with pytest.raises(ValueError, match="not registered"):
        voice_profile_ops.normalize_voice_profile_override("voice_unknown")
    with pytest.raises(ValueError, match="invalid"):
        voice_profile_ops.normalize_voice_profile_override("voice_" + "a" * 59)


@pytest.mark.asyncio
async def test_resolve_agent_voice_profile_reads_authoritative_agent_scope() -> None:
    vresolved = await voice_profile_ops.resolve_agent_voice_profile(
        _Connection(
            {
                "agent_voice_profile_id": None,
                "blueprint_voice_profile_id": "voice_sidra",
                "ws_voice_profile_id": "voice_boss",
            }
        ),
        "group-1",
        "agent-1",
    )

    assert vresolved.vprofile.vprofile_id == "voice_sidra"
    assert vresolved.vsource == "blueprint"
