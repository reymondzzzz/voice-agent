from __future__ import annotations

import logging
from typing import Any

from voice_agent.pipeline import voice_contracts
from voice_agent.realtime import events
from voice_agent.realtime.session import RealtimeSpeechSession

logger = logging.getLogger("voice_agent.livekit")


class LiveKitTransport:
    """Moves audio between a LiveKit room and a speech session, and nothing else.

    No orchestration lives here on purpose. Everything the agent does is reachable without a room,
    which is what makes the rest of the system testable; this class only exists to carry frames and
    report participant lifecycle.
    """

    def __init__(self, *, vroom: Any, vsession: RealtimeSpeechSession, vsample_rate_hz: int = voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ) -> None:
        self.vroom = vroom
        self.vsession = vsession
        self.vsample_rate_hz = vsample_rate_hz
        self.vframes_forwarded = 0

    async def forward_frame(self, vpcm: bytes) -> None:
        if not self.vsession.vcapabilities.native_audio_input:
            return
        self.vframes_forwarded += 1
        await self.vsession.send_audio(events.InputAudioChunk(vpcm=vpcm, vsample_rate_hz=self.vsample_rate_hz))

    async def commit_input(self, *, vreason: str = "vad") -> None:
        await self.vsession.commit_audio(events.InputAudioCommit(vreason=vreason))

    def require_self_hosted(self, vlk_url: str) -> str:
        return voice_contracts.require_self_hosted_livekit_url(vlk_url)
