import pathlib
import re

import pytest

from flexus_backend.services import workspace_stt
from voice_agent.pipeline import voice_contracts

REPO_ROOT = pathlib.Path(voice_contracts.__file__).resolve().parents[2]
ROOM_IO_VALUES_WERE_READ_FROM = "livekit-agents==1.7.1"


def test_contract_carries_a_semver_version():
    assert re.fullmatch(r"\d+\.\d+\.\d+", voice_contracts.VOICE_CONTRACT_VERSION)
    assert voice_contracts.VOICE_CONTRACT_VERSION == "1.2.0"


def test_livekit_dependencies_are_exact_pins():
    assert voice_contracts.LIVEKIT_SERVER_IMAGE.startswith("livekit/livekit-server:v")
    for pin in voice_contracts.LIVEKIT_PYTHON_PINS:
        assert re.fullmatch(r"[a-z0-9-]+==\d+\.\d+\.\d+", pin), pin
    assert re.fullmatch(r"livekit-client@\d+\.\d+\.\d+", voice_contracts.LIVEKIT_BROWSER_CLIENT_PIN)


def test_pyproject_pins_match_the_declared_python_sdks():
    pyproject_source = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    installed = {re.sub(r"\[[^\]]*\]", "", vdep) for vdep in re.findall(r'"(livekit[^"]*)"', pyproject_source)}
    missing = [vpin for vpin in voice_contracts.LIVEKIT_PYTHON_PINS if vpin not in installed]
    assert not missing, missing


@pytest.mark.parametrize("vlk_url", ["ws://localhost:7880", "wss://voice.flexus.internal:443", "wss://voice.flexus.internal.", "wss://notlivekit.cloudy.internal"])
def test_self_hosted_livekit_urls_are_accepted(vlk_url):
    assert voice_contracts.require_self_hosted_livekit_url(vlk_url) == vlk_url


def test_the_local_dev_url_passes_its_own_guard():
    assert voice_contracts.require_self_hosted_livekit_url(voice_contracts.LIVEKIT_LOCAL_DEV_URL) == voice_contracts.LIVEKIT_LOCAL_DEV_URL


@pytest.mark.parametrize(
    "vlk_url",
    [
        "wss://flexus-prod.livekit.cloud",
        "wss://LIVEKIT.CLOUD",
        "ws://sub.domain.livekit.cloud:7880",
        "wss://livekit.cloud.",
        "wss://flexus-prod.livekit.cloud.",
        "wss://flexus-prod.LiveKit.Cloud.:443",
    ],
)
def test_managed_livekit_urls_are_rejected(vlk_url):
    with pytest.raises(ValueError, match="managed livekit host"):
        voice_contracts.require_self_hosted_livekit_url(vlk_url)


@pytest.mark.parametrize("vlk_url", ["https://voice.flexus.internal", "voice.flexus.internal", "wss://"])
def test_non_signal_livekit_urls_are_rejected(vlk_url):
    with pytest.raises(ValueError):
        voice_contracts.require_self_hosted_livekit_url(vlk_url)


def test_openrouter_audio_endpoints_are_the_documented_ones():
    assert voice_contracts.OPENROUTER_STT_ENDPOINT == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert voice_contracts.OPENROUTER_TTS_ENDPOINT == "https://openrouter.ai/api/v1/audio/speech"
    assert voice_contracts.OPENROUTER_STT_ENDPOINT == workspace_stt.OPENROUTER_STT_ENDPOINT


def test_tts_is_requested_as_incrementally_consumable_pcm():
    assert voice_contracts.OPENROUTER_TTS_RESPONSE_FORMAT == "pcm"
    assert "response_format" in voice_contracts.OPENROUTER_TTS_REQUEST_FIELDS
    assert "input" in voice_contracts.OPENROUTER_TTS_REQUEST_FIELDS


def test_stt_request_shape_matches_the_existing_openrouter_caller():
    workspace_stt_source = pathlib.Path(workspace_stt.__file__).read_text(encoding="utf-8")
    for field in voice_contracts.OPENROUTER_STT_REQUEST_FIELDS[:2]:
        assert '"%s"' % field in workspace_stt_source
    assert voice_contracts.OPENROUTER_STT_PROMPT_VOCABULARY_SUPPORTED is False


def test_provisional_stt_default_is_the_latency_budget_winner():
    assert voice_contracts.VOICE_DEFAULT_STT_MODEL == "openai/gpt-4o-mini-transcribe"


def test_prototype_echo_uses_the_measured_boss_voice_without_claiming_a_profile_system():
    assert voice_contracts.VOICE_PROTOTYPE_ECHO_TTS_VOICE == "alloy"


def test_pcm_format_is_mono_signed_16_bit_little_endian():
    assert voice_contracts.VOICE_PCM_ENCODING == "pcm_s16le"
    assert voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES == 2
    assert voice_contracts.VOICE_PCM_CHANNELS == 1


def test_room_and_provider_sample_rates_are_frame_aligned():
    assert voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ == voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
    assert voice_contracts.VOICE_STT_SAMPLE_RATE_HZ <= voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
    assert voice_contracts.pcm_frame_bytes(voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ, voice_contracts.VOICE_RTC_FRAME_MS) == 2400


def test_room_io_values_are_exactly_what_the_pinned_agents_sdk_uses():
    assert voice_contracts.LIVEKIT_PYTHON_AGENTS_PIN == ROOM_IO_VALUES_WERE_READ_FROM
    assert voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ == 24000
    assert voice_contracts.VOICE_RTC_FRAME_MS == 50
    assert voice_contracts.VOICE_RTC_QUEUE_MS == 200


@pytest.mark.parametrize("vpcm_sample_rate_hz", [16000, 24000])
def test_pcm_frame_bytes_and_duration_round_trip(vpcm_sample_rate_hz):
    frame_bytes = voice_contracts.pcm_frame_bytes(vpcm_sample_rate_hz, voice_contracts.VOICE_RTC_FRAME_MS)
    assert voice_contracts.pcm_duration_seconds(frame_bytes, vpcm_sample_rate_hz) == pytest.approx(voice_contracts.VOICE_RTC_FRAME_MS / 1000)


@pytest.mark.parametrize(("vpcm_sample_rate_hz", "vpcm_frame_ms"), [(0, 50), (24000, 0), (24001, 1)])
def test_pcm_frame_bytes_rejects_unrepresentable_frames(vpcm_sample_rate_hz, vpcm_frame_ms):
    with pytest.raises(ValueError):
        voice_contracts.pcm_frame_bytes(vpcm_sample_rate_hz, vpcm_frame_ms)


def test_pcm_duration_rejects_impossible_inputs():
    with pytest.raises(ValueError):
        voice_contracts.pcm_duration_seconds(-1, 24000)
    with pytest.raises(ValueError):
        voice_contracts.pcm_duration_seconds(100, 0)


def test_cancellation_contract_names_every_stop_cause_and_bounds_each_stage():
    assert set(voice_contracts.VOICE_CANCEL_REASONS) >= {"barge_in", "handoff", "session_end", "deadline", "provider_error", "stale_turn"}
    assert len(set(voice_contracts.VOICE_CANCEL_REASONS)) == len(voice_contracts.VOICE_CANCEL_REASONS)
    assert voice_contracts.VOICE_CANCEL_STOP_PUBLISH_DEADLINE_S < voice_contracts.VOICE_CANCEL_PROVIDER_CLOSE_DEADLINE_S
    assert voice_contracts.VOICE_CANCEL_PROVIDER_CLOSE_DEADLINE_S < voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S
    assert voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S < voice_contracts.VOICE_TTS_STREAM_DEADLINE_S


def test_segmenter_bounds_are_ordered():
    assert voice_contracts.VOICE_SEGMENT_MIN_CHARS < voice_contracts.VOICE_SEGMENT_MAX_CHARS
    assert voice_contracts.VOICE_SEGMENT_MAX_BUFFER_MS > 0


def test_correlation_ids_cover_every_stage_of_one_spoken_turn():
    assert set(voice_contracts.VOICE_CORRELATION_ID_FIELDS) >= {
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
    }


def test_opaque_ids_are_unguessable_and_carry_no_participant_name():
    room = voice_contracts.new_opaque_voice_id(voice_contracts.VOICE_ROOM_NAME_PREFIX)
    participant = voice_contracts.new_opaque_voice_id(voice_contracts.VOICE_PARTICIPANT_IDENTITY_PREFIX)
    assert room != voice_contracts.new_opaque_voice_id(voice_contracts.VOICE_ROOM_NAME_PREFIX)
    assert voice_contracts.is_opaque_voice_id(room)
    assert voice_contracts.is_opaque_voice_id(participant)
    assert len(room) == len(voice_contracts.VOICE_ROOM_NAME_PREFIX) + voice_contracts.VOICE_OPAQUE_ID_HEX_CHARS


@pytest.mark.parametrize("vid_value", ["vroom_boss-and-anna", "anna@example.com", "vroom_ABCDEF", "vpart_", ""])
def test_non_opaque_identifiers_are_rejected(vid_value):
    assert not voice_contracts.is_opaque_voice_id(vid_value)


def test_unknown_id_prefixes_are_refused():
    with pytest.raises(ValueError):
        voice_contracts.new_opaque_voice_id("room_")


def test_env_var_contract_is_unique_prefixed_and_documented():
    names = [v.vvar_name for v in voice_contracts.VOICE_ENV_VARS]
    assert len(set(names)) == len(names)
    assert {n for n in names if not n.startswith("FLEXUS_VOICE_")} == {"OPENROUTER_API_KEY"}
    assert {v.vvar_name for v in voice_contracts.VOICE_ENV_VARS if v.vvar_required} == {
        "FLEXUS_VOICE_LIVEKIT_URL",
        "FLEXUS_VOICE_LIVEKIT_API_KEY",
        "FLEXUS_VOICE_LIVEKIT_API_SECRET",
        "OPENROUTER_API_KEY",
    }
    for v in voice_contracts.VOICE_ENV_VARS:
        assert v.vvar_purpose.strip()


def test_every_declared_env_var_appears_in_env_example():
    declared = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    missing = [v.vvar_name for v in voice_contracts.VOICE_ENV_VARS if "\n%s=" % v.vvar_name not in declared]
    assert not missing, missing


@pytest.mark.parametrize(
    ("vid_prefix", "vid_value"),
    [
        (voice_contracts.VOICE_ROOM_NAME_PREFIX, "vroom_" + "0" * 32),
        (voice_contracts.VOICE_PARTICIPANT_IDENTITY_PREFIX, "vpart_" + "0" * 32),
        (voice_contracts.VOICE_SESSION_ID_PREFIX, "vsess_" + "0" * 32),
    ],
)
def test_an_opaque_id_is_only_valid_for_the_kind_it_was_minted_as(vid_prefix, vid_value):
    assert voice_contracts.is_opaque_voice_id_of(vid_value, vid_prefix)
    for other_prefix in voice_contracts.VOICE_OPAQUE_ID_PREFIXES:
        if other_prefix != vid_prefix:
            assert not voice_contracts.is_opaque_voice_id_of(vid_value, other_prefix)


def test_checking_an_id_against_an_unknown_kind_is_refused():
    with pytest.raises(ValueError):
        voice_contracts.is_opaque_voice_id_of("vroom_" + "0" * 32, "room_")
