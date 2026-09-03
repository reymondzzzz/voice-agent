from __future__ import annotations

import os

import aiohttp
import pytest
from dotenv import load_dotenv

import contracts
import pcm_resample

pytestmark = pytest.mark.provider

SPOKEN_PROBE = "Hello, this is Alice testing the voice pipeline."


@pytest.fixture(scope="session")
def vapi_key() -> str:
    load_dotenv(".env.local")
    vkey = os.environ.get("OPENROUTER_API_KEY")
    if not vkey:
        pytest.skip("OPENROUTER_API_KEY is not set")
    return vkey


@pytest.mark.asyncio
async def test_tts_then_stt_round_trips_the_spoken_probe(vapi_key):
    vheaders = {"Authorization": f"Bearer {vapi_key}"}
    async with aiohttp.ClientSession(headers=vheaders) as vsession:
        async with vsession.post(
            contracts.OPENROUTER_TTS_ENDPOINT,
            json={
                "model": contracts.VOICE_DEFAULT_TTS_MODEL,
                "input": SPOKEN_PROBE,
                "voice": "alloy",
                "response_format": contracts.OPENROUTER_TTS_RESPONSE_FORMAT,
                "speed": 1.0,
            },
        ) as vspeech:
            assert vspeech.status == 200
            vprovider_rate = contracts.require_accepted_tts_sample_rate(
                contracts.sample_rate_from_content_type(vspeech.headers.get("content-type", ""))
            )
            vpcm = await vspeech.read()

        vresampler = pcm_resample.resampler_for(vprovider_rate)
        if vresampler is not None:
            vpcm = vresampler.process(vpcm) + vresampler.flush()

        import base64

        async with vsession.post(
            contracts.OPENROUTER_STT_ENDPOINT,
            json={
                "model": contracts.VOICE_DEFAULT_STT_MODEL,
                "input_audio": {
                    "data": base64.b64encode(
                        contracts.pcm16_to_wav(vpcm, contracts.VOICE_ROOM_SAMPLE_RATE_HZ)
                    ).decode("ascii"),
                    "format": contracts.OPENROUTER_STT_INPUT_AUDIO_FORMAT,
                },
                "language": "en",
            },
        ) as vtranscription:
            assert vtranscription.status == 200
            vtext = (await vtranscription.json())["text"]

    assert "alice" in vtext.lower()
    assert "voice pipeline" in vtext.lower()
