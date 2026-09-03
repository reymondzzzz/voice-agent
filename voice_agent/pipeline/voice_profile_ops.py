from __future__ import annotations

import dataclasses
import re
from types import MappingProxyType

from voice_agent.pipeline import voice_contracts


VOICE_GLOBAL_PROFILE_ID = "voice_default"
_VOICE_PROFILE_ID_RE = re.compile(r"^voice_[a-z0-9_]{1,58}$")


class VoiceProfileError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class VoiceProfile:
    vprofile_id: str
    vtts_provider: str
    vtts_model: str
    vtts_voice_id: str
    vtts_speed: float


@dataclasses.dataclass(frozen=True)
class ResolvedVoiceProfile:
    vprofile: VoiceProfile
    vsource: str


VOICE_PROFILE_REGISTRY = MappingProxyType(
    {
        "voice_default": VoiceProfile("voice_default", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 1.0),
        "voice_boss": VoiceProfile("voice_boss", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 1.0),
        "voice_sidra": VoiceProfile("voice_sidra", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 1.0),
    }
)


def _normalize_voice_profile_id(vprofile_id: str | None) -> str | None:
    if vprofile_id is None:
        return None
    if type(vprofile_id) is not str or vprofile_id.strip() != vprofile_id or _VOICE_PROFILE_ID_RE.fullmatch(vprofile_id) is None:
        raise ValueError("voice profile id is invalid")
    if vprofile_id not in VOICE_PROFILE_REGISTRY:
        raise ValueError("voice profile id is not registered")
    return vprofile_id


def normalize_voice_profile_override(vprofile_id: str) -> str | None:
    if vprofile_id == "":
        return None
    return _normalize_voice_profile_id(vprofile_id)


def resolve_registered_voice_profile(vprofile_id: str) -> VoiceProfile:
    profile_id = _normalize_voice_profile_id(vprofile_id)
    if profile_id is None:
        raise ValueError("voice profile id is required")
    return VOICE_PROFILE_REGISTRY[profile_id]


def _resolve_voice_profile(
    vagent_voice_profile_id: str | None,
    vblueprint_voice_profile_id: str | None,
    vworkspace_voice_profile_id: str | None,
) -> ResolvedVoiceProfile:
    candidates = (
        ("agent_instance", vagent_voice_profile_id),
        ("blueprint", vblueprint_voice_profile_id),
        ("workspace", vworkspace_voice_profile_id),
        ("global", VOICE_GLOBAL_PROFILE_ID),
    )
    for vsource, candidate in candidates:
        profile = _registered_profile(candidate)
        if profile is not None:
            return ResolvedVoiceProfile(profile, vsource)
    raise VoiceProfileError("global voice profile is unavailable")


def _registered_profile(vcandidate: object) -> VoiceProfile | None:
    if type(vcandidate) is not str or _VOICE_PROFILE_ID_RE.fullmatch(vcandidate) is None:
        return None
    return VOICE_PROFILE_REGISTRY.get(vcandidate)


async def resolve_agent_voice_profile(vconn, vfgroup_id: str, vagent_id: str) -> ResolvedVoiceProfile:
    row = await vconn.fetchrow(
        "SELECT ai.agent_voice_profile_id, bp.blueprint_voice_profile_id, w.ws_voice_profile_id "
        "FROM flexus_agent_instance ai "
        "JOIN flexus_group g ON g.fgroup_id = ai.located_fgroup_id AND g.fgroup_archived_ts = 0 "
        "JOIN flexus_workspace w ON w.ws_id = g.ws_id AND w.ws_archived_ts = 0 "
        "LEFT JOIN flexus_agent_blueprint bp ON bp.blueprint_name = ai.agent_source_blueprint_name AND bp.blueprint_version = ai.agent_source_blueprint_version "
        "WHERE ai.agent_id = $1 AND ai.located_fgroup_id = $2 AND ai.agent_archived_ts = 0 AND ai.agent_enabled = true "
        "FOR SHARE OF ai",
        vagent_id,
        vfgroup_id,
    )
    if row is None:
        raise VoiceProfileError("enabled agent voice profile is unavailable")
    return _resolve_voice_profile(
        row["agent_voice_profile_id"],
        row["blueprint_voice_profile_id"],
        row["ws_voice_profile_id"],
    )
