from __future__ import annotations

import logging

from voice_agent.agent.delivery.policy import DeliveryContext, DeliveryDecision, DeliveryPolicy, topic_relevance
from voice_agent.agent.events import BackgroundTaskCompleted, Criticality
from voice_agent.agent.conversation.routing import (
    RouteAction,
    RoutingDecision,
    SemanticRouter,
    classify_relationship,
    is_active,
    looks_like_recall,
)
from voice_agent.agent.conversation.runtime import ConversationRuntime
from voice_agent.agent.conversation.speech_policy import ResponseMode, SpeechPolicy, informed_injection, mode_for
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.correlation import Correlation

logger = logging.getLogger("voice_agent.director")

SPECULATION_CONFIDENCE = 0.7
TRIVIAL_WORD_COUNT = 4


class ConversationDirector:
    """Decides what the conversation should do, one utterance at a time.

    It sits above the bridge because these are semantic decisions — delegate or answer, extend or
    supersede, surface a stored result or hold it — and none of them belong in transport callbacks.
    """

    def __init__(
        self,
        *,
        vrouter: SemanticRouter,
        vsupervisor: TaskSupervisor,
        vregistry: TaskRegistry,
        vruntime: ConversationRuntime,
        vpolicy: DeliveryPolicy | None = None,
    ) -> None:
        self.vrouter = vrouter
        self.vsupervisor = vsupervisor
        self.vregistry = vregistry
        self.vruntime = vruntime
        self.vdelivery = vpolicy or DeliveryPolicy()
        self.vcandidate: TaskRecord | None = None
        self.vcurrent_topic = ""
        self.vlast_decision: RoutingDecision | None = None

    def active_tasks(self) -> tuple[TaskRecord, ...]:
        return tuple(vrecord for vrecord in self.vregistry.all() if is_active(vrecord))

    def undelivered_results(self) -> tuple[TaskRecord, ...]:
        return tuple(
            vrecord
            for vrecord in self.vregistry.all()
            if vrecord.vstatus is TaskStatus.COMPLETED and not vrecord.vdelivered
        )

    def response_mode(self, *, vlast_text: str = "") -> ResponseMode:
        return mode_for(
            vhas_pending_tasks=bool(self.active_tasks()),
            vhas_undelivered_results=bool(self.undelivered_results()),
            vturn_is_trivial=len(vlast_text.split()) <= TRIVIAL_WORD_COUNT,
        )

    def speech_policy(self, *, vlast_text: str = "") -> SpeechPolicy:
        vmode = self.response_mode(vlast_text=vlast_text)
        vtransient = ""
        if vmode is ResponseMode.INFORMED:
            vrecord = self.undelivered_results()[0]
            vtransient = informed_injection(
                vtopic=vrecord.vtopic or vrecord.vgoal,
                vsummary=vrecord.vresult.vsummary if vrecord.vresult else "",
                vimportant_points=tuple(str(vpoint) for vpoint in (vrecord.vresult.vpayload.get("important_points", []) if vrecord.vresult else [])),
            )
        return SpeechPolicy(vmode=vmode, vtransient=vtransient)

    async def on_partial_transcript(self, vtext: str) -> TaskRecord | None:
        """Start work before end of turn when the intent is already unambiguous."""

        vdecision = await self.vrouter.route(vtext)
        if vdecision.vaction is not RouteAction.DELEGATE or vdecision.vconfidence < SPECULATION_CONFIDENCE:
            return None
        vspec = self._spec_for(vdecision)
        vcandidate = self.vsupervisor.ensure_candidate(
            vspec=vspec,
            vconversation_epoch=self.vruntime.vconversation_epoch,
            vsource_turn_id=self.vruntime.vcurrent_turn_id,
        )
        if vcandidate is not None and vcandidate.vstatus is TaskStatus.CANDIDATE:
            self.vcandidate = vcandidate
            logger.info("speculative task created", extra={**vcandidate.log_fields(), "goal": vcandidate.vgoal})
        return vcandidate

    async def on_final_transcript(self, vtext: str) -> tuple[RoutingDecision, TaskRecord | None]:
        vdecision = await self.vrouter.route(vtext)
        self.vlast_decision = vdecision

        if looks_like_recall(vtext):
            return vdecision, None

        if vdecision.vaction is not RouteAction.DELEGATE:
            if vdecision.vtopic:
                self.vcurrent_topic = vdecision.vtopic
            await self._discard_candidate()
            return vdecision, None

        vrelationship, vrelates_to = classify_relationship(vdecision, self.active_tasks())
        vspec = self._spec_for(vdecision)
        vrecord = await self.vsupervisor.reconcile_with_final(
            vcandidate=self.vcandidate,
            vfinal_spec=vspec,
            vrelationship=vrelationship,
            vconversation_epoch=self.vruntime.vconversation_epoch,
            vsource_turn_id=self.vruntime.vcurrent_turn_id,
        )
        self.vcandidate = None
        if vrecord is not None:
            vrecord.vrelates_to = vrelates_to
            self.vcurrent_topic = vrecord.vtopic or self.vcurrent_topic
            self.vruntime.vcurrent_intent = vdecision.vtopic or self.vruntime.vcurrent_intent
        return vdecision, vrecord

    def recall_for(self, vtext: str) -> TaskRecord | None:
        """Find the stored result the user is asking about.

        A result held back because the topic had moved on must be instantly available the moment
        they ask for it; otherwise holding it is indistinguishable from losing it.
        """

        vcandidates = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus is TaskStatus.COMPLETED]
        if not vcandidates:
            return None
        vasked = " ".join(vtext.casefold().split())
        vbest, vbest_score = None, 0.0
        for vrecord in vcandidates:
            vscore = topic_relevance(vrecord.vtopic or vrecord.vgoal, vasked)
            if vscore > vbest_score:
                vbest, vbest_score = vrecord, vscore
        if vbest is not None and vbest_score > 0.0:
            return vbest
        vundelivered = [vrecord for vrecord in vcandidates if not vrecord.vdelivered]
        return vundelivered[-1] if vundelivered else None

    def delivery_decision(self, vevent: BackgroundTaskCompleted, *, vexplicitly_requested: bool = False) -> DeliveryDecision:
        return self.vdelivery.decide(
            vevent,
            DeliveryContext(
                vuser_speaking=self.vruntime.vuser_speaking,
                vassistant_speaking=self.vruntime.vassistant_speaking,
                vconversation_epoch=self.vruntime.vconversation_epoch,
                vcurrent_intent=self.vruntime.vcurrent_intent,
                vcurrent_topic=self.vcurrent_topic,
                vidle_seconds=self.vruntime.vidle_seconds,
                vexplicitly_requested=vexplicitly_requested,
            ),
        )

    def completion_event(self, vrecord: TaskRecord, *, vurgent: bool = False) -> BackgroundTaskCompleted:
        return BackgroundTaskCompleted(
            vcorrelation=Correlation.create(
                vconversation_id=vrecord.vconversation_id,
                vsession_id=vrecord.vsession_id,
                vconversation_epoch=vrecord.vconversation_epoch,
                vturn_id=vrecord.vsource_turn_id,
            ),
            vtask_id=vrecord.vtask_id,
            vresult=vrecord.vresult,
            vgoal=vrecord.vtopic or vrecord.vgoal,
            vcriticality=Criticality.HIGH if vurgent else Criticality.NORMAL,
        )

    def mark_delivered(self, vrecord: TaskRecord) -> None:
        vrecord.vdelivered = True

    async def _discard_candidate(self) -> None:
        if self.vcandidate is not None and self.vcandidate.vstatus is TaskStatus.CANDIDATE:
            self.vsupervisor.cancel(self.vcandidate.vtask_id)
        self.vcandidate = None

    def _spec_for(self, vdecision: RoutingDecision) -> TaskSpec:
        return TaskSpec(
            vgoal=vdecision.vnormalized_goal or "",
            vmode=TaskMode.BACKGROUND,
            vintent=vdecision.vtopic or "",
            vtopic=vdecision.vtopic or "",
            vurgency=vdecision.vurgency,
        )
