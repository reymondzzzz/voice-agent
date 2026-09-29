import numpy as np
import pytest
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions

from examples.livekit_providers import FlexusOpenRouterSTT
from voice_agent.pipeline import voice_stt

RATE_HZ = 24000


class FlakyProvider:
    def __init__(self, vfailures: list[voice_stt.SttError]) -> None:
        self.vfailures = vfailures

    async def transcribe_utterance(self, _vpcm: bytes, _vconfig: voice_stt.SttConfig) -> voice_stt.TranscriptEvent:
        if self.vfailures:
            raise self.vfailures.pop(0)
        return voice_stt.TranscriptEvent("final", "the deadline is october fifteenth", "fake", "en", 1.0, 1.0, voice_stt.SttUsage(None, None, None, None, None), None)

    async def aclose(self) -> None:
        pass


def speech() -> rtc.AudioFrame:
    vtone = (np.sin(2 * np.pi * 220 * np.arange(RATE_HZ) / RATE_HZ) * 8000).astype(np.int16)
    return rtc.AudioFrame(vtone.tobytes(), RATE_HZ, 1, RATE_HZ)


def stt_with(vfailures: list[voice_stt.SttError], monkeypatch: pytest.MonkeyPatch) -> FlexusOpenRouterSTT:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    vstt = FlexusOpenRouterSTT()
    vstt.vprovider = FlakyProvider(vfailures)
    return vstt


@pytest.mark.asyncio
async def test_a_timed_out_utterance_is_retried_instead_of_ending_recognition(monkeypatch: pytest.MonkeyPatch) -> None:
    vstt = stt_with([voice_stt.SttError("timeout", "openrouter stt exceeded its 15.0s deadline", None)], monkeypatch)
    vevent = await vstt.recognize(speech(), conn_options=APIConnectOptions(max_retry=1, retry_interval=0))
    assert vevent.alternatives[0].text == "the deadline is october fifteenth"


@pytest.mark.asyncio
async def test_unusable_audio_is_dropped_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    vstt = stt_with([voice_stt.SttError("invalid_audio", "too short", 400)], monkeypatch)
    vevent = await vstt.recognize(speech(), conn_options=APIConnectOptions(max_retry=0))
    assert vevent.alternatives[0].text == ""


@pytest.mark.asyncio
async def test_a_bad_key_still_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    vstt = stt_with([voice_stt.SttError("auth", "invalid key", 401)], monkeypatch)
    with pytest.raises(APIConnectionError):
        await vstt.recognize(speech(), conn_options=APIConnectOptions(max_retry=0))
