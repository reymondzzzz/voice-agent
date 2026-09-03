from __future__ import annotations

import base64
import dataclasses
import io
import logging
import time
import wave
from collections.abc import AsyncIterator

import httpx

from voice_agent.pipeline import openrouter_shared
from voice_agent.pipeline import voice_contracts

logger = logging.getLogger(__name__)

STT_EVENT_KINDS = ("partial", "final")
STT_ERROR_KINDS = (
    "invalid_audio",
    "auth",
    "rate_limit",
    "invalid_request",
    "provider_error",
    "transport",
    "timeout",
    "malformed_response",
)
STT_GENERATION_ID_HEADER = "x-generation-id"
USAGE_VALUE_MAX = 1e9
_PROVIDER_ERROR_EXCERPT_CHARS = 200
_USAGE_VALUE_EXCERPT_CHARS = 40


class SttError(Exception):
    def __init__(self, sterr_kind: str, sterr_message: str, sterr_status_code: int | None) -> None:
        if sterr_kind not in STT_ERROR_KINDS:
            raise ValueError("stt error kind must be one of %s, got %r" % (STT_ERROR_KINDS, sterr_kind))
        super().__init__(sterr_message)
        self.sterr_kind = sterr_kind
        self.sterr_status_code = sterr_status_code


@dataclasses.dataclass(frozen=True)
class SttUsage:
    sttu_audio_seconds: float | None
    sttu_input_tokens: int | None
    sttu_output_tokens: int | None
    sttu_total_tokens: int | None
    sttu_cost_usd: float | None


@dataclasses.dataclass(frozen=True)
class SttConfig:
    sttc_model: str
    sttc_language: str | None
    sttc_sample_rate_hz: int
    sttc_deadline_s: float


@dataclasses.dataclass(frozen=True)
class TranscriptEvent:
    stte_kind: str
    stte_text: str
    stte_model: str
    stte_language: str | None
    stte_audio_seconds: float
    stte_request_ms: float
    stte_usage: SttUsage
    stte_provider_generation_id: str | None


def max_utterance_bytes(stt_sample_rate_hz: int) -> int:
    if stt_sample_rate_hz <= 0:
        raise ValueError("utterance bound needs a positive sample rate, got %r" % (stt_sample_rate_hz,))
    samples = voice_contracts.VOICE_MAX_UTTERANCE_SECONDS * stt_sample_rate_hz
    return int(samples) * voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES * voice_contracts.VOICE_PCM_CHANNELS


def pcm16_to_wav(stt_pcm_bytes: bytes, stt_sample_rate_hz: int) -> bytes:
    if stt_sample_rate_hz <= 0:
        raise ValueError("wav wrapper needs a positive sample rate, got %r" % (stt_sample_rate_hz,))
    frame_bytes = voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES * voice_contracts.VOICE_PCM_CHANNELS
    if len(stt_pcm_bytes) % frame_bytes:
        raise ValueError("pcm payload of %d bytes is not a whole number of %d-byte frames" % (len(stt_pcm_bytes), frame_bytes))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_writer:
        wav_writer.setnchannels(voice_contracts.VOICE_PCM_CHANNELS)
        wav_writer.setsampwidth(voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES)
        wav_writer.setframerate(stt_sample_rate_hz)
        wav_writer.writeframes(stt_pcm_bytes)
    return buffer.getvalue()


def _error_kind_for_status(stt_status_code: int) -> str:
    if stt_status_code in (401, 403):
        return "auth"
    if stt_status_code == 429:
        return "rate_limit"
    if stt_status_code >= 500:
        return "provider_error"
    return "invalid_request"


def _usage_number(stt_usage_json: dict, stt_field: str, stt_status_code: int | None) -> float | None:
    value = stt_usage_json.get(stt_field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SttError("malformed_response", "openrouter stt usage.%s is %s, not a number" % (stt_field, type(value).__name__), stt_status_code)
    if not 0.0 <= value <= USAGE_VALUE_MAX:
        raise SttError("malformed_response", "openrouter stt usage.%s is %s, not a number in [0, %g]" % (stt_field, repr(value)[:_USAGE_VALUE_EXCERPT_CHARS], USAGE_VALUE_MAX), stt_status_code)
    return float(value)


def _usage_token_count(stt_usage_json: dict, stt_field: str, stt_status_code: int | None) -> int | None:
    counted = _usage_number(stt_usage_json, stt_field, stt_status_code)
    if counted is None:
        return None
    if counted != int(counted):
        raise SttError("malformed_response", "openrouter stt usage.%s is %r, not a whole number of tokens" % (stt_field, counted), stt_status_code)
    return int(counted)


def _usage_from_response(stt_usage_json: dict, stt_status_code: int | None) -> SttUsage:
    return SttUsage(
        sttu_audio_seconds=_usage_number(stt_usage_json, "seconds", stt_status_code),
        sttu_input_tokens=_usage_token_count(stt_usage_json, "input_tokens", stt_status_code),
        sttu_output_tokens=_usage_token_count(stt_usage_json, "output_tokens", stt_status_code),
        sttu_total_tokens=_usage_token_count(stt_usage_json, "total_tokens", stt_status_code),
        sttu_cost_usd=_usage_number(stt_usage_json, "cost", stt_status_code),
    )


def _validated_payload(stt_response: httpx.Response) -> tuple[str, SttUsage]:
    try:
        payload = stt_response.json()
    except ValueError as exc:
        raise SttError("malformed_response", "openrouter stt returned a non-json body", stt_response.status_code) from exc
    if not isinstance(payload, dict) or "text" not in payload:
        raise SttError("malformed_response", "openrouter stt response carries no text field", stt_response.status_code)
    stt_text = payload["text"]
    if stt_text is not None and not isinstance(stt_text, str):
        raise SttError("malformed_response", "openrouter stt response text is %s, not a string" % type(stt_text).__name__, stt_response.status_code)
    stt_usage_json = payload.get("usage")
    if stt_usage_json is not None and not isinstance(stt_usage_json, dict):
        raise SttError("malformed_response", "openrouter stt response usage is %s, not an object" % type(stt_usage_json).__name__, stt_response.status_code)
    return (stt_text or "").strip(), _usage_from_response(stt_usage_json or {}, stt_response.status_code)


def _transcription_request_body(stt_audio_pcm: bytes, stt_config: SttConfig) -> dict:
    wav_bytes = pcm16_to_wav(stt_audio_pcm, stt_config.sttc_sample_rate_hz)
    body = {
        "model": stt_config.sttc_model,
        "input_audio": {
            "data": base64.b64encode(wav_bytes).decode("ascii"),
            "format": voice_contracts.OPENROUTER_STT_INPUT_AUDIO_FORMAT,
        },
    }
    if stt_config.sttc_language:
        body["language"] = stt_config.sttc_language
    return body


class OpenRouterSttProvider:
    def __init__(self, sttp_api_key: str) -> None:
        if not sttp_api_key:
            raise ValueError("openrouter stt provider needs an api key")
        self._sttp_api_key = sttp_api_key
        self._sttp_client = httpx.AsyncClient()

    async def aclose(self) -> None:
        await self._sttp_client.aclose()

    async def stream(self, stt_audio_pcm: bytes, stt_config: SttConfig) -> AsyncIterator[TranscriptEvent]:
        yield await self.transcribe_utterance(stt_audio_pcm, stt_config)

    async def transcribe_utterance(self, stt_audio_pcm: bytes, stt_config: SttConfig) -> TranscriptEvent:
        stt_audio_seconds = self._validated_audio_seconds(stt_audio_pcm, stt_config)
        body = _transcription_request_body(stt_audio_pcm, stt_config)
        started_at = time.monotonic()
        try:
            response = await self._sttp_client.post(
                voice_contracts.OPENROUTER_STT_ENDPOINT,
                headers=openrouter_shared.openrouter_request_headers(self._sttp_api_key),
                json=body,
                timeout=stt_config.sttc_deadline_s,
            )
        except httpx.TimeoutException as exc:
            raise SttError("timeout", "openrouter stt exceeded its %.1fs deadline" % stt_config.sttc_deadline_s, None) from exc
        except httpx.TransportError as exc:
            raise SttError("transport", "openrouter stt transport failed: %s" % type(exc).__name__, None) from exc
        stt_request_ms = (time.monotonic() - started_at) * 1000.0
        stt_generation_id = response.headers.get(STT_GENERATION_ID_HEADER)
        if response.status_code >= 400:
            kind = _error_kind_for_status(response.status_code)
            logger.warning(
                "voice stt failed kind=%s status=%d model=%s generation=%s audio_seconds=%.2f",
                kind,
                response.status_code,
                stt_config.sttc_model,
                stt_generation_id,
                stt_audio_seconds,
            )
            raise SttError(kind, "openrouter stt returned %d: %s" % (response.status_code, response.text[:_PROVIDER_ERROR_EXCERPT_CHARS]), response.status_code)
        stt_text, stt_usage = _validated_payload(response)
        event = TranscriptEvent(
            stte_kind="final",
            stte_text=stt_text,
            stte_model=stt_config.sttc_model,
            stte_language=stt_config.sttc_language,
            stte_audio_seconds=stt_audio_seconds,
            stte_request_ms=stt_request_ms,
            stte_usage=stt_usage,
            stte_provider_generation_id=stt_generation_id,
        )
        logger.info(
            "voice stt final model=%s generation=%s audio_seconds=%.2f request_ms=%.0f text_chars=%d cost_usd=%s",
            event.stte_model,
            event.stte_provider_generation_id,
            event.stte_audio_seconds,
            event.stte_request_ms,
            len(event.stte_text),
            event.stte_usage.sttu_cost_usd,
        )
        return event

    def _validated_audio_seconds(self, stt_audio_pcm: bytes, stt_config: SttConfig) -> float:
        limit_bytes = max_utterance_bytes(stt_config.sttc_sample_rate_hz)
        if len(stt_audio_pcm) > limit_bytes:
            raise SttError("invalid_audio", "utterance is %d bytes, over the %d byte contract bound" % (len(stt_audio_pcm), limit_bytes), None)
        frame_bytes = voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES * voice_contracts.VOICE_PCM_CHANNELS
        if len(stt_audio_pcm) % frame_bytes:
            raise SttError("invalid_audio", "utterance is %d bytes, not a whole number of %d byte pcm frames" % (len(stt_audio_pcm), frame_bytes), None)
        stt_audio_seconds = voice_contracts.pcm_duration_seconds(len(stt_audio_pcm), stt_config.sttc_sample_rate_hz)
        if stt_audio_seconds <= 0.0:
            raise SttError("invalid_audio", "utterance carries no audio", None)
        return stt_audio_seconds
