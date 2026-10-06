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

    @classmethod
    def from_payload(cls, vpayload: dict[str, object]) -> SemanticResult:
        vfacts = tuple(
            Fact(vclaim=str(vitem.get("claim", "")), vconfidence=float(vitem.get("confidence", 1.0)))
            for vitem in vpayload.get("facts", [])  # type: ignore[union-attr]
            if isinstance(vitem, dict)
        )
        return cls(
            vtask_id=str(vpayload.get("task_id", "")),
            vtopic=str(vpayload.get("topic", "")),
            vsummary=str(vpayload.get("summary", "")),
            vimportant_points=tuple(str(vpoint) for vpoint in vpayload.get("important_points", [])),  # type: ignore[union-attr]
            vfacts=vfacts,
            vopen_questions=tuple(str(vq) for vq in vpayload.get("open_questions", [])),  # type: ignore[union-attr]
        )


BACKGROUND_REASONING_PROMPT = """You are a background reasoning worker. You are not talking to the user.

Produce a semantic result another system will phrase aloud. Return JSON only:
{"summary": one or two sentences stating the conclusion,
 "important_points": up to five short strings,
 "facts": [{"claim": string, "confidence": 0.0-1.0}],
 "open_questions": short strings for anything you could not settle}

Do not write conversational prose, do not address the user, and do not describe your process.
State confidence honestly: a low-confidence fact is more useful than a confident guess."""
