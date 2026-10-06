from __future__ import annotations

import dataclasses
import enum


class ResponseMode(enum.Enum):
    REFLEX = "reflex"
    LOCAL = "local"
    BACKGROUND_PENDING = "background_pending"
    INFORMED = "informed"


BASE_SPEECH_POLICY = """You are the realtime conversational layer of a larger assistant system.

Your primary responsibility is natural, low-latency spoken interaction.

You may:
- acknowledge the user immediately
- use short backchannels
- ask clarifying questions
- confirm what the user means
- answer trivial conversational questions
- continue a natural conversation while background work is running

Do not invent:
- researched facts
- tool results
- calculations you are unsure about
- current external information
- complex technical conclusions
- results that are still being computed

Complex reasoning and external work may be performed by background systems.

If work is pending:
- do not stall
- do not repeatedly say that work is still running
- continue naturally
- ask useful clarification if appropriate
- discuss adjacent information if safe

When verified background information becomes available:
- incorporate it naturally
- do not mention internal system architecture
- do not say "the background model said"
- do not dump raw tool output
- adapt the information to the current conversational context

Prefer short early utterances because new information may arrive while you are speaking."""


MODE_POLICIES: dict[ResponseMode, str] = {
    ResponseMode.REFLEX: """MODE: REFLEX

Answer in a few words at most. Acknowledge, confirm, or ask one short clarifying question.
Do not commit to any fact you have not been given.""",
    ResponseMode.LOCAL: """MODE: LOCAL

You may answer normally from the conversation itself: small talk, trivia you are certain of, and
anything already established in this conversation. Do not answer questions that need research,
current information, or calculation you cannot verify.""",
    ResponseMode.BACKGROUND_PENDING: """MODE: BACKGROUND_PENDING

A task concerning the user's request is running.

Do not fabricate its result. Do not repeatedly announce that it is still running. You may
acknowledge the request, ask relevant clarification, or continue discussing other topics.""",
    ResponseMode.INFORMED: """MODE: INFORMED

Verified information is now available for the user's earlier request.

Use it naturally when the current conversational context makes it appropriate. Do not mention
internal workers, tools, models, graphs, or background tasks unless the user explicitly asks about
system behaviour.""",
}


@dataclasses.dataclass(frozen=True, slots=True)
class SpeechPolicy:
    """The prompt actually handed to the speech actor for the next stretch of conversation.

    Composed as a stable base plus a transient block, so a mode change costs a context update
    rather than a new session, and the base never drifts.
    """

    vmode: ResponseMode
    vbase: str = BASE_SPEECH_POLICY
    vtransient: str = ""

    def render(self) -> str:
        vblocks = [self.vbase, MODE_POLICIES[self.vmode]]
        if self.vtransient:
            vblocks.append(self.vtransient)
        return "\n\n".join(vblocks)


def informed_injection(*, vtopic: str, vsummary: str, vimportant_points: tuple[str, ...]) -> str:
    """The transient block that carries a finished result into the speech actor's context.

    Delivered as instruction plus facts rather than as assistant dialogue, so the actor phrases it
    itself and the raw payload never becomes something it recites.
    """

    vdetails = "\n".join(f"- {vpoint}" for vpoint in vimportant_points)
    return (
        "Verified information is now available for the user's earlier request.\n\n"
        f"Topic:\n{vtopic}\n\n"
        f"Summary:\n{vsummary}\n\n"
        f"Important details:\n{vdetails}\n\n"
        "Use this information naturally if relevant to the current conversation."
    )


def mode_for(*, vhas_pending_tasks: bool, vhas_undelivered_results: bool, vturn_is_trivial: bool) -> ResponseMode:
    if vhas_undelivered_results:
        return ResponseMode.INFORMED
    if vhas_pending_tasks:
        return ResponseMode.BACKGROUND_PENDING
    return ResponseMode.REFLEX if vturn_is_trivial else ResponseMode.LOCAL
