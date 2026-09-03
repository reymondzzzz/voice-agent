from __future__ import annotations

import base64
import os

import aiohttp
from livekit import rtc
from livekit.agents import APIConnectOptions, APIStatusError, stt, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

import contracts


class OpenRouterSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self._vmodel = contracts.VOICE_DEFAULT_STT_MODEL
        self._vapi_key = os.environ["OPENROUTER_API_KEY"]
        self._vsession: aiohttp.ClientSession | None = None

    def session(self) -> aiohttp.ClientSession:
        if self._vsession is None or self._vsession.closed:
            self._vsession = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._vapi_key}"},
                timeout=aiohttp.ClientTimeout(total=contracts.VOICE_STT_REQUEST_DEADLINE_S),
            )
        return self._vsession

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        vwav = rtc.combine_audio_frames(buffer).to_wav_bytes()
        vbody: dict[str, object] = {
            "model": self._vmodel,
            "input_audio": {
                "data": base64.b64encode(vwav).decode("ascii"),
                "format": contracts.OPENROUTER_STT_INPUT_AUDIO_FORMAT,
            },
        }
        if language:
            vbody["language"] = language

        async with self.session().post(contracts.OPENROUTER_STT_ENDPOINT, json=vbody) as vresponse:
            if vresponse.status != 200:
                raise APIStatusError(
                    f"openrouter stt refused the request: {(await vresponse.text())[:200]}",
                    status_code=vresponse.status,
                )
            vpayload = await vresponse.json()

        vtext = vpayload.get("text")
        if not isinstance(vtext, str):
            raise APIStatusError("openrouter stt returned no text field", status_code=vresponse.status)

        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=vresponse.headers.get("x-generation-id") or utils.shortuuid("OR_"),
            alternatives=[stt.SpeechData(text=vtext.strip(), language=language or "")],
        )

    async def aclose(self) -> None:
        if self._vsession is not None and not self._vsession.closed:
            await self._vsession.close()
