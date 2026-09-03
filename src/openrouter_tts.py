from __future__ import annotations

import os

import aiohttp
from livekit.agents import APIConnectOptions, APIStatusError, tts, utils

import contracts
import pcm_resample
from personas import VoiceProfile


class OpenRouterTTS(tts.TTS):
    def __init__(self, vprofile: VoiceProfile) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=contracts.VOICE_ROOM_SAMPLE_RATE_HZ,
            num_channels=contracts.VOICE_PCM_CHANNELS,
        )
        self._vprofile = vprofile
        self._vapi_key = os.environ["OPENROUTER_API_KEY"]
        self._vsession: aiohttp.ClientSession | None = None

    @property
    def vprofile(self) -> VoiceProfile:
        return self._vprofile

    def session(self) -> aiohttp.ClientSession:
        if self._vsession is None or self._vsession.closed:
            self._vsession = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._vapi_key}"},
                timeout=aiohttp.ClientTimeout(
                    total=contracts.VOICE_TTS_STREAM_DEADLINE_S,
                    sock_read=contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S,
                ),
            )
        return self._vsession

    def synthesize(self, text: str, *, conn_options: APIConnectOptions) -> OpenRouterChunkedStream:
        return OpenRouterChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        if self._vsession is not None and not self._vsession.closed:
            await self._vsession.close()


class OpenRouterChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        vtts: OpenRouterTTS = self._tts
        vprofile = vtts.vprofile
        vbody = {
            "model": vprofile.vmodel,
            "input": self.input_text,
            "voice": vprofile.vvoice,
            "response_format": contracts.OPENROUTER_TTS_RESPONSE_FORMAT,
            "speed": vprofile.vspeed,
        }
        async with vtts.session().post(contracts.OPENROUTER_TTS_ENDPOINT, json=vbody) as vresponse:
            if vresponse.status != 200:
                raise APIStatusError(
                    f"openrouter tts refused the request: {(await vresponse.text())[:200]}",
                    status_code=vresponse.status,
                )
            vprovider_rate = contracts.require_accepted_tts_sample_rate(
                contracts.sample_rate_from_content_type(vresponse.headers.get("content-type", ""))
            )
            vresampler = pcm_resample.resampler_for(vprovider_rate)
            output_emitter.initialize(
                request_id=utils.shortuuid("OR_"),
                sample_rate=contracts.VOICE_ROOM_SAMPLE_RATE_HZ,
                num_channels=contracts.VOICE_PCM_CHANNELS,
                mime_type="audio/pcm",
            )
            async for vchunk in vresponse.content.iter_chunked(4096):
                output_emitter.push(vresampler.process(vchunk) if vresampler else vchunk)
            if vresampler:
                output_emitter.push(vresampler.flush())
        output_emitter.flush()
