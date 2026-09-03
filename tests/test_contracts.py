from __future__ import annotations

import io
import wave

import pytest

import contracts


@pytest.mark.parametrize(
    ("vheader", "vexpected"),
    [
        ("audio/pcm;rate=44100;channels=1", 44100),
        ("audio/pcm; rate=24000; channels=1", 24000),
        ("audio/pcm", contracts.VOICE_TTS_SAMPLE_RATE_HZ),
        ("", contracts.VOICE_TTS_SAMPLE_RATE_HZ),
        ("audio/pcm;rate=notanumber", contracts.VOICE_TTS_SAMPLE_RATE_HZ),
    ],
)
def test_sample_rate_comes_from_the_content_type(vheader, vexpected):
    assert contracts.sample_rate_from_content_type(vheader) == vexpected


def test_fish_audio_returns_an_accepted_rate():
    assert contracts.require_accepted_tts_sample_rate(44100) == 44100


def test_an_unexpected_provider_rate_is_refused():
    with pytest.raises(ValueError, match="expected one of"):
        contracts.require_accepted_tts_sample_rate(48000)


@pytest.mark.parametrize(
    "vurl",
    ["wss://my-project.livekit.cloud", "ws://livekit.cloud", "wss://sub.livekit.cloud."],
)
def test_managed_livekit_hosts_are_refused(vurl):
    with pytest.raises(ValueError, match="self-hosted"):
        contracts.require_self_hosted_livekit_url(vurl)


@pytest.mark.parametrize("vurl", ["ws://localhost:7880", "ws://127.0.0.1:7880", "wss://voice.example.com"])
def test_self_hosted_urls_pass(vurl):
    assert contracts.require_self_hosted_livekit_url(vurl) == vurl


@pytest.mark.parametrize("vurl", ["http://localhost:7880", "localhost:7880", "ws://"])
def test_malformed_signal_urls_are_refused(vurl):
    with pytest.raises(ValueError):
        contracts.require_self_hosted_livekit_url(vurl)


def test_wav_wrapper_declares_the_sample_rate_it_was_given():
    vwav = contracts.pcm16_to_wav(b"\x00\x01" * 1600, 16000)
    with wave.open(io.BytesIO(vwav), "rb") as vreader:
        assert vreader.getframerate() == 16000
        assert vreader.getnchannels() == contracts.VOICE_PCM_CHANNELS
        assert vreader.getsampwidth() == contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES
        assert vreader.getnframes() == 1600


def test_wav_wrapper_refuses_a_partial_frame():
    with pytest.raises(ValueError, match="whole number"):
        contracts.pcm16_to_wav(b"\x00", 16000)


def test_pcm_duration_matches_the_sample_rate():
    assert contracts.pcm_duration_seconds(24000 * 2, 24000) == pytest.approx(1.0)


def test_max_utterance_bytes_bounds_thirty_seconds():
    vbytes = contracts.max_utterance_bytes(contracts.VOICE_STT_SAMPLE_RATE_HZ)
    assert contracts.pcm_duration_seconds(vbytes, contracts.VOICE_STT_SAMPLE_RATE_HZ) == pytest.approx(
        contracts.VOICE_MAX_UTTERANCE_SECONDS
    )
