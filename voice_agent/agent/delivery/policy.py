from __future__ import annotations

import dataclasses
import enum

from voice_agent.agent.events import (
    ActionAwaitingConfirmation,
    BackgroundTaskCompleted,
    BackgroundTaskFailed,
    BackgroundTaskProgress,
    BackgroundTaskStarted,
    ContextUpdate,
    Criticality,
    SemanticEvent,
)

NEVER_SPOKEN = (BackgroundTaskStarted, BackgroundTaskProgress, ContextUpdate)


class DeliveryDecision(enum.Enum):
    SPEAK_NOW = "speak_now"
    QUEUE = "queue"
    INTERRUPT = "interrupt"
    STORE_SILENTLY = "store_silently"
    DROP = "drop"


@dataclasses.dataclass(frozen=True, slots=True)
class DeliveryContext:
    vuser_speaking: bool
    vassistant_speaking: bool
    vconversation_epoch: int
    vcurrent_intent: str = ""
    vidle_seconds: float = 0.0


class DeliveryPolicy:
    """Decides what happens when a result arrives, so a background task never seizes the floor.

    A result is information, not speech. The only case that interrupts is a correction, because
    letting the assistant finish a sentence that is now wrong is worse than cutting it off.
    """

    def __init__(self, *, vrelevant_idle_seconds: float = 1.5) -> None:
        self.vrelevant_idle_seconds = vrelevant_idle_seconds

    def decide(self, vevent: SemanticEvent, vcontext: DeliveryContext) -> DeliveryDecision:
        if isinstance(vevent, NEVER_SPOKEN):
            return DeliveryDecision.STORE_SILENTLY

        if vevent.vcorrelation.vconversation_epoch < vcontext.vconversation_epoch and vevent.vcriticality is not Criticality.CORRECTION:
            return DeliveryDecision.STORE_SILENTLY

        if vcontext.vuser_speaking:
            return DeliveryDecision.QUEUE

        if vevent.vcriticality is Criticality.CORRECTION:
            return DeliveryDecision.INTERRUPT if vcontext.vassistant_speaking else DeliveryDecision.SPEAK_NOW

        if vcontext.vassistant_speaking:
            return DeliveryDecision.QUEUE

        if isinstance(vevent, ActionAwaitingConfirmation):
            return DeliveryDecision.SPEAK_NOW

        if isinstance(vevent, BackgroundTaskCompleted | BackgroundTaskFailed):
            if vcontext.vidle_seconds >= self.vrelevant_idle_seconds:
                return DeliveryDecision.SPEAK_NOW
            return DeliveryDecision.QUEUE

        return DeliveryDecision.STORE_SILENTLY
