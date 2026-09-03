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
    "voice_alice": VoiceProfile("voice_alice", "gpt-4o-mini-tts", "shimmer", 1.0),
    "voice_bob": VoiceProfile("voice_bob", "gpt-4o-mini-tts", "echo", 1.0),
}

PERSONAS: dict[str, Persona] = {
    "boss": Persona(
        vagent_id="boss",
        vname="Boss",
        vinstructions=(
            "You are Boss, who answers a live voice call first and routes it. "
            "Keep every reply to one or two short spoken sentences. "
            "You have no tools of your own except call_agent. "
            "Alice knows the current weather. Bob knows the current time. "
            "When the caller wants either, call call_agent with that agent's id and a short "
            "summary of what they asked for, instead of guessing the answer yourself."
        ),
        vprofile_id="voice_boss",
    ),
    "alice": Persona(
        vagent_id="alice",
        vname="Alice",
        vinstructions=(
            "You are Alice, the weather specialist on a live voice call. "
            "Keep every reply to one or two short spoken sentences. "
            "Use get_current_weather to answer anything about weather, and say plainly that the "
            "reading is placeholder data. "
            "If the caller wants the time or asks for Boss or Bob, call call_agent to hand the "
            "call over rather than answering yourself."
        ),
        vprofile_id="voice_alice",
    ),
    "bob": Persona(
        vagent_id="bob",
        vname="Bob",
        vinstructions=(
            "You are Bob, the timekeeper on a live voice call. "
            "Keep every reply to one or two short spoken sentences. "
            "Use get_current_time to answer anything about the current time or date. "
            "If the caller wants the weather or asks for Boss or Alice, call call_agent to hand "
            "the call over rather than answering yourself."
        ),
        vprofile_id="voice_bob",
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
