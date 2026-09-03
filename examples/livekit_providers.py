from __future__ import annotations

import asyncio
import os

import numpy

from livekit import rtc
from livekit.agents import APIConnectOptions, stt, tts, utils
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import AudioBuffer

from voice_agent.pipeline import (
    openrouter_tts,
    voice_contracts,
    voice_interruption_policy,
    voice_profile_ops,
    voice_speech_segments,
    voice_stt,
    voice_tts_prefetch,
)

SEGMENT_PREROLL_SECONDS = 0.75


class FlexusOpenRouterTTS(tts.TTS):
    def __init__(self, vprofile: voice_profile_ops.VoiceProfile) -> None:
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=True),
            sample_rate=voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
            num_channels=voice_contracts.VOICE_PCM_CHANNELS,
        )
        self.vprofile = vprofile
        self.vapi_key = os.environ["OPENROUTER_API_KEY"]

    def synthesize(self, text: str, *, conn_options: APIConnectOptions) -> FlexusChunkedStream:
        return FlexusChunkedStream(tts=self, input_text=text, conn_options=conn_options)

    def stream(self, *, conn_options: APIConnectOptions) -> FlexusSynthesizeStream:
        return FlexusSynthesizeStream(tts=self, conn_options=conn_options)

    async def open_segment(self, vtext: str) -> DrainedSegment:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            openrouter_tts.VoiceTtsRequest(
                vtts_input=vtext,
                vtts_voice=self.vprofile.vtts_voice_id,
                vtts_model=self.vprofile.vtts_model,
                vtts_speed=self.vprofile.vtts_speed,
            ),
            vtts_api_key=self.vapi_key,
        )
        return DrainedSegment(vstream)


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


def has_speech_energy(vpcm: bytes, vconfig: voice_interruption_policy.VoiceInterruptionConfig, vsample_rate_hz: int) -> bool:
    """Whether any short window of this audio is loud enough to be speech.

    flexus applies venergy_threshold per captured frame. LiveKit hands over a whole utterance
    including its leading and trailing silence, and averaging across that silence pushes real
    speech under the threshold, so the check runs over probable-speech-sized windows and keeps the
    loudest one.
    """
    vsamples = numpy.frombuffer(vpcm, dtype="<i2")
    if vsamples.size == 0 or vsample_rate_hz <= 0:
        return False
    vwindow = max(1, int(vsample_rate_hz * vconfig.vprobable_speech_ms / 1000))
    vmagnitudes = numpy.abs(vsamples.astype(numpy.int32))
    if vmagnitudes.size <= vwindow:
        return int(vmagnitudes.mean()) >= vconfig.venergy_threshold
    vusable = vmagnitudes[: vmagnitudes.size // vwindow * vwindow]
    vwindow_means = vusable.reshape(-1, vwindow).mean(axis=1)
    return int(vwindow_means.max()) >= vconfig.venergy_threshold


class FlexusOpenRouterSTT(stt.STT):
    def __init__(self) -> None:
        super().__init__(capabilities=stt.STTCapabilities(streaming=False, interim_results=False))
        self.vprovider = voice_stt.OpenRouterSttProvider(os.environ["OPENROUTER_API_KEY"])
        self.vinterruption = voice_interruption_policy.VOICE_DEFAULT_INTERRUPTION_CONFIG.validated()

    def empty_transcript(self) -> stt.SpeechEvent:
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=utils.shortuuid("OR_"),
            alternatives=[stt.SpeechData(text="", language="")],
        )

    async def _recognize_impl(
        self,
        buffer: AudioBuffer,
        *,
        language: NotGivenOr[str] = NOT_GIVEN,
        conn_options: APIConnectOptions,
    ) -> stt.SpeechEvent:
        vframe = rtc.combine_audio_frames(buffer)
        vpcm = bytes(vframe.data)
        if not has_speech_energy(vpcm, self.vinterruption, vframe.sample_rate):
            return self.empty_transcript()
        vconfig = voice_stt.SttConfig(
            sttc_model=os.environ.get("FLEXUS_VOICE_STT_MODEL") or voice_contracts.VOICE_DEFAULT_STT_MODEL,
            sttc_language=language or None,
            sttc_sample_rate_hz=vframe.sample_rate,
            sttc_deadline_s=voice_contracts.VOICE_STT_REQUEST_DEADLINE_S,
        )
        vevent = await self.vprovider.transcribe_utterance(vpcm, vconfig)
        vtext = vevent.stte_text if voice_interruption_policy.transcript_commits_interruption(vevent.stte_text) else ""
        return stt.SpeechEvent(
            type=stt.SpeechEventType.FINAL_TRANSCRIPT,
            request_id=vevent.stte_provider_generation_id or utils.shortuuid("OR_"),
            alternatives=[stt.SpeechData(text=vtext, language=vevent.stte_language or "")],
        )

    async def aclose(self) -> None:
        await self.vprovider.aclose()


class DrainedSegment:
    """A prefetched TTS segment that downloads while earlier segments are still playing.

    Two things make this necessary. httpx does not fetch a streamed body until it is iterated, so
    prefetching without draining only hides time-to-first-byte. And this provider's throughput is
    variable: the same request usually returns faster than realtime but occasionally takes several
    times longer, which starves playback mid-sentence unless a cushion is built first.
    """

    def __init__(self, vstream: openrouter_tts._OpenRouterPcmStream) -> None:
        self.vstream_generation_id = vstream.vstream_generation_id
        self._vstream = vstream
        self._vchunks: list[bytes] = []
        self._vbuffered_bytes = 0
        self._vcomplete = False
        self._varrived = asyncio.Event()
        self._vdrain = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            async for vchunk in self._vstream:
                self._vchunks.append(vchunk)
                self._vbuffered_bytes += len(vchunk)
                self._varrived.set()
        finally:
            self._vcomplete = True
            self._varrived.set()

    @property
    def vbuffered_seconds(self) -> float:
        return voice_contracts.pcm_duration_seconds(self._vbuffered_bytes, voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ)

    async def ready(self, vpreroll_seconds: float) -> None:
        while not self._vcomplete and self.vbuffered_seconds < vpreroll_seconds:
            self._varrived.clear()
            await self._varrived.wait()

    async def __aenter__(self) -> DrainedSegment:
        return self

    async def __aexit__(self, *vargs: object) -> None:
        await self.aclose()

    async def __aiter__(self):
        vindex = 0
        while True:
            while vindex >= len(self._vchunks):
                if self._vcomplete:
                    return
                self._varrived.clear()
                await self._varrived.wait()
            yield self._vchunks[vindex]
            vindex += 1

    async def aclose(self) -> None:
        self._vdrain.cancel()
        await self._vstream.aclose()


class FlexusSynthesizeStream(tts.SynthesizeStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        vtts: FlexusOpenRouterTTS = self._tts
        output_emitter.initialize(
            request_id=utils.shortuuid("OR_"),
            sample_rate=voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
            num_channels=voice_contracts.VOICE_PCM_CHANNELS,
            mime_type="audio/pcm",
            stream=True,
        )

        vtexts: list[str] = []
        vutterances: list[int] = []
        varrived = asyncio.Event()
        vcollected = False
        vprefetcher = voice_tts_prefetch.VoiceTtsPrefetcher(
            vtts.open_segment,
            lambda vstream: vstream.aclose(),
            voice_contracts.VOICE_TTS_PREFETCH_SEGMENTS,
        )

        async def collect() -> None:
            nonlocal vcollected
            vsegmenter = voice_speech_segments.VoiceSpeechStreamSegmenter()
            vutterance = 0
            async for vinput in self._input_ch:
                vflushing = isinstance(vinput, self._FlushSentinel)
                vnew = vsegmenter.flush() if vflushing else vsegmenter.push(vinput)
                if vnew:
                    vtexts.extend(vnew)
                    vutterances.extend([vutterance] * len(vnew))
                    vprefetcher.prefetch(vtexts, True)
                    varrived.set()
                if vflushing:
                    vutterance += 1
            vtail = vsegmenter.flush()
            if vtail:
                vtexts.extend(vtail)
                vutterances.extend([vutterance] * len(vtail))
                vprefetcher.prefetch(vtexts, True)
            vcollected = True
            varrived.set()

        vcollector = asyncio.create_task(collect())
        vopen_utterance: int | None = None
        try:
            vindex = 0
            while True:
                if vindex >= len(vtexts):
                    if vcollected:
                        break
                    varrived.clear()
                    await varrived.wait()
                    continue
                vstream = await vprefetcher.take(vindex, vtexts[vindex])
                await vstream.ready(SEGMENT_PREROLL_SECONDS)
                async with vstream:
                    if vopen_utterance != vutterances[vindex]:
                        if vopen_utterance is not None:
                            output_emitter.end_segment()
                        output_emitter.start_segment(segment_id=vstream.vstream_generation_id)
                        vopen_utterance = vutterances[vindex]
                    async for vchunk in vstream:
                        output_emitter.push(vchunk)
                vindex += 1
                vprefetcher.prefetch(vtexts, True)
            if vopen_utterance is not None:
                output_emitter.end_segment()
            output_emitter.flush()
        finally:
            vcollector.cancel()
            await vprefetcher.discard()
