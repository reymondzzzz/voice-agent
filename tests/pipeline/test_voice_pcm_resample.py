import numpy
import pytest

from voice_agent.pipeline import voice_contracts
from voice_agent.pipeline import voice_pcm_resample

SOURCE_RATE_HZ = 44100


def _tone(vfrequency_hz: float, vseconds: float, vrate_hz: int) -> bytes:
    vtime = numpy.arange(int(vrate_hz * vseconds)) / vrate_hz
    return (numpy.sin(2 * numpy.pi * vfrequency_hz * vtime) * 12000).astype("<i2").tobytes()


def _resample_in_chunks(vpcm: bytes, vchunk_bytes: int) -> bytes:
    vresampler = voice_pcm_resample.PcmResampler(SOURCE_RATE_HZ, voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ)
    vout = bytearray()
    for vindex in range(0, len(vpcm), vchunk_bytes):
        vout += vresampler.process(vpcm[vindex : vindex + vchunk_bytes])
    vout += vresampler.flush()
    return bytes(vout)


def _dominant_frequency_hz(vpcm: bytes, vrate_hz: int) -> float:
    vsamples = numpy.frombuffer(vpcm, dtype="<i2").astype(float)
    vspectrum = numpy.abs(numpy.fft.rfft(vsamples * numpy.hanning(len(vsamples))))
    return float(numpy.fft.rfftfreq(len(vsamples), 1 / vrate_hz)[int(numpy.argmax(vspectrum))])


def test_one_second_of_audio_resamples_to_the_room_rate() -> None:
    vout = _resample_in_chunks(_tone(440, 1.0, SOURCE_RATE_HZ), 4096)

    vsamples = len(vout) // 2
    assert abs(vsamples - voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ) < voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ * 0.01


def test_resampling_preserves_pitch() -> None:
    vout = _resample_in_chunks(_tone(440, 1.0, SOURCE_RATE_HZ), 4096)

    assert abs(_dominant_frequency_hz(vout, voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ) - 440) < 5


def test_content_above_the_target_nyquist_is_attenuated_instead_of_aliased() -> None:
    vout = _resample_in_chunks(_tone(18000, 0.5, SOURCE_RATE_HZ), 4096)

    vpeak = numpy.abs(numpy.frombuffer(vout, dtype="<i2").astype(float)).max()
    assert vpeak < 12000 * 0.2


def test_output_is_identical_regardless_of_how_the_stream_is_chunked() -> None:
    vpcm = _tone(1000, 0.3, SOURCE_RATE_HZ)

    assert _resample_in_chunks(vpcm, 8192) == _resample_in_chunks(vpcm, 733)


def test_an_odd_byte_split_never_loses_or_misaligns_a_sample() -> None:
    vpcm = _tone(1000, 0.2, SOURCE_RATE_HZ)

    vout = _resample_in_chunks(vpcm, 1)

    assert len(vout) % 2 == 0
    assert vout == _resample_in_chunks(vpcm, 4096)


def test_a_matching_rate_needs_no_resampler() -> None:
    assert voice_pcm_resample.resampler_for(voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ) is None
    assert voice_pcm_resample.resampler_for(SOURCE_RATE_HZ) is not None


def test_upsampling_is_refused() -> None:
    with pytest.raises(voice_pcm_resample.PcmResampleError, match="only downsamples"):
        voice_pcm_resample.PcmResampler(16000, voice_contracts.VOICE_TTS_SAMPLE_RATE_HZ)
