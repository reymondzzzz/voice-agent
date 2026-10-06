from __future__ import annotations

import dataclasses
import enum
import re
from typing import Protocol

from voice_agent.agent.tasks.models import TaskRecord, TaskRelationship, TaskStatus, Urgency, fingerprint_goal


class RouteAction(enum.Enum):
    LOCAL = "local"
    DELEGATE = "delegate"
    WAIT_FOR_MORE_INPUT = "wait_for_more_input"


@dataclasses.dataclass(frozen=True, slots=True)
class RoutingDecision:
    vaction: RouteAction
    vconfidence: float
    vtopic: str | None = None
    vnormalized_goal: str | None = None
    vurgency: Urgency = Urgency.NORMAL

    @property
    def vfingerprint(self) -> str:
        return fingerprint_goal(self.vnormalized_goal or "", self.vtopic or "")


class SemanticRouter(Protocol):
    """Runs on every partial transcript, so it must never call a heavy model.

    Deciding whether to invoke a reasoning model must not itself cost a reasoning model call.
    """

    async def route(self, vtext: str) -> RoutingDecision: ...


_DELEGATE_MARKERS = (
    "research", "compare", "find", "look up", "search", "investigate", "analyse", "analyze",
    "check", "figure out", "work out", "calculate", "book", "plan", "summarise", "summarize",
    "исследуй", "сравни", "найди", "проверь", "посчитай",
)
_LOCAL_MARKERS = (
    "hello", "hi", "hey", "thanks", "thank you", "yes", "no", "okay", "ok", "sure",
    "what did you find", "what about", "tell me about that", "repeat",
    "привет", "спасибо", "да", "нет",
)
_RECALL_MARKERS = ("what did you find", "what about", "how did it go", "any luck", "did you find", "что ты нашёл", "что нашла")
_URGENT_MARKERS = ("urgent", "right now", "immediately", "asap", "срочно")
_MIN_DELEGATE_WORDS = 4


def _normalize(vtext: str) -> str:
    return " ".join(vtext.casefold().strip().split())


def looks_like_recall(vtext: str) -> bool:
    vnormalized = _normalize(vtext)
    return any(vmarker in vnormalized for vmarker in _RECALL_MARKERS)


class HeuristicRouter:
    """Keyword routing, deliberately dumb and instant.

    It exists so the architecture can be proven and measured without a model in the loop; swapping
    in a small classifier means implementing SemanticRouter and nothing else.
    """

    async def route(self, vtext: str) -> RoutingDecision:
        vnormalized = _normalize(vtext)
        vwords = vnormalized.split()
        vurgency = Urgency.HIGH if any(vmarker in vnormalized for vmarker in _URGENT_MARKERS) else Urgency.NORMAL

        if looks_like_recall(vnormalized):
            return RoutingDecision(RouteAction.LOCAL, 0.9, vtopic=None, vnormalized_goal=None, vurgency=vurgency)

        vhas_delegate_marker = any(vmarker in vnormalized for vmarker in _DELEGATE_MARKERS)
        if vhas_delegate_marker and len(vwords) < _MIN_DELEGATE_WORDS:
            return RoutingDecision(RouteAction.WAIT_FOR_MORE_INPUT, 0.4, vurgency=vurgency)
        if vhas_delegate_marker:
            vconfidence = 0.75 if len(vwords) < 8 else 0.9
            return RoutingDecision(
                RouteAction.DELEGATE,
                vconfidence,
                vtopic=_topic_of(vnormalized),
                vnormalized_goal=vtext.strip(),
                vurgency=vurgency,
            )
        if any(vnormalized.startswith(vmarker) or vnormalized == vmarker for vmarker in _LOCAL_MARKERS):
            return RoutingDecision(RouteAction.LOCAL, 0.85, vurgency=vurgency)
        if len(vwords) < 3:
            return RoutingDecision(RouteAction.WAIT_FOR_MORE_INPUT, 0.3, vurgency=vurgency)
        return RoutingDecision(RouteAction.LOCAL, 0.6, vurgency=vurgency)


_STOPWORDS = frozenset({"the", "a", "an", "and", "or", "to", "of", "for", "me", "you", "please", "can", "could", "would", "about"})


def _topic_of(vnormalized: str) -> str:
    vwords = [vword for vword in re.findall(r"[\w'-]+", vnormalized) if vword not in _STOPWORDS]
    vsignificant = [vword for vword in vwords if vword not in _DELEGATE_MARKERS]
    return " ".join(vsignificant[:4]) or " ".join(vwords[:4])


def classify_relationship(vdecision: RoutingDecision, vactive: tuple[TaskRecord, ...]) -> tuple[TaskRelationship, str | None]:
    """Decide how a new request relates to work already running.

    Only an explicit correction supersedes. Sharing a topic is treated as EXTENDS, because a user
    adding to a request has not withdrawn it, and discarding the running work would be the wrong
    reading of "also".
    """

    if not vactive:
        return TaskRelationship.NEW, None

    vgoal = _normalize(vdecision.vnormalized_goal or "")
    vsupersede_marker = any(
        vmarker in vgoal for vmarker in ("actually", "instead", "forget", "never mind", "scratch that", "вместо", "забудь")
    )
    vtopic_words = set(_normalize(vdecision.vtopic or "").split())

    vbest: TaskRecord | None = None
    vbest_overlap = 0.0
    for vrecord in vactive:
        vwords = set(_normalize(vrecord.vtopic).split())
        if not vwords or not vtopic_words:
            continue
        voverlap = len(vwords & vtopic_words) / len(vwords | vtopic_words)
        if voverlap > vbest_overlap:
            vbest, vbest_overlap = vrecord, voverlap

    if vbest is None or vbest_overlap == 0.0:
        return (TaskRelationship.SUPERSEDES, vactive[-1].vtask_id) if vsupersede_marker else (TaskRelationship.NEW, None)
    if vsupersede_marker:
        return TaskRelationship.SUPERSEDES, vbest.vtask_id
    if vbest_overlap >= 0.5:
        return TaskRelationship.EXTENDS, vbest.vtask_id
    return TaskRelationship.RELATED, vbest.vtask_id


def is_active(vrecord: TaskRecord) -> bool:
    return vrecord.vstatus in {TaskStatus.CANDIDATE, TaskStatus.PENDING, TaskStatus.RUNNING}
