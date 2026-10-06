from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True, slots=True)
class RealtimeModelCapabilities:
    """What a concrete speech backend can actually do.

    The bridge branches on these rather than assuming every provider behaves alike. A backend that
    lacks function_calling still works: delegation is then derived from transcripts by the router
    fallback, which is why native tool calls are a capability and not an assumption.
    """

    native_audio_input: bool
    native_audio_output: bool
    full_duplex: bool
    barge_in: bool
    function_calling: bool
    transcript_events: bool
    context_injection: bool
    async_context_updates: bool
    supports_response_cancel: bool = False
    supports_tool_results: bool = False
    supports_manual_response_trigger: bool = False
    supports_audio_playback_ack: bool = False

    def requires_transcript_router(self) -> bool:
        return not self.function_calling

    def can_deliver_out_of_band(self) -> bool:
        return self.context_injection and self.async_context_updates


NATIVE_DUPLEX = RealtimeModelCapabilities(
    native_audio_input=True,
    native_audio_output=True,
    full_duplex=True,
    barge_in=True,
    function_calling=True,
    transcript_events=True,
    context_injection=True,
    async_context_updates=True,
    supports_response_cancel=True,
    supports_tool_results=True,
    supports_manual_response_trigger=True,
    supports_audio_playback_ack=True,
)

