from __future__ import annotations

import base64
import io
import json
import logging
import wave

import httpx
import pytest

from voice_agent.pipeline import voice_contracts, voice_stt

STT_API_KEY = "sk-or-v1-test-key-never-logged"
STT_RATE = voice_contracts.VOICE_STT_SAMPLE_RATE_HZ


def one_second_of_pcm(seconds: float = 1.0) -> bytes:
    return b"\x11\x22" * int(STT_RATE * seconds)


@pytest.fixture
def stt_provider(monkeypatch):
    real_async_client = httpx.AsyncClient
    calls: list[httpx.Request] = []

    def build(handler):
        def recording_handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return handler(request)

        monkeypatch.setattr(voice_stt.httpx, "AsyncClient", lambda: real_async_client(transport=httpx.MockTransport(recording_handler)))
        return voice_stt.OpenRouterSttProvider(STT_API_KEY), calls

    return build


def transcription_response(text: str, usage: dict, generation_id: str = "gen-abc") -> httpx.Response:
    return httpx.Response(200, json={"text": text, "usage": usage}, headers={"X-Generation-Id": generation_id})


@pytest.mark.asyncio
async def test_one_utterance_produces_one_normalized_final_event(stt_provider):
    usage = {"seconds": 1.0, "cost": 3.33e-06}
    provider, _ = stt_provider(lambda request: transcription_response("  Book the meeting.  ", usage))
    config = voice_stt.SttConfig(sttc_model="openai/whisper-large-v3-turbo", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    events = [e async for e in provider.stream(one_second_of_pcm(), config)]
    await provider.aclose()

    assert len(events) == 1
    event = events[0]
    assert event.stte_kind == "final"
    assert event.stte_text == "Book the meeting."
    assert event.stte_model == "openai/whisper-large-v3-turbo"
    assert event.stte_audio_seconds == pytest.approx(1.0)
    assert event.stte_request_ms >= 0.0
    assert event.stte_provider_generation_id == "gen-abc"
    assert event.stte_usage.sttu_audio_seconds == pytest.approx(1.0)
    assert event.stte_usage.sttu_cost_usd == pytest.approx(3.33e-06)


@pytest.mark.asyncio
async def test_the_uploaded_payload_is_a_contract_shaped_wav(stt_provider):
    provider, calls = stt_provider(lambda request: transcription_response("ok", {}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    await provider.transcribe_utterance(one_second_of_pcm(0.5), config)
    await provider.aclose()

    body = json.loads(calls[0].content)
    assert body["model"] == "m"
    assert body["input_audio"]["format"] == voice_contracts.OPENROUTER_STT_INPUT_AUDIO_FORMAT
    assert "language" not in body
    with wave.open(io.BytesIO(base64.b64decode(body["input_audio"]["data"])), "rb") as uploaded:
        assert uploaded.getframerate() == STT_RATE
        assert uploaded.getnchannels() == voice_contracts.VOICE_PCM_CHANNELS
        assert uploaded.getsampwidth() == voice_contracts.VOICE_PCM_SAMPLE_WIDTH_BYTES
        assert uploaded.getnframes() == int(STT_RATE * 0.5)


@pytest.mark.asyncio
async def test_a_selected_language_is_sent_and_carried_on_the_event(stt_provider):
    provider, calls = stt_provider(lambda request: transcription_response("da", {}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language="ru", sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    event = await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert json.loads(calls[0].content)["language"] == "ru"
    assert event.stte_language == "ru"


@pytest.mark.asyncio
async def test_an_over_long_utterance_is_refused_without_calling_the_provider(stt_provider):
    provider, calls = stt_provider(lambda request: transcription_response("never", {}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)
    too_long = one_second_of_pcm(voice_contracts.VOICE_MAX_UTTERANCE_SECONDS + 0.1)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(too_long, config)
    await provider.aclose()

    assert raised.value.sterr_kind == "invalid_audio"
    assert calls == []


@pytest.mark.asyncio
async def test_an_empty_utterance_is_refused_without_calling_the_provider(stt_provider):
    provider, calls = stt_provider(lambda request: transcription_response("never", {}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(b"", config)
    await provider.aclose()

    assert raised.value.sterr_kind == "invalid_audio"
    assert calls == []


@pytest.mark.parametrize(
    ("status_code", "expected_kind"),
    [(401, "auth"), (403, "auth"), (429, "rate_limit"), (400, "invalid_request"), (404, "invalid_request"), (500, "provider_error"), (503, "provider_error")],
)
@pytest.mark.asyncio
async def test_provider_status_codes_become_normalized_error_kinds(stt_provider, status_code, expected_kind):
    provider, _ = stt_provider(lambda request: httpx.Response(status_code, json={"error": {"message": "nope", "code": status_code}}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == expected_kind
    assert raised.value.sterr_status_code == status_code


@pytest.mark.asyncio
async def test_a_provider_timeout_is_normalized_and_names_the_deadline(stt_provider):
    def timing_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    provider, _ = stt_provider(timing_out)
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=1.5)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "timeout"
    assert "1.5s" in str(raised.value)


@pytest.mark.asyncio
async def test_a_transport_failure_is_normalized_and_hides_the_provider_exception_type_from_callers(stt_provider):
    def refusing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    provider, _ = stt_provider(refusing)
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "transport"
    assert raised.value.sterr_status_code is None


@pytest.mark.asyncio
async def test_a_non_json_body_and_a_missing_text_field_are_both_malformed(stt_provider):
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)
    for response in (httpx.Response(200, text="<html>maintenance</html>"), httpx.Response(200, json={"usage": {}})):
        provider, _ = stt_provider(lambda request, r=response: r)
        with pytest.raises(voice_stt.SttError) as raised:
            await provider.transcribe_utterance(one_second_of_pcm(), config)
        await provider.aclose()
        assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.asyncio
async def test_an_empty_transcript_is_a_normal_final_event_rather_than_an_error(stt_provider):
    provider, _ = stt_provider(lambda request: transcription_response("   ", {"seconds": 1.0, "cost": 1e-06}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    event = await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert event.stte_kind == "final"
    assert event.stte_text == ""


@pytest.mark.asyncio
async def test_the_api_key_never_reaches_the_logs(stt_provider, caplog):
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)
    with caplog.at_level(logging.DEBUG, logger="voice_agent.pipeline.voice_stt"):
        provider, _ = stt_provider(lambda request: transcription_response("hello", {"seconds": 1.0, "cost": 1e-06}))
        await provider.transcribe_utterance(one_second_of_pcm(), config)
        await provider.aclose()
        failing, _ = stt_provider(lambda request: httpx.Response(500, text="upstream on fire"))
        with pytest.raises(voice_stt.SttError):
            await failing.transcribe_utterance(one_second_of_pcm(), config)
        await failing.aclose()

    assert caplog.records
    for record in caplog.records:
        assert STT_API_KEY not in record.getMessage()


def test_the_wav_wrapper_describes_the_pcm_it_wraps():
    pcm = one_second_of_pcm(0.25)
    wav_bytes = voice_stt.pcm16_to_wav(pcm, STT_RATE)

    with wave.open(io.BytesIO(wav_bytes), "rb") as parsed:
        assert parsed.getframerate() == STT_RATE
        assert parsed.getnchannels() == 1
        assert parsed.getsampwidth() == 2
        assert parsed.readframes(parsed.getnframes()) == pcm
    assert voice_contracts.pcm_duration_seconds(len(pcm), STT_RATE) == pytest.approx(0.25)


def test_the_wav_wrapper_refuses_a_partial_sample_and_a_bad_rate():
    with pytest.raises(ValueError):
        voice_stt.pcm16_to_wav(b"\x01", STT_RATE)
    with pytest.raises(ValueError):
        voice_stt.pcm16_to_wav(b"\x01\x02", 0)


def test_the_utterance_byte_bound_matches_the_contract():
    assert voice_stt.max_utterance_bytes(STT_RATE) == int(voice_contracts.VOICE_MAX_UTTERANCE_SECONDS) * STT_RATE * 2
    with pytest.raises(ValueError):
        voice_stt.max_utterance_bytes(0)


def test_the_interface_admits_partial_events_even_though_this_adapter_only_finalizes():
    assert voice_stt.STT_EVENT_KINDS == ("partial", "final")


def test_an_unknown_error_kind_cannot_be_constructed():
    with pytest.raises(ValueError):
        voice_stt.SttError("weird", "boom", None)


def test_a_provider_without_an_api_key_is_refused():
    with pytest.raises(ValueError):
        voice_stt.OpenRouterSttProvider("")


@pytest.mark.asyncio
async def test_a_partial_pcm_frame_is_a_normalized_error_rather_than_a_value_error(stt_provider):
    provider, calls = stt_provider(lambda request: transcription_response("never", {}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(0.5) + b"\x11", config)
    await provider.aclose()

    assert raised.value.sterr_kind == "invalid_audio"
    assert calls == []


@pytest.mark.parametrize("body", [{"text": 42}, {"text": ["book the meeting"]}, {"text": {"value": "hi"}}])
@pytest.mark.asyncio
async def test_a_wrongly_typed_text_field_is_malformed_rather_than_an_attribute_error(stt_provider, body):
    provider, _ = stt_provider(lambda request: httpx.Response(200, json=body))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.parametrize(
    "usage",
    [["seconds", 1.0], "1.0", {"cost": "free"}, {"cost": [3.3e-06]}, {"seconds": {"value": 1.0}}, {"input_tokens": "many"}, {"output_tokens": None, "total_tokens": "12"}],
)
@pytest.mark.asyncio
async def test_a_wrongly_typed_usage_block_is_malformed_rather_than_a_crash(stt_provider, usage):
    provider, _ = stt_provider(lambda request: httpx.Response(200, json={"text": "book the meeting", "usage": usage}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.asyncio
async def test_a_null_text_and_a_missing_usage_block_still_produce_a_final_event(stt_provider):
    provider, _ = stt_provider(lambda request: httpx.Response(200, json={"text": None}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    event = await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert event.stte_kind == "final"
    assert event.stte_text == ""
    assert event.stte_usage.sttu_cost_usd is None
    assert event.stte_usage.sttu_total_tokens is None


@pytest.mark.parametrize(
    "usage",
    [
        {"cost": True},
        {"seconds": False},
        {"total_tokens": True},
        {"input_tokens": 1.9},
        {"output_tokens": 0.5},
        {"cost": -1.0},
        {"seconds": -2.0},
        {"total_tokens": -3},
    ],
)
@pytest.mark.asyncio
async def test_booleans_fractional_tokens_and_negative_usage_are_malformed(stt_provider, usage):
    provider, _ = stt_provider(lambda request: httpx.Response(200, json={"text": "book the meeting", "usage": usage}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.asyncio
async def test_non_finite_usage_numbers_are_malformed(stt_provider, literal):
    body = ('{"text": "book the meeting", "usage": {"cost": %s, "seconds": 1.0}}' % literal).encode()
    provider, _ = stt_provider(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.asyncio
async def test_zero_usage_values_are_valid_and_survive_onto_the_event(stt_provider):
    usage = {"cost": 0.0, "seconds": 0.0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    provider, _ = stt_provider(lambda request: transcription_response("book the meeting", usage))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    event = await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert event.stte_usage.sttu_cost_usd == 0.0
    assert event.stte_usage.sttu_audio_seconds == 0.0
    assert event.stte_usage.sttu_total_tokens == 0


@pytest.mark.parametrize("field", ["cost", "seconds", "total_tokens"])
@pytest.mark.asyncio
async def test_a_usage_integer_too_large_for_a_float_is_malformed_rather_than_an_overflow(stt_provider, field):
    body = ('{"text": "book the meeting", "usage": {"%s": %d}}' % (field, 10**400)).encode()
    provider, _ = stt_provider(lambda request: httpx.Response(200, content=body, headers={"content-type": "application/json"}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"


@pytest.mark.asyncio
async def test_a_usage_value_above_the_plausible_bound_is_malformed(stt_provider):
    provider, _ = stt_provider(lambda request: transcription_response("ok", {"cost": voice_stt.USAGE_VALUE_MAX * 2}))
    config = voice_stt.SttConfig(sttc_model="m", sttc_language=None, sttc_sample_rate_hz=STT_RATE, sttc_deadline_s=15.0)

    with pytest.raises(voice_stt.SttError) as raised:
        await provider.transcribe_utterance(one_second_of_pcm(), config)
    await provider.aclose()

    assert raised.value.sterr_kind == "malformed_response"
