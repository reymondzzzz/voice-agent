from __future__ import annotations

import dataclasses

from voice_agent.agent.actions.models import ActionMetadata, EffectLevel


@dataclasses.dataclass(frozen=True, slots=True)
class PolicyDecision:
    vrequires_confirmation: bool
    vallowed_in_background: bool
    vreason: str


class ActionPolicy:
    """Deterministic, application-owned authority on what needs confirmation.

    This is code, not model judgement: an irreversible action always requires confirmation and can
    never be auto-committed by a background task, whatever the model believes about the user's
    wishes.
    """

    AUTO_COMMIT_LEVELS = frozenset({EffectLevel.READ, EffectLevel.WRITE_REVERSIBLE})
    ALWAYS_CONFIRM_LEVELS = frozenset({EffectLevel.WRITE_IMPORTANT, EffectLevel.IRREVERSIBLE})

    def decide(self, vmetadata: ActionMetadata, *, vfrom_background: bool = False) -> PolicyDecision:
        if vmetadata.veffect_level in self.ALWAYS_CONFIRM_LEVELS:
            return PolicyDecision(
                vrequires_confirmation=True,
                vallowed_in_background=False,
                vreason=f"{vmetadata.veffect_level.value} always requires explicit confirmation",
            )
        if vmetadata.vrequires_confirmation:
            return PolicyDecision(True, not vfrom_background, "action metadata requests confirmation")
        return PolicyDecision(False, True, f"{vmetadata.veffect_level.value} may execute directly")

    def may_auto_commit(self, vmetadata: ActionMetadata, *, vfrom_background: bool) -> bool:
        vdecision = self.decide(vmetadata, vfrom_background=vfrom_background)
        if vdecision.vrequires_confirmation:
            return False
        return vmetadata.veffect_level in self.AUTO_COMMIT_LEVELS
