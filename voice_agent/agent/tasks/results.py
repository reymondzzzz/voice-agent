from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class Fact:
    vclaim: str
    vconfidence: float = 1.0


@dataclasses.dataclass(frozen=True, slots=True)
class SemanticResult:
    """What a background worker returns: meaning, not prose.

    The reasoning model decides WHAT should be communicated; the speech actor decides WHEN and HOW.
    Returning a paragraph of finished prose would collapse that split and get read out verbatim.
    """

    vtask_id: str
    vtopic: str
    vsummary: str
    vimportant_points: tuple[str, ...] = ()
    vfacts: tuple[Fact, ...] = ()
    vopen_questions: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, object]:
        return {
            "task_id": self.vtask_id,
            "topic": self.vtopic,
            "summary": self.vsummary,
            "important_points": list(self.vimportant_points),
            "facts": [{"claim": vfact.vclaim, "confidence": vfact.vconfidence} for vfact in self.vfacts],
            "open_questions": list(self.vopen_questions),
        }

