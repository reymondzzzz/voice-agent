from __future__ import annotations

import dataclasses

from personas import Persona, PersonaUnknown, VoiceProfile, resolve_persona, resolve_voice_profile


MAX_HANDOFF_SUMMARY_CHARS = 500


class VoiceHandoffRefused(RuntimeError):
    def __init__(self, vreason: str) -> None:
        super().__init__(vreason)
        self.vreason = vreason


@dataclasses.dataclass(frozen=True)
class VoiceHandoffAuthorization:
    vsource_agent_id: str
    vtarget_agent_id: str
    vtarget_name: str
    vhandoff_summary: str
    vprofile: VoiceProfile


def authorize_handoff(
    vsource_agent_id: str,
    vtarget_agent_id: str,
    vhandoff_summary: str,
    *,
    vhandoff_pending: bool = False,
) -> VoiceHandoffAuthorization:
    if vhandoff_pending:
        raise VoiceHandoffRefused("a handoff is already pending on this call")
    vtarget = _require_known_target(vtarget_agent_id)
    _require_distinct_target(vsource_agent_id, vtarget.vagent_id)
    vsummary = _require_bounded_summary(vhandoff_summary)
    return VoiceHandoffAuthorization(
        vsource_agent_id=vsource_agent_id,
        vtarget_agent_id=vtarget.vagent_id,
        vtarget_name=vtarget.vname,
        vhandoff_summary=vsummary,
        vprofile=resolve_voice_profile(vtarget.vprofile_id),
    )


def _require_known_target(vtarget_agent_id: str) -> Persona:
    vagent_id = str(vtarget_agent_id or "").strip().lower()
    if not vagent_id:
        raise VoiceHandoffRefused("target_agent_id is required")
    try:
        return resolve_persona(vagent_id)
    except PersonaUnknown:
        raise VoiceHandoffRefused(f"target agent {vagent_id!r} not found") from None


def _require_distinct_target(vsource_agent_id: str, vtarget_agent_id: str) -> None:
    if vsource_agent_id == vtarget_agent_id:
        raise VoiceHandoffRefused("target agent is already active on this call")


def _require_bounded_summary(vhandoff_summary: str) -> str:
    vtext = str(vhandoff_summary or "").strip()
    if not vtext:
        raise VoiceHandoffRefused("handoff summary is required")
    if len(vtext) > MAX_HANDOFF_SUMMARY_CHARS:
        raise VoiceHandoffRefused(f"handoff summary exceeds {MAX_HANDOFF_SUMMARY_CHARS} characters")
    if any(ord(vchar) < 0x20 for vchar in vtext):
        raise VoiceHandoffRefused("handoff summary contains control characters")
    return vtext


class PendingHandoff:
    def __init__(self) -> None:
        self._vauth: VoiceHandoffAuthorization | None = None

    def armed(self) -> bool:
        return self._vauth is not None

    def arm(self, vauth: VoiceHandoffAuthorization) -> None:
        self._vauth = vauth

    def take(self) -> VoiceHandoffAuthorization | None:
        vauth = self._vauth
        self._vauth = None
        return vauth
