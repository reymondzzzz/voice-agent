from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class VoiceProfile:
    vprofile_id: str
    vmodel: str
    vvoice: str
    vspeed: float


@dataclasses.dataclass(frozen=True)
class Persona:
    vagent_id: str
    vname: str
    vinstructions: str
    vprofile_id: str


VOICE_GLOBAL_PROFILE_ID = "voice_default"

VOICE_PROFILES: dict[str, VoiceProfile] = {
    "voice_default": VoiceProfile("voice_default", "gpt-4o-mini-tts", "alloy", 1.0),
    "voice_boss": VoiceProfile("voice_boss", "gpt-4o-mini-tts", "onyx", 1.0),
    "voice_sidra": VoiceProfile("voice_sidra", "gpt-4o-mini-tts", "shimmer", 1.0),
}

PERSONAS: dict[str, Persona] = {
    "boss": Persona(
        vagent_id="boss",
        vname="Boss",
        vinstructions=(
            "You are Boss, the primary assistant on a live voice call. "
            "Keep answers short and speakable. "
            "When the caller asks for a specialist by name, call the call_agent tool "
            "with a concise scoped summary instead of answering yourself."
        ),
        vprofile_id="voice_boss",
    ),
    "sidra": Persona(
        vagent_id="sidra",
        vname="Sidra",
        vinstructions=(
            "You are Sidra, a sales-reporting specialist on a live voice call. "
            "Confirm the scoped subject you were handed, then help with it. "
            "When the caller asks for Boss, call the call_agent tool to hand the call back."
        ),
        vprofile_id="voice_sidra",
    ),
}


class PersonaUnknown(KeyError):
    pass


def resolve_persona(vagent_id: str) -> Persona:
    vpersona = PERSONAS.get(vagent_id)
    if vpersona is None:
        raise PersonaUnknown(vagent_id)
    return vpersona


def resolve_voice_profile(vprofile_id: str) -> VoiceProfile:
    return VOICE_PROFILES.get(vprofile_id) or VOICE_PROFILES[VOICE_GLOBAL_PROFILE_ID]
