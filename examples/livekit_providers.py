from __future__ import annotations

import os

from livekit import rtc
from livekit.agents import APIConnectOptions, stt, tts, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

from voice_agent.pipeline import openrouter_tts, voice_contracts, voice_profile_ops, voice_stt


class FlexusOpenRouterTTS(tts.TTS):
    def __init__(self, vprofile: voice_profile_ops.VoiceProfile) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
            num_channels=voice_contracts.VOICE_PCM_CHANNELS,
        )
        self.vprofile = vprofile
        self.vapi_key = os.environ["OPENROUTER_API_KEY"]

    def synthesize(self, text: str, *, conn_options: APIConnectOptions) -> FlexusChunkedStream:
        return FlexusChunkedStream(tts=self, input_text=text, conn_options=conn_options)


class FlexusChunkedStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        vtts: FlexusOpenRouterTTS = self._tts
        vrequest = openrouter_tts.VoiceTtsRequest(
            vtts_input=self.input_text,
            vtts_voice=vtts.vprofile.vtts_voice_id,
            vtts_model=vtts.vprofile.vtts_model,
            vtts_speed=vtts.vprofile.vtts_speed,
        )
        vstream = await openrouter_tts.open_openrouter_pcm_stream(vrequest, vtts_api_key=vtts.vapi_key)
        async with vstream:
            output_emitter.initialize(
                request_id=vstream.vstream_generation_id,
                sample_rate=vstream.vstream_sample_rate_hz,
                num_channels=vstream.vstream_channels,
                mime_type="audio/pcm",
            )
            async for vchunk in vstream:
                output_emitter.push(vchunk)
        output_emitter.flush()


class FlexusOpenRouterSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.vprovider = voice_stt.OpenRouterSttProvider(os.environ["OPENROUTER_API_KEY"])

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        vframe = rtc.combine_audio_frames(buffer)
        vconfig = voice_stt.SttConfig(
            sttc_model=os.environ.get("FLEXUS_VOICE_STT_MODEL") or voice_contracts.VOICE_DEFAULT_STT_MODEL,
            sttc_language=language or None,
            sttc_sample_rate_hz=vframe.sample_rate,
            sttc_deadline_s=voice_contracts.VOICE_STT_REQUEST_DEADLINE_S,
        )
        vevent = await self.vprovider.transcribe_utterance(bytes(vframe.data), vconfig)
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=vevent.stte_provider_generation_id or utils.shortuuid("OR_"),
            alternatives=[stt.SpeechData(text=vevent.stte_text, language=vevent.stte_language or "")],
        )

    async def aclose(self) -> None:
        await self.vprovider.aclose()
