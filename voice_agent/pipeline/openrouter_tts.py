from __future__ import annotations

import asyncio
import dataclasses
import time
from contextlib import AbstractAsyncContextManager
from typing import cast

import httpx

from voice_agent.pipeline import openrouter_shared
from voice_agent.pipeline import voice_contracts
from voice_agent.pipeline import voice_pcm_resample

TTS_PCM_CONTENT_TYPE = "audio/pcm"
TTS_GENERATION_ENDPOINT = voice_contracts.OPENROUTER_AUDIO_BASE_URL + "/generation"
TTS_MAX_INPUT_CHARS = voice_contracts.VOICE_SEGMENT_MAX_CHARS
TTS_MIN_SPEED = 0.25
TTS_MAX_SPEED = 4.0
TTS_MAX_PCM_CHUNK_BYTES = voice_contracts.pcm_frame_bytes(
    voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
    voice_contracts.VOICE_RTC_QUEUE_MS,
)
TTS_MAX_PENDING_PCM_BYTES = TTS_MAX_PCM_CHUNK_BYTES * 5
TTS_MAX_TOTAL_PCM_BYTES = voice_contracts.pcm_frame_bytes(
    voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
    int(voice_contracts.VOICE_TTS_STREAM_DEADLINE_S * 1000),
)
_TTS_STREAM_END = object()


class VoiceTtsError(RuntimeError):
    def __init__(
        self,
        verror_code: str,
        *,
        verror_status_code: int | None = None,
        verror_retryable: bool = False,
    ) -> None:
        super().__init__(verror_code)
        self.verror_code = verror_code
        self.verror_status_code = verror_status_code
        self.verror_retryable = verror_retryable


@dataclasses.dataclass(frozen=True)
class VoiceTtsRequest:
    vtts_input: str
    vtts_voice: str
    vtts_model: str = voice_contracts.VOICE_DEFAULT_TTS_MODEL
    vtts_speed: float = 1.0


@dataclasses.dataclass(frozen=True)
class VoiceTtsUsage:
    vusage_generation_id: str
    vusage_provider_name: str | None
    vusage_total_cost_usd: float | None
    vusage_cancelled: bool | None
    vusage_native_input_units: int | None


@dataclasses.dataclass(frozen=True)
class _VoicePcmLayout:
    vpcm_content_type: str
    vpcm_sample_rate_hz: int
    vpcm_source_sample_rate_hz: int
    vpcm_channels: int
    vpcm_sample_width_bytes: int
    vpcm_encoding: str
    vpcm_declared_fields: tuple[str, ...]
    vpcm_validation_source: str


def _validated_request(vtts_request: VoiceTtsRequest) -> VoiceTtsRequest:
    if not vtts_request.vtts_input.strip():
        raise ValueError("tts input must not be empty")
    if len(vtts_request.vtts_input) > TTS_MAX_INPUT_CHARS:
        raise ValueError("tts input exceeds %d characters" % TTS_MAX_INPUT_CHARS)
    if not vtts_request.vtts_voice.strip():
        raise ValueError("tts voice must not be empty")
    if not vtts_request.vtts_model.strip():
        raise ValueError("tts model must not be empty")
    if not TTS_MIN_SPEED <= vtts_request.vtts_speed <= TTS_MAX_SPEED:
        raise ValueError("tts speed must be between %.2f and %.2f" % (TTS_MIN_SPEED, TTS_MAX_SPEED))
    return vtts_request


def _request_body(vtts_request: VoiceTtsRequest) -> dict[str, object]:
    return {
        "model": vtts_request.vtts_model,
        "input": vtts_request.vtts_input,
        "voice": vtts_request.vtts_voice,
        "response_format": voice_contracts.OPENROUTER_TTS_RESPONSE_FORMAT,
        "speed": vtts_request.vtts_speed,
    }


def _status_error(vstatus_code: int) -> VoiceTtsError:
    vretryable = vstatus_code in (408, 409, 425, 429) or vstatus_code >= 500
    return VoiceTtsError(
        "openrouter_tts_http_%d" % vstatus_code,
        verror_status_code=vstatus_code,
        verror_retryable=vretryable,
    )


def _content_type_parts(vcontent_type: str) -> tuple[str, dict[str, str]]:
    vparts = [vpart.strip() for vpart in vcontent_type.split(";")]
    vmedia_type = vparts[0].lower()
    vparameters: dict[str, str] = {}
    for vpart in vparts[1:]:
        vname, vseparator, vvalue = vpart.partition("=")
        vname = vname.strip().lower()
        if not vseparator or not vname or vname in vparameters:
            raise VoiceTtsError("openrouter_tts_invalid_response_metadata")
        vparameters[vname] = vvalue.strip().strip('"').lower()
    return vmedia_type, vparameters


def _reject_parameter_mismatch(
    vparameters: dict[str, str],
    vnames: tuple[str, ...],
    vexpected_values: tuple[str, ...],
) -> bool:
    vdeclared = False
    for vname in vnames:
        if vname not in vparameters:
            continue
        vdeclared = True
        if vparameters[vname] not in vexpected_values:
            raise VoiceTtsError("openrouter_tts_pcm_layout_mismatch")
    return vdeclared


def _declared_source_sample_rate_hz(vparameters: dict[str, str]) -> int:
    for vname in ("rate", "sample-rate", "sample_rate", "samplerate"):
        if vname in vparameters:
            return int(vparameters[vname])
    return voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ


def _validated_pcm_layout(vcontent_type: str) -> _VoicePcmLayout:
    vmedia_type, vparameters = _content_type_parts(vcontent_type)
    if vmedia_type != TTS_PCM_CONTENT_TYPE:
        raise VoiceTtsError("openrouter_tts_invalid_response_metadata")
    vdeclared_fields = []
    vchecks = (
        ("sample_rate_hz", ("rate", "sample-rate", "sample_rate", "samplerate"), tuple(str(vrate) for vrate in voice_contracts.VOICE_TTS_ACCEPTED_PROVIDER_SAMPLE_RATES_HZ)),
        ("channels", ("channels", "channel-count", "channel_count"), (str(voice_contracts.VOICE_PCM_CHANNELS),)),
        ("sample_width_bits", ("bits", "bit-depth", "bit_depth", "bits-per-sample"), (str(voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES * 8),)),
        ("encoding", ("encoding", "codec"), ("pcm_s16le", "s16le", "linear16")),
        ("byte_order", ("endianness", "byte-order", "byte_order"), ("little", "little-endian", "le")),
        ("signed", ("signed",), ("1", "true", "signed")),
    )
    for vfield, vnames, vexpected_values in vchecks:
        if _reject_parameter_mismatch(vparameters, vnames, vexpected_values):
            vdeclared_fields.append(vfield)
    return _VoicePcmLayout(
        vpcm_content_type=vmedia_type,
        vpcm_sample_rate_hz=voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ,
        vpcm_source_sample_rate_hz=_declared_source_sample_rate_hz(vparameters),
        vpcm_channels=voice_contracts.VOICE_PCM_CHANNELS,
        vpcm_sample_width_bytes=voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES,
        vpcm_encoding=voice_contracts.VOICE_PCM_ENCODING,
        vpcm_declared_fields=tuple(vdeclared_fields),
        vpcm_validation_source="openai_audio_speech_pcm_contract",
    )


class _OpenRouterPcmStream:
    def __init__(
        self,
        *,
        vstream_response: httpx.Response,
        vstream_context: AbstractAsyncContextManager[httpx.Response],
        vstream_client: httpx.AsyncClient,
        vstream_owns_client: bool,
        vstream_started_at: float,
        vstream_pcm_layout: _VoicePcmLayout,
    ) -> None:
        self.vstream_generation_id = vstream_response.headers["X-Generation-Id"]
        self.vstream_content_type = vstream_pcm_layout.vpcm_content_type
        self.vstream_sample_rate_hz = vstream_pcm_layout.vpcm_sample_rate_hz
        self.vstream_channels = vstream_pcm_layout.vpcm_channels
        self.vstream_sample_width_bytes = vstream_pcm_layout.vpcm_sample_width_bytes
        self.vstream_encoding = vstream_pcm_layout.vpcm_encoding
        self.vstream_declared_layout_fields = vstream_pcm_layout.vpcm_declared_fields
        self.vstream_layout_validation_source = vstream_pcm_layout.vpcm_validation_source
        self.vstream_total_bytes = 0
        self.vstream_chunk_count = 0
        self.vstream_cancel_reason: str | None = None
        self.vstream_completed = False
        self.vstream_closed = False
        self._vresponse = vstream_response
        self._vcontext = vstream_context
        self._vclient = vstream_client
        self._vowns_client = vstream_owns_client
        self._vstarted_at = vstream_started_at
        self._viterator = vstream_response.aiter_raw()
        self._vpending = bytearray()
        self._vresampler: voice_pcm_resample.PcmResampler | None = voice_pcm_resample.resampler_for(vstream_pcm_layout.vpcm_source_sample_rate_hz)
        self._vclose_lock = asyncio.Lock()

    @property
    def vstream_audio_duration_seconds(self) -> float:
        return voice_contracts.pcm_duration_seconds(
            self.vstream_total_bytes,
            self.vstream_sample_rate_hz,
        )

    def __aiter__(self) -> _OpenRouterPcmStream:
        return self

    async def __anext__(self) -> bytes:
        if self.vstream_closed:
            raise StopAsyncIteration
        while True:
            vpcm = self._take_pending_pcm()
            if vpcm is not None:
                return vpcm
            vtimeout_s = self._next_timeout_seconds()
            try:
                async with asyncio.timeout(vtimeout_s):
                    vchunk = await anext(self._viterator, _TTS_STREAM_END)
            except TimeoutError as exc:
                await self.cancel("deadline")
                raise VoiceTtsError("openrouter_tts_stream_deadline", verror_retryable=True) from exc
            except (httpx.ReadError, httpx.StreamClosed) as exc:
                if self.vstream_cancel_reason is not None:
                    raise StopAsyncIteration from exc
                await self.aclose()
                raise VoiceTtsError("openrouter_tts_stream_read", verror_retryable=True) from exc
            if vchunk is _TTS_STREAM_END:
                return await self._finish_iteration()
            await self._append_pending_pcm(cast(bytes, vchunk))

    async def _append_pending_pcm(self, vchunk: bytes) -> None:
        if self._vresampler is not None:
            vchunk = self._vresampler.process(vchunk)
            if not vchunk:
                return
        vtotal_pending_bytes = self.vstream_total_bytes + len(self._vpending) + len(vchunk)
        if vtotal_pending_bytes > TTS_MAX_TOTAL_PCM_BYTES:
            await self.aclose()
            raise VoiceTtsError("openrouter_tts_pcm_total_limit")
        if len(self._vpending) + len(vchunk) > TTS_MAX_PENDING_PCM_BYTES:
            await self.aclose()
            raise VoiceTtsError("openrouter_tts_pcm_pending_limit")
        self._vpending.extend(vchunk)

    def _take_pending_pcm(self) -> bytes | None:
        vusable_bytes = len(self._vpending) // self.vstream_sample_width_bytes * self.vstream_sample_width_bytes
        vbounded_bytes = min(vusable_bytes, TTS_MAX_PCM_CHUNK_BYTES)
        if not vbounded_bytes:
            return None
        vpcm = bytes(self._vpending[:vbounded_bytes])
        del self._vpending[:vbounded_bytes]
        self.vstream_total_bytes += len(vpcm)
        self.vstream_chunk_count += 1
        return vpcm

    def _next_timeout_seconds(self) -> float:
        velapsed_s = time.perf_counter() - self._vstarted_at
        vstream_remaining_s = voice_contracts.VOICE_TTS_STREAM_DEADLINE_S - velapsed_s
        if self.vstream_chunk_count == 0:
            vfirst_remaining_s = voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S - velapsed_s
            return max(0.0, min(vstream_remaining_s, vfirst_remaining_s))
        return max(0.0, vstream_remaining_s)

    async def _finish_iteration(self) -> bytes:
        if self._vresampler is not None:
            self._vpending.extend(self._vresampler.flush())
            self._vresampler = None
            vpcm = self._take_pending_pcm()
            if vpcm is not None:
                return vpcm
        if self._vpending:
            await self.aclose()
            raise VoiceTtsError("openrouter_tts_unaligned_pcm")
        if self.vstream_total_bytes == 0:
            await self.aclose()
            raise VoiceTtsError("openrouter_tts_empty_pcm", verror_retryable=True)
        self.vstream_completed = True
        await self.aclose()
        raise StopAsyncIteration

    async def cancel(self, vcancel_reason: str) -> None:
        if vcancel_reason not in voice_contracts.VOICE_CANCEL_REASONS:
            raise ValueError("unknown voice cancellation reason %r" % vcancel_reason)
        self.vstream_cancel_reason = vcancel_reason
        try:
            async with asyncio.timeout(voice_contracts.VOICE_CANCEL_PROVIDER_CLOSE_DEADLINE_S):
                await self.aclose()
        except TimeoutError as exc:
            raise VoiceTtsError("openrouter_tts_cancel_close_deadline", verror_retryable=True) from exc

    async def aclose(self) -> None:
        async with self._vclose_lock:
            if self.vstream_closed:
                return
            self.vstream_closed = True
            self._vpending.clear()
            await self._vresponse.aclose()
            await self._vcontext.__aexit__(None, None, None)
            if self._vowns_client:
                await self._vclient.aclose()

    async def __aenter__(self) -> _OpenRouterPcmStream:
        return self

    async def __aexit__(self, vexc_type: object, vexc: object, vtraceback: object) -> None:
        await self.aclose()


async def open_openrouter_pcm_stream(
    vtts_request: VoiceTtsRequest,
    *,
    vtts_api_key: str,
    vtts_client: httpx.AsyncClient | None = None,
) -> _OpenRouterPcmStream:
    _validated_request(vtts_request)
    if not vtts_api_key:
        raise ValueError("openrouter api key must not be empty")
    vstarted_at = time.perf_counter()
    vowns_client = vtts_client is None
    vclient = vtts_client or httpx.AsyncClient()
    vcontext = vclient.stream(
        "POST",
        voice_contracts.OPENROUTER_TTS_ENDPOINT,
        headers=openrouter_shared.openrouter_request_headers(vtts_api_key),
        json=_request_body(vtts_request),
        timeout=httpx.Timeout(
            voice_contracts.VOICE_TTS_STREAM_DEADLINE_S,
            read=voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S,
        ),
    )
    try:
        vremaining_s = voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S - (time.perf_counter() - vstarted_at)
        async with asyncio.timeout(max(0.0, vremaining_s)):
            vresponse = await vcontext.__aenter__()
    except TimeoutError as exc:
        if vowns_client:
            await vclient.aclose()
        raise VoiceTtsError("openrouter_tts_first_byte_deadline", verror_retryable=True) from exc
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        if vowns_client:
            await vclient.aclose()
        raise VoiceTtsError("openrouter_tts_connect", verror_retryable=True) from exc
    if vresponse.status_code >= 400:
        await vresponse.aclose()
        await vcontext.__aexit__(None, None, None)
        if vowns_client:
            await vclient.aclose()
        raise _status_error(vresponse.status_code)
    try:
        vpcm_layout = _validated_pcm_layout(vresponse.headers.get("Content-Type", ""))
    except VoiceTtsError:
        await vresponse.aclose()
        await vcontext.__aexit__(None, None, None)
        if vowns_client:
            await vclient.aclose()
        raise
    if not vresponse.headers.get("X-Generation-Id"):
        await vresponse.aclose()
        await vcontext.__aexit__(None, None, None)
        if vowns_client:
            await vclient.aclose()
        raise VoiceTtsError("openrouter_tts_invalid_response_metadata")
    return _OpenRouterPcmStream(
        vstream_response=vresponse,
        vstream_context=vcontext,
        vstream_client=vclient,
        vstream_owns_client=vowns_client,
        vstream_started_at=vstarted_at,
        vstream_pcm_layout=vpcm_layout,
    )


async def fetch_openrouter_tts_usage(
    vusage_generation_id: str,
    *,
    vusage_api_key: str,
    vusage_client: httpx.AsyncClient | None = None,
) -> VoiceTtsUsage:
    if not vusage_generation_id:
        raise ValueError("generation id must not be empty")
    vowns_client = vusage_client is None
    vclient = vusage_client or httpx.AsyncClient()
    try:
        vresponse = await vclient.get(
            TTS_GENERATION_ENDPOINT,
            headers=openrouter_shared.openrouter_request_headers(vusage_api_key),
            params={"id": vusage_generation_id},
            timeout=voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S,
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        raise VoiceTtsError("openrouter_tts_usage_connect", verror_retryable=True) from exc
    finally:
        if vowns_client:
            await vclient.aclose()
    if vresponse.status_code >= 400:
        raise _status_error(vresponse.status_code)
    vdata = vresponse.json()["data"]
    vcost = vdata.get("total_cost")
    vnative_input = vdata.get("native_tokens_prompt")
    return VoiceTtsUsage(
        vusage_generation_id=vusage_generation_id,
        vusage_provider_name=vdata.get("provider_name"),
        vusage_total_cost_usd=float(vcost) if vcost is not None else None,
        vusage_cancelled=vdata.get("cancelled"),
        vusage_native_input_units=int(vnative_input) if vnative_input is not None else None,
    )
