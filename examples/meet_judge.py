from __future__ import annotations

import asyncio
import os
import time

from voice_agent.realtime import events
from voice_agent.realtime.audio import NullAudioSink
from voice_agent.realtime.qwen.session import QwenOmniSession

JUDGE_TIMEOUT_S = 5.0
JUDGE_RENEW_EVERY = 40
# DashScope closes a session after 300s without a response; an idle one is renewed before a question, not after.
JUDGE_IDLE_S = 240.0


class MeetJudge:
    """Internal one-word checks (who a line is for, whether a reply promised work or told a result), asked on a
    text-only Qwen session of their own.

    Not her session: DashScope ignores `conversation: "none"` and client item ids, and deleting a verdict item can
    delete the line before it, so every verdict stayed in her history. Six of them made her answer a question with
    "RESPOND" 8 times in 8. Each prompt carries what it needs, so verdicts piling up here cost nothing.
    """

    def __init__(self, vname: str, vmodel: str) -> None:
        self.vname, self.vmodel = vname, vmodel
        self.vsession: QwenOmniSession | None = None
        self.vevents = None
        self.vjudged = 0
        self.vused_at = 0.0
        self.vlock = asyncio.Lock()

    async def __call__(self, vprompt: str) -> str:
        async with self.vlock:
            for _ in range(2):
                if self.vsession is None or self.vjudged >= JUDGE_RENEW_EVERY or time.monotonic() - self.vused_at > JUDGE_IDLE_S:
                    await self.renew()
                try:
                    vreply = await asyncio.wait_for(self.ask(vprompt), JUDGE_TIMEOUT_S)
                except (StopAsyncIteration, ConnectionResetError):
                    # DashScope closed the session under us; a fresh one gets the same question.
                    await self.close()
                    continue
                except TimeoutError:
                    await self.close()
                    return ""
                self.vused_at = time.monotonic()
                return vreply
            return ""

    async def ask(self, vprompt: str) -> str:
        assert self.vsession is not None and self.vevents is not None
        await self.vsession.request_response(events.ResponseRequest(vcorrelation=self.vsession.correlation(), vinstructions=vprompt, vtext_only=True))
        self.vjudged += 1
        vparts: list[str] = []
        while True:
            vevent = await anext(self.vevents)
            if isinstance(vevent, events.AssistantTranscript):
                vparts.append(vevent.vtext)
            elif isinstance(vevent, events.AssistantSpeechStopped):
                return "".join(vparts)
            elif isinstance(vevent, events.RealtimeSessionError) and not vevent.vrecoverable:
                return ""

    async def renew(self) -> None:
        await self.close()
        self.vsession = QwenOmniSession(
            vapi_key=os.environ["DASHSCOPE_API_KEY"],
            vconversation_id=f"{self.vname}-judge",
            vsession_id=f"{self.vname}-judge",
            vepoch_provider=lambda: 0,
            vaudio_sink=NullAudioSink(),
            vmodel=self.vmodel,
            vinstructions="You answer internal checks about a meeting with a single word.",
            vtools=[],
            vauto_response=False,
        )
        await self.vsession.start()
        self.vevents = self.vsession.events()
        self.vjudged = 0
        self.vused_at = time.monotonic()

    async def close(self) -> None:
        if self.vsession is not None:
            vsession, self.vsession, self.vevents = self.vsession, None, None
            await vsession.close()
