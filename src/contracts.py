from __future__ import annotations

import io
import urllib.parse
import wave


VOICE_CONTRACT_VERSION = "1.2.0"

LIVEKIT_MANAGED_HOST = "livekit.cloud"
LIVEKIT_SIGNAL_SCHEMES = ("ws", "wss")
LIVEKIT_BROWSER_CLIENT_PIN = "livekit-client@2.22.1"

OPENROUTER_AUDIO_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_STT_ENDPOINT = OPENROUTER_AUDIO_BASE_URL + "/audio/transcriptions"
OPENROUTER_TTS_ENDPOINT = OPENROUTER_AUDIO_BASE_URL + "/audio/speech"
OPENROUTER_STT_INPUT_AUDIO_FORMAT = "wav"
OPENROUTER_TTS_RESPONSE_FORMAT = "pcm"

VOICE_DEFAULT_LLM_MODEL = "google/gemini-2.5-flash"
VOICE_DEFAULT_STT_MODEL = "openai/gpt-4o-mini-transcribe"
VOICE_DEFAULT_TTS_MODEL = "fish-audio/s2.1-pro"

VOICE_PCM_SAMPLE_WIDTH_BYTES = 2
VOICE_PCM_CHANNELS = 1
VOICE_ROOM_SAMPLE_RATE_HZ = 24000
VOICE_TTS_SAMPLE_RATE_HZ = 24000
VOICE_TTS_ACCEPTED_PROVIDER_SAMPLE_RATES_HZ = (24000, 44100)
VOICE_STT_SAMPLE_RATE_HZ = 16000
VOICE_MAX_UTTERANCE_SECONDS = 30.0

VOICE_STT_REQUEST_DEADLINE_S = 15.0
VOICE_TTS_FIRST_BYTE_DEADLINE_S = 10.0
VOICE_TTS_STREAM_DEADLINE_S = 60.0


def require_self_hosted_livekit_url(vlk_url: str) -> str:
    vparsed = urllib.parse.urlparse(vlk_url)
    if vparsed.scheme not in LIVEKIT_SIGNAL_SCHEMES:
        raise ValueError(f"livekit url scheme must be one of {LIVEKIT_SIGNAL_SCHEMES}, got {vlk_url!r}")
    vhost = (vparsed.hostname or "").lower().rstrip(".")
    if not vhost:
        raise ValueError(f"livekit url has no host: {vlk_url!r}")
    if vhost == LIVEKIT_MANAGED_HOST or vhost.endswith("." + LIVEKIT_MANAGED_HOST):
        raise ValueError(f"managed livekit host {vhost} is out of scope; voice media stays on self-hosted infrastructure")
    return vlk_url


def require_accepted_tts_sample_rate(vsample_rate_hz: int) -> int:
    if vsample_rate_hz not in VOICE_TTS_ACCEPTED_PROVIDER_SAMPLE_RATES_HZ:
        raise ValueError(
            f"tts provider returned {vsample_rate_hz} Hz, expected one of {VOICE_TTS_ACCEPTED_PROVIDER_SAMPLE_RATES_HZ}"
        )
    return vsample_rate_hz


def sample_rate_from_content_type(vcontent_type: str) -> int:
    for vparameter in vcontent_type.split(";")[1:]:
        vname, _, vvalue = vparameter.strip().partition("=")
        if vname.lower() == "rate" and vvalue.strip().isdigit():
            return int(vvalue.strip())
    return VOICE_TTS_SAMPLE_RATE_HZ


def pcm16_to_wav(vpcm_bytes: bytes, vsample_rate_hz: int) -> bytes:
    if vsample_rate_hz <= 0:
        raise ValueError(f"wav wrapper needs a positive sample rate, got {vsample_rate_hz!r}")
    vframe_bytes = VOICE_PCM_SAMPLE_WIDTH_BYTES * VOICE_PCM_CHANNELS
    if len(vpcm_bytes) % vframe_bytes:
        raise ValueError(f"pcm payload of {len(vpcm_bytes)} bytes is not a whole number of {vframe_bytes}-byte frames")
    vbuffer = io.BytesIO()
    with wave.open(vbuffer, "wb") as vwriter:
        vwriter.setnchannels(VOICE_PCM_CHANNELS)
        vwriter.setsampwidth(VOICE_PCM_SAMPLE_WIDTH_BYTES)
        vwriter.setframerate(vsample_rate_hz)
        vwriter.writeframes(vpcm_bytes)
    return vbuffer.getvalue()


def pcm_duration_seconds(vpcm_byte_count: int, vsample_rate_hz: int) -> float:
    if vsample_rate_hz <= 0:
        raise ValueError(f"pcm duration needs a positive sample rate, got {vsample_rate_hz!r}")
    if vpcm_byte_count < 0:
        raise ValueError(f"pcm duration needs a non-negative byte count, got {vpcm_byte_count!r}")
    return vpcm_byte_count / (vsample_rate_hz * VOICE_PCM_SAMPLE_WIDTH_BYTES * VOICE_PCM_CHANNELS)


def max_utterance_bytes(vsample_rate_hz: int) -> int:
    if vsample_rate_hz <= 0:
        raise ValueError(f"utterance bound needs a positive sample rate, got {vsample_rate_hz!r}")
    vsamples = int(VOICE_MAX_UTTERANCE_SECONDS * vsample_rate_hz)
    return vsamples * VOICE_PCM_SAMPLE_WIDTH_BYTES * VOICE_PCM_CHANNELS
