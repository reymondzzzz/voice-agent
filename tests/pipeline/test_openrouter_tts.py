from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from voice_agent.pipeline import openrouter_tts
from voice_agent.pipeline import voice_contracts


class VoiceTestByteStream(httpx.AsyncByteStream):
    def __init__(self, vtest_chunks: list[bytes], *, vtest_delay_s: float = 0.0) -> None:
        self.vtest_chunks = vtest_chunks
        self.vtest_delay_s = vtest_delay_s
        self.vtest_reads = 0
        self.vtest_closed = False

    async def __aiter__(self):
        for vchunk in self.vtest_chunks:
            if self.vtest_delay_s:
                await asyncio.sleep(self.vtest_delay_s)
            self.vtest_reads += 1
            yield vchunk

    async def aclose(self) -> None:
        self.vtest_closed = True


def _tts_request() -> openrouter_tts.VoiceTtsRequest:
    return openrouter_tts.VoiceTtsRequest(
        vtts_input="A short, synthetic sentence for the voice adapter.",
        vtts_voice="am_adam",
    )


def _pcm_response(
    vrequest: httpx.Request,
    vstream: VoiceTestByteStream,
    *,
    vcontent_type: str = "audio/pcm",
) -> httpx.Response:
    return httpx.Response(
        200,
        headers={
            "Content-Type": vcontent_type,
            "X-Generation-Id": "gen-voice-test",
        },
        stream=vstream,
        request=vrequest,
    )


@pytest.mark.asyncio
async def test_pcm_stream_yields_before_the_complete_response() -> None:
    vframe = bytes(voice_contracts.pcm_frame_bytes(voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ, voice_contracts.VOICE_RTC_FRAME_MS))
    vsource = VoiceTestByteStream([vframe, vframe])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        vbody = json.loads(vrequest.content)
        assert vbody == {
            "model": voice_contracts.VOICE_DEFAULT_TTS_MODEL,
            "input": _tts_request().vtts_input,
            "voice": "am_adam",
            "response_format": "pcm",
            "speed": 1.0,
        }
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        vfirst = await anext(vstream)
        assert vfirst == vframe
        assert vsource.vtest_reads == 1
        assert vstream.vstream_completed is False
        vremaining = [vchunk async for vchunk in vstream]

    assert vremaining == [vframe]
    assert vstream.vstream_completed is True
    assert vstream.vstream_closed is True
    assert vstream.vstream_generation_id == "gen-voice-test"
    assert vstream.vstream_chunk_count == 2
    assert vstream.vstream_total_bytes == len(vframe) * 2
    assert vstream.vstream_audio_duration_seconds == pytest.approx(0.1)
    assert vstream.vstream_sample_rate_hz == 24000
    assert vstream.vstream_channels == 1
    assert vstream.vstream_sample_width_bytes == 2
    assert vstream.vstream_encoding == "pcm_s16le"
    assert vstream.vstream_declared_layout_fields == ()
    assert vstream.vstream_layout_validation_source == "openai_audio_speech_pcm_contract"
    assert vsource.vtest_closed is True


@pytest.mark.asyncio
async def test_pcm_stream_reassembles_split_samples_without_buffering_the_body() -> None:
    vsource = VoiceTestByteStream([b"\x01", b"\x02\x03\x04", b"\x05\x06"])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        vchunks = [vchunk async for vchunk in vstream]

    assert vchunks == [b"\x01\x02\x03\x04", b"\x05\x06"]
    assert vstream.vstream_total_bytes == 6


@pytest.mark.asyncio
async def test_pcm_stream_rejects_an_oversized_upstream_chunk_without_retaining_it() -> None:
    voversized = bytes(openrouter_tts.TTS_MAX_PENDING_PCM_BYTES + 2)
    vsource = VoiceTestByteStream([voversized])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_pcm_pending_limit") as verror:
            await anext(vstream)
        assert verror.value.verror_retryable is False
        assert vsource.vtest_reads == 1

    assert vstream.vstream_total_bytes == 0
    assert len(vstream._vpending) == 0
    assert vstream.vstream_closed is True
    assert vsource.vtest_closed is True


@pytest.mark.asyncio
async def test_pcm_stream_rejects_total_audio_over_the_stream_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openrouter_tts, "TTS_MAX_TOTAL_PCM_BYTES", 8)
    vsource = VoiceTestByteStream([b"\x01\x00\x02\x00", b"\x03\x00\x04\x00", b"\x05\x00"])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        assert await anext(vstream) == b"\x01\x00\x02\x00"
        assert await anext(vstream) == b"\x03\x00\x04\x00"
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_pcm_total_limit") as verror:
            await anext(vstream)

    assert verror.value.verror_retryable is False
    assert vstream.vstream_total_bytes == 8
    assert len(vstream._vpending) == 0
    assert vstream.vstream_closed is True
    assert vsource.vtest_closed is True


@pytest.mark.asyncio
async def test_pcm_stream_accepts_matching_declared_layout() -> None:
    vsource = VoiceTestByteStream([b"\x00\x00"])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(
            vrequest,
            vsource,
            vcontent_type="audio/pcm; rate=24000; channels=1; bits=16; encoding=pcm_s16le; endianness=little; signed=true",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        assert await anext(vstream) == b"\x00\x00"
        await vstream.aclose()

    assert vstream.vstream_declared_layout_fields == (
        "sample_rate_hz",
        "channels",
        "sample_width_bits",
        "encoding",
        "byte_order",
        "signed",
    )


@pytest.mark.parametrize(
    "vcontent_type",
    [
        "audio/pcm; rate=16000",
        "audio/pcm; channels=2",
        "audio/pcm; bits=24",
        "audio/pcm; encoding=pcm_s16be",
        "audio/pcm; endianness=big",
        "audio/pcm; signed=false",
    ],
)
@pytest.mark.asyncio
async def test_pcm_stream_rejects_contradictory_layout_metadata(vcontent_type: str) -> None:
    vsource = VoiceTestByteStream([bytes(32000)])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource, vcontent_type=vcontent_type)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_pcm_layout_mismatch"):
            await openrouter_tts.open_openrouter_pcm_stream(
                _tts_request(),
                vtts_api_key="test-key",
                vtts_client=vclient,
            )

    assert vsource.vtest_closed is True


@pytest.mark.asyncio
async def test_pcm_stream_rejects_an_unaligned_final_sample() -> None:
    vsource = VoiceTestByteStream([b"\x01"])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_unaligned_pcm"):
            await anext(vstream)

    assert vstream.vstream_total_bytes == 0
    assert vstream.vstream_closed is True


@pytest.mark.asyncio
async def test_cancel_closes_the_provider_stream_and_stops_chunks() -> None:
    vsource = VoiceTestByteStream([b"\x00\x00", b"\x01\x00"])

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        assert await anext(vstream) == b"\x00\x00"
        await vstream.cancel("barge_in")
        with pytest.raises(StopAsyncIteration):
            await anext(vstream)

    assert vstream.vstream_cancel_reason == "barge_in"
    assert vstream.vstream_completed is False
    assert vstream.vstream_closed is True
    assert vsource.vtest_closed is True
    assert vsource.vtest_reads == 1


@pytest.mark.asyncio
async def test_first_byte_deadline_closes_the_provider_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    vsource = VoiceTestByteStream([b"\x00\x00"], vtest_delay_s=0.05)
    monkeypatch.setattr(voice_contracts, "VOICE_TTS_FIRST_BYTE_DEADLINE_S", 0.01)

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(vrequest, vsource)

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_stream_deadline"):
            await anext(vstream)

    assert vstream.vstream_cancel_reason == "deadline"
    assert vstream.vstream_closed is True
    assert vsource.vtest_closed is True


@pytest.mark.asyncio
async def test_error_response_is_sanitized() -> None:
    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            content=b'{"error":"secret provider body and request text"}',
            request=vrequest,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        with pytest.raises(openrouter_tts.VoiceTtsError) as vexc:
            await openrouter_tts.open_openrouter_pcm_stream(
                _tts_request(),
                vtts_api_key="test-key",
                vtts_client=vclient,
            )

    assert str(vexc.value) == "openrouter_tts_http_429"
    assert vexc.value.verror_status_code == 429
    assert vexc.value.verror_retryable is True
    assert "secret" not in str(vexc.value)
    assert _tts_request().vtts_input not in str(vexc.value)


@pytest.mark.asyncio
async def test_response_requires_pcm_and_a_generation_id() -> None:
    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Type": "audio/mpeg"},
            content=b"not pcm",
            request=vrequest,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_invalid_response_metadata"):
            await openrouter_tts.open_openrouter_pcm_stream(
                _tts_request(),
                vtts_api_key="test-key",
                vtts_client=vclient,
            )


@pytest.mark.asyncio
async def test_generation_usage_exposes_cost_without_content() -> None:
    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        assert vrequest.url.params["id"] == "gen-voice-test"
        return httpx.Response(
            200,
            json={
                "data": {
                    "provider_name": "DeepInfra",
                    "total_cost": 0.000031,
                    "cancelled": False,
                    "native_tokens_prompt": 50,
                }
            },
            request=vrequest,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vusage = await openrouter_tts.fetch_openrouter_tts_usage(
            "gen-voice-test",
            vusage_api_key="test-key",
            vusage_client=vclient,
        )

    assert vusage == openrouter_tts.VoiceTtsUsage(
        vusage_generation_id="gen-voice-test",
        vusage_provider_name="DeepInfra",
        vusage_total_cost_usd=0.000031,
        vusage_cancelled=False,
        vusage_native_input_units=50,
    )


@pytest.mark.parametrize(
    ("vrequest", "verror"),
    [
        (openrouter_tts.VoiceTtsRequest(vtts_input="", vtts_voice="am_adam"), "input"),
        (openrouter_tts.VoiceTtsRequest(vtts_input="x", vtts_voice=""), "voice"),
        (openrouter_tts.VoiceTtsRequest(vtts_input="x", vtts_voice="am_adam", vtts_speed=0.0), "speed"),
        (openrouter_tts.VoiceTtsRequest(vtts_input="x" * 161, vtts_voice="am_adam"), "exceeds"),
    ],
)
@pytest.mark.asyncio
async def test_invalid_requests_fail_before_network(vrequest: openrouter_tts.VoiceTtsRequest, verror: str) -> None:
    async def vhandler(vhttp_request: httpx.Request) -> httpx.Response:
        raise AssertionError("network must not be reached")

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        with pytest.raises(ValueError, match=verror):
            await openrouter_tts.open_openrouter_pcm_stream(
                vrequest,
                vtts_api_key="test-key",
                vtts_client=vclient,
            )


@pytest.mark.asyncio
async def test_stream_open_overrides_the_httpx_default_read_timeout() -> None:
    vseen: list[httpx.Timeout] = []
    vframe = bytes(voice_contracts.pcm_frame_bytes(voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ, voice_contracts.VOICE_RTC_FRAME_MS))

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        vseen.append(vrequest.extensions["timeout"])
        return _pcm_response(vrequest, VoiceTestByteStream([vframe]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        await vstream.aclose()

    assert vseen[0]["read"] == voice_contracts.VOICE_TTS_FIRST_BYTE_DEADLINE_S
    assert vseen[0]["connect"] == voice_contracts.VOICE_TTS_STREAM_DEADLINE_S


@pytest.mark.asyncio
async def test_a_44100_provider_stream_is_downsampled_to_the_room_rate() -> None:
    vsource_rate_hz = 44100
    vseconds = 0.5
    vsamples = int(vsource_rate_hz * vseconds)
    vpcm = b"".join(int(3000).to_bytes(2, "little", signed=True) for _ in range(vsamples))
    vchunks = [vpcm[vindex : vindex + 7000] for vindex in range(0, len(vpcm), 7000)]

    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(
            vrequest,
            VoiceTestByteStream(vchunks),
            vcontent_type="audio/pcm;rate=44100;channels=1",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        vstream = await openrouter_tts.open_openrouter_pcm_stream(
            _tts_request(),
            vtts_api_key="test-key",
            vtts_client=vclient,
        )
        vreceived = b"".join([vchunk async for vchunk in vstream])

    assert vstream.vstream_sample_rate_hz == voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ
    vexpected_samples = vseconds * voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ
    assert abs(len(vreceived) / 2 - vexpected_samples) < vexpected_samples * 0.02
    assert len(vreceived) % 2 == 0


@pytest.mark.asyncio
async def test_an_unsupported_provider_sample_rate_is_still_refused() -> None:
    async def vhandler(vrequest: httpx.Request) -> httpx.Response:
        return _pcm_response(
            vrequest,
            VoiceTestByteStream([b"\x00\x00"]),
            vcontent_type="audio/pcm;rate=16000;channels=1",
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(vhandler)) as vclient:
        with pytest.raises(openrouter_tts.VoiceTtsError, match="openrouter_tts_pcm_layout_mismatch"):
            await openrouter_tts.open_openrouter_pcm_stream(
                _tts_request(),
                vtts_api_key="test-key",
                vtts_client=vclient,
            )
