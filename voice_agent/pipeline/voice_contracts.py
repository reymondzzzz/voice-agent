from __future__ import annotations

import dataclasses
import re
import secrets
import urllib.parse

VOICE_CONTRACT_VERSION = "1.2.0"

LIVEKIT_SERVER_IMAGE = "livekit/livekit-server:v1.13.6"
LIVEKIT_PYTHON_RTC_PIN = "livekit==1.1.15"
LIVEKIT_PYTHON_API_PIN = "livekit-api==1.2.1"
LIVEKIT_PYTHON_AGENTS_PIN = "livekit-agents==1.7.1"
LIVEKIT_BROWSER_CLIENT_PIN = "livekit-client@2.22.1"
LIVEKIT_PYTHON_PINS = (
    LIVEKIT_PYTHON_RTC_PIN,
    LIVEKIT_PYTHON_API_PIN,
    LIVEKIT_PYTHON_AGENTS_PIN,
)

LIVEKIT_MANAGED_HOST = "livekit.cloud"
LIVEKIT_SIGNAL_SCHEMES = ("ws", "wss")
LIVEKIT_LOCAL_DEV_URL = "ws://localhost:7880"

OPENROUTER_AUDIO_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_STT_ENDPOINT = OPENROUTER_AUDIO_BASE_URL + "/audio/transcriptions"
OPENROUTER_TTS_ENDPOINT = OPENROUTER_AUDIO_BASE_URL + "/audio/speech"
OPENROUTER_STT_REQUEST_FIELDS = ("model", "input_audio", "language")
OPENROUTER_STT_INPUT_AUDIO_FORMAT = "wav"
OPENROUTER_STT_PROMPT_VOCABULARY_SUPPORTED = False
OPENROUTER_TTS_REQUEST_FIELDS = ("model", "input", "voice", "response_format", "speed")
OPENROUTER_TTS_RESPONSE_FORMAT = "pcm"

VOICE_DEFAULT_STT_MODEL = "openai/gpt-4o-mini-transcribe"
VOICE_DEFAULT_TTS_MODEL = "fish-audio/s2.1-pro"
VOICE_PROTOTYPE_ECHO_TTS_VOICE = "alloy"

VOICE_PCM_ENCODING = "pcm_s16le"
VOICE_PCM_SAMPLE_WIDTH_BYTES = 2
VOICE_PCM_CHANNELS = 1
VOICE_ROOM_SAMPLE_RATE_HZ = 24000
VOICE_TTS_SAMPLE_RATE_HZ = 24000
VOICE_TTS_ACCEPTED_PROVIDER_SAMPLE_RATES_HZ = (24000, 44100)
VOICE_STT_SAMPLE_RATE_HZ = 16000
VOICE_RTC_FRAME_MS = 50
VOICE_RTC_QUEUE_MS = 200
VOICE_MAX_UTTERANCE_SECONDS = 30.0

VOICE_SEGMENT_MIN_CHARS = 40
VOICE_SEGMENT_MIN_WORDS = 5
VOICE_SEGMENT_MAX_CHARS = 160
VOICE_SEGMENT_MAX_BUFFER_MS = 250
VOICE_TEXT_QUEUE_MAX_ITEMS = 4
VOICE_TEXT_QUEUE_MAX_AGE_S = 30.0
VOICE_TTS_REQUEST_QUEUE_MAX_ITEMS = 1
VOICE_TTS_PREFETCH_SEGMENTS = 2
VOICE_TTS_REQUEST_QUEUE_MAX_AGE_S = 1.0
VOICE_PCM_FRAME_QUEUE_MAX_ITEMS = VOICE_RTC_QUEUE_MS // VOICE_RTC_FRAME_MS
VOICE_PCM_FRAME_QUEUE_MAX_AGE_S = VOICE_RTC_QUEUE_MS / 1000
VOICE_RTC_FRAME_QUEUE_MAX_ITEMS = VOICE_RTC_QUEUE_MS // VOICE_RTC_FRAME_MS
VOICE_RTC_FRAME_QUEUE_MAX_AGE_S = VOICE_RTC_QUEUE_MS / 1000
VOICE_RTC_CAPTURE_DEADLINE_S = VOICE_RTC_QUEUE_MS / 1000

VOICE_CANCEL_REASONS = (
    "barge_in",
    "handoff",
    "session_end",
    "deadline",
    "provider_error",
    "stale_turn",
)
VOICE_ACTOR_INIT_DEADLINE_S = 5.0
VOICE_STT_REQUEST_DEADLINE_S = 15.0
VOICE_TTS_FIRST_BYTE_DEADLINE_S = 10.0
VOICE_TTS_STREAM_DEADLINE_S = 60.0
VOICE_CANCEL_STOP_PUBLISH_DEADLINE_S = 0.2
VOICE_CANCEL_PROVIDER_CLOSE_DEADLINE_S = 1.0

VOICE_AGENT_WORKER_NAME = "flexus-voice-agent"
VOICE_PARTICIPANT_TOKEN_TTL_S = 120
VOICE_JOB_METADATA_TTL_S = 120

VOICE_CORRELATION_ID_FIELDS = (
    "vsession_id",
    "vleg_seq",
    "voice_utterance_id",
    "turn_id",
    "run_id",
    "speech_id",
    "segment_index",
    "conversation_id",
    "agent_id",
    "workspace_id",
    "livekit_room_sid",
    "provider_generation_id",
)

VOICE_WORKER_DISPATCH_NAME = "flexus-voice-agent"
VOICE_WORKER_DEFAULT_MAX_SESSIONS = 4
VOICE_DEFAULT_REDIS_DB = 13

VOICE_ROOM_NAME_PREFIX = "vroom_"
VOICE_PARTICIPANT_IDENTITY_PREFIX = "vpart_"
VOICE_SESSION_ID_PREFIX = "vsess_"
VOICE_OPAQUE_ID_PREFIXES = (VOICE_ROOM_NAME_PREFIX, VOICE_PARTICIPANT_IDENTITY_PREFIX, VOICE_SESSION_ID_PREFIX)
VOICE_OPAQUE_ID_HEX_CHARS = 32
_OPAQUE_ID_RE = re.compile("^(%s)[0-9a-f]{%d}$" % ("|".join(VOICE_OPAQUE_ID_PREFIXES), VOICE_OPAQUE_ID_HEX_CHARS))


@dataclasses.dataclass(frozen=True)
class VoiceEnvVar:
    vvar_name: str
    vvar_required: bool
    vvar_purpose: str


VOICE_ENV_VARS = (
    VoiceEnvVar("FLEXUS_VOICE_ENABLED", False, "prototype gate; voice session creation stays refused unless this is 1"),
    VoiceEnvVar("FLEXUS_VOICE_LIVEKIT_URL", True, "self-hosted livekit signal url, ws or wss, never a managed livekit host"),
    VoiceEnvVar("FLEXUS_VOICE_LIVEKIT_API_KEY", True, "livekit api key used to mint short-lived room-scoped access tokens"),
    VoiceEnvVar("FLEXUS_VOICE_LIVEKIT_API_SECRET", True, "livekit api secret, server side only, never sent to a client"),
    VoiceEnvVar("FLEXUS_VOICE_REDIS_DB", False, "redis database index isolating prototype voice state from application state"),
    VoiceEnvVar("FLEXUS_VOICE_STT_MODEL", False, "openrouter transcription model id, defaults to VOICE_DEFAULT_STT_MODEL"),
    VoiceEnvVar("FLEXUS_VOICE_WORKER_MAX_SESSIONS", False, "per-replica voice session slots, defaults to VOICE_WORKER_DEFAULT_MAX_SESSIONS until VA-106 measures the safe value"),
    VoiceEnvVar("OPENROUTER_API_KEY", True, "shared openrouter credential used by voice stt and tts"),
)


def require_self_hosted_livekit_url(vlk_url: str) -> str:
    parsed = urllib.parse.urlparse(vlk_url)
    if parsed.scheme not in LIVEKIT_SIGNAL_SCHEMES:
        raise ValueError("livekit url scheme must be one of %s, got %r" % (LIVEKIT_SIGNAL_SCHEMES, vlk_url))
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise ValueError("livekit url has no host: %r" % (vlk_url,))
    if host == LIVEKIT_MANAGED_HOST or host.endswith("." + LIVEKIT_MANAGED_HOST):
        raise ValueError("managed livekit host %s is out of scope; voice media stays on flexus-controlled infrastructure" % (host,))
    return vlk_url


def pcm_frame_bytes(vpcm_sample_rate_hz: int, vpcm_frame_ms: int) -> int:
    if vpcm_sample_rate_hz <= 0 or vpcm_frame_ms <= 0:
        raise ValueError("pcm frame needs a positive sample rate and duration, got %r Hz %r ms" % (vpcm_sample_rate_hz, vpcm_frame_ms))
    samples = vpcm_sample_rate_hz * vpcm_frame_ms
    if samples % 1000:
        raise ValueError("%d Hz does not divide into whole samples over %d ms" % (vpcm_sample_rate_hz, vpcm_frame_ms))
    return samples // 1000 * VOICE_PCM_SAMPLE_WIDTH_BYTES * VOICE_PCM_CHANNELS


def pcm_duration_seconds(vpcm_byte_count: int, vpcm_sample_rate_hz: int) -> float:
    if vpcm_sample_rate_hz <= 0:
        raise ValueError("pcm duration needs a positive sample rate, got %r" % (vpcm_sample_rate_hz,))
    if vpcm_byte_count < 0:
        raise ValueError("pcm duration needs a non-negative byte count, got %r" % (vpcm_byte_count,))
    return vpcm_byte_count / (vpcm_sample_rate_hz * VOICE_PCM_SAMPLE_WIDTH_BYTES * VOICE_PCM_CHANNELS)


def new_opaque_voice_id(vid_prefix: str) -> str:
    if vid_prefix not in VOICE_OPAQUE_ID_PREFIXES:
        raise ValueError("opaque voice ids use one of %s, got %r" % (VOICE_OPAQUE_ID_PREFIXES, vid_prefix))
    return vid_prefix + secrets.token_hex(VOICE_OPAQUE_ID_HEX_CHARS // 2)


def is_opaque_voice_id(vid_value: str) -> bool:
    return bool(_OPAQUE_ID_RE.match(vid_value))


def is_opaque_voice_id_of(vid_value: str, vid_prefix: str) -> bool:
    if vid_prefix not in VOICE_OPAQUE_ID_PREFIXES:
        raise ValueError("opaque voice ids use one of %s, got %r" % (VOICE_OPAQUE_ID_PREFIXES, vid_prefix))
    return vid_value.startswith(vid_prefix) and is_opaque_voice_id(vid_value)
