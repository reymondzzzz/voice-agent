from __future__ import annotations

import asyncio

import pytest
from livekit.agents import DEFAULT_API_CONNECT_OPTIONS

from examples import livekit_providers
from voice_agent.pipeline import voice_contracts, voice_profile_ops

SPOKEN = (
    "Hello there, this is a first spoken sentence. "
    "Here is a second sentence with enough words to stand alone. "
    "And a third one that also carries plenty of words. "
)


class _FakePcmStream:
    def __init__(self, vtext: str, vopened: list[str]) -> None:
        self.vstream_generation_id = f"gen-{len(vopened)}"
        self._vtext = vtext
        self.vclosed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *vargs):
        await self.aclose()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        yield b"\x00\x01" * 240

    async def aclose(self) -> None:
        self.vclosed = True


@pytest.fixture
def vopened(monkeypatch) -> list[str]:
    vopened: list[str] = []

    async def fake_open(vrequest, *, vtts_api_key, vtts_client=None):
        vopened.append(vrequest.vtts_input)
        await asyncio.sleep(0)
        return _FakePcmStream(vrequest.vtts_input, vopened)

    monkeypatch.setattr(livekit_providers.openrouter_tts, "open_openrouter_pcm_stream", fake_open)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return vopened


def build_tts() -> livekit_providers.FlexusOpenRouterTTS:
    return livekit_providers.FlexusOpenRouterTTS(voice_profile_ops.resolve_registered_voice_profile("voice_boss"))


@pytest.mark.asyncio
async def test_streaming_tts_segments_the_text_through_the_moved_segmenter(vopened):
    vtts = build_tts()
    async with vtts.stream(conn_options=DEFAULT_API_CONNECT_OPTIONS) as vstream:
        vstream.push_text(SPOKEN)
        vstream.end_input()
        async for _ in vstream:
            pass

    assert len(vopened) >= 2, vopened
    for vsegment in vopened:
        assert len(vsegment.split()) >= voice_contracts.VOICE_SEGMENT_MIN_WORDS, vsegment
        assert len(vsegment) <= voice_contracts.VOICE_SEGMENT_MAX_CHARS, vsegment
    assert " ".join(vopened).count("first spoken sentence") == 1


@pytest.mark.asyncio
async def test_short_fragments_are_merged_rather_than_spoken_alone(vopened):
    vtts = build_tts()
    async with vtts.stream(conn_options=DEFAULT_API_CONNECT_OPTIONS) as vstream:
        vstream.push_text("Yes. No. Maybe so, but only when there are clearly enough words here. ")
        vstream.end_input()
        async for _ in vstream:
            pass

    assert vopened, "nothing was synthesized"
    assert all(len(vsegment.split()) >= voice_contracts.VOICE_SEGMENT_MIN_WORDS for vsegment in vopened), vopened


@pytest.mark.asyncio
async def test_prefetch_opens_ahead_of_playback(monkeypatch):
    vopened: list[str] = []
    vin_flight = {"max": 0, "now": 0}

    async def fake_open(vrequest, *, vtts_api_key, vtts_client=None):
        vopened.append(vrequest.vtts_input)
        vin_flight["now"] += 1
        vin_flight["max"] = max(vin_flight["max"], vin_flight["now"])
        await asyncio.sleep(0.01)
        vin_flight["now"] -= 1
        return _FakePcmStream(vrequest.vtts_input, vopened)

    monkeypatch.setattr(livekit_providers.openrouter_tts, "open_openrouter_pcm_stream", fake_open)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    vtts = build_tts()
    async with vtts.stream(conn_options=DEFAULT_API_CONNECT_OPTIONS) as vstream:
        vstream.push_text(SPOKEN)
        vstream.end_input()
        async for _ in vstream:
            pass

    assert vin_flight["max"] >= 2, f"expected the prefetcher to open ahead, saw {vin_flight['max']}"
    assert vin_flight["max"] <= voice_contracts.VOICE_TTS_PREFETCH_SEGMENTS, vin_flight["max"]


class _SlowPcmStream:
    def __init__(self, vchunks: int, vchunk_bytes: int, vdelay_s: float) -> None:
        self.vstream_generation_id = "gen-slow"
        self._vchunks = vchunks
        self._vchunk_bytes = vchunk_bytes
        self._vdelay_s = vdelay_s

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for _ in range(self._vchunks):
            await asyncio.sleep(self._vdelay_s)
            yield b"\x00\x01" * (self._vchunk_bytes // 2)

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_ready_waits_for_the_preroll_cushion():
    # 24000 Hz mono s16le -> 48000 bytes per second of audio
    vsegment = livekit_providers.DrainedSegment(_SlowPcmStream(vchunks=10, vchunk_bytes=12000, vdelay_s=0.01))
    try:
        await vsegment.ready(0.5)
        assert vsegment.vbuffered_seconds >= 0.5
    finally:
        await vsegment.aclose()


@pytest.mark.asyncio
async def test_ready_returns_early_when_the_segment_is_shorter_than_the_cushion():
    vsegment = livekit_providers.DrainedSegment(_SlowPcmStream(vchunks=1, vchunk_bytes=4800, vdelay_s=0.01))
    try:
        await asyncio.wait_for(vsegment.ready(10.0), timeout=2.0)
        assert vsegment.vbuffered_seconds == pytest.approx(0.1, abs=0.01)
    finally:
        await vsegment.aclose()


@pytest.mark.asyncio
async def test_a_drained_segment_downloads_without_being_iterated():
    vsegment = livekit_providers.DrainedSegment(_SlowPcmStream(vchunks=5, vchunk_bytes=4800, vdelay_s=0.01))
    try:
        await asyncio.sleep(0.2)
        assert vsegment.vbuffered_seconds > 0.0, "prefetch did not download ahead of playback"
    finally:
        await vsegment.aclose()


def _pcm(vamplitude: int, vsamples: int = 4000) -> bytes:
    import struct

    return struct.pack(f"<{vsamples}h", *([vamplitude, -vamplitude] * (vsamples // 2)))


def test_quiet_audio_is_below_the_moved_energy_threshold():
    from voice_agent.pipeline import voice_interruption_policy

    vconfig = voice_interruption_policy.VOICE_DEFAULT_INTERRUPTION_CONFIG
    assert not livekit_providers.has_speech_energy(_pcm(vconfig.venergy_threshold - 100), vconfig)


def test_loud_audio_passes_the_energy_gate():
    from voice_agent.pipeline import voice_interruption_policy

    vconfig = voice_interruption_policy.VOICE_DEFAULT_INTERRUPTION_CONFIG
    assert livekit_providers.has_speech_energy(_pcm(vconfig.venergy_threshold + 500), vconfig)


def test_silence_never_passes_the_energy_gate():
    from voice_agent.pipeline import voice_interruption_policy

    vconfig = voice_interruption_policy.VOICE_DEFAULT_INTERRUPTION_CONFIG
    assert not livekit_providers.has_speech_energy(b"", vconfig)
    assert not livekit_providers.has_speech_energy(_pcm(0), vconfig)


def test_backchannels_do_not_commit_a_turn():
    from voice_agent.pipeline import voice_interruption_policy

    for vbackchannel in ("mhm", "uh-huh", "угу", "Mhm."):
        assert not voice_interruption_policy.transcript_commits_interruption(vbackchannel)
    assert voice_interruption_policy.transcript_commits_interruption("what is the weather in London")
