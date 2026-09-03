from __future__ import annotations

import os

import pytest

from voice_agent.pipeline import openrouter_tts

PROVIDER_TEST_TEXT = "Flexus incremental voice smoke test."
PROVIDER_TEST_VOICES = ("am_adam", "af_heart")


@pytest.mark.integration
@pytest.mark.provider
@pytest.mark.skipif(os.getenv("FLEXUS_PROVIDER_TESTS") != "1", reason="set FLEXUS_PROVIDER_TESTS=1 for metered provider tests")
@pytest.mark.parametrize("vtts_voice", PROVIDER_TEST_VOICES)
@pytest.mark.asyncio
async def test_openrouter_pcm_provider_streams_and_cancels(vtts_voice: str) -> None:
    vstream = await openrouter_tts.open_openrouter_pcm_stream(
        openrouter_tts.VoiceTtsRequest(
            vtts_input=PROVIDER_TEST_TEXT,
            vtts_voice=vtts_voice,
        ),
        vtts_api_key=os.environ["OPENROUTER_API_KEY"],
    )
    async with vstream:
        vfirst = await anext(vstream)
        assert vfirst
        assert len(vfirst) % 2 == 0
        assert len(vfirst) <= openrouter_tts.TTS_MAX_PCM_CHUNK_BYTES
        assert vstream.vstream_sample_rate_hz == 24000
        assert vstream.vstream_channels == 1
        assert vstream.vstream_sample_width_bytes == 2
        assert vstream.vstream_encoding == "pcm_s16le"
        assert vstream.vstream_layout_validation_source == "openai_audio_speech_pcm_contract"
        assert vstream.vstream_completed is False
        await vstream.cancel("session_end")
    assert vstream.vstream_cancel_reason == "session_end"
    assert vstream.vstream_closed is True
