from __future__ import annotations

import math
import struct

import pytest

import contracts
import pcm_resample


def sine_pcm(vsample_rate_hz: int, vseconds: float, vfrequency_hz: float = 220.0) -> bytes:
    vcount = int(vsample_rate_hz * vseconds)
    vsamples = (int(12000 * math.sin(2 * math.pi * vfrequency_hz * vindex / vsample_rate_hz)) for vindex in range(vcount))
    return struct.pack(f"<{vcount}h", *vsamples)


def test_no_resampler_is_built_at_the_room_rate():
    assert pcm_resample.resampler_for(contracts.VOICE_TTS_SAMPLE_RATE_HZ) is None


def test_downsampling_preserves_duration():
    vpcm44 = sine_pcm(44100, 1.0)
    vresampler = pcm_resample.resampler_for(44100)
    vpcm24 = vresampler.process(vpcm44) + vresampler.flush()
    assert contracts.pcm_duration_seconds(len(vpcm24), 24000) == pytest.approx(1.0, abs=0.01)


def test_chunked_input_matches_a_single_pass():
    vpcm = sine_pcm(44100, 0.25)
    vwhole = pcm_resample.resampler_for(44100)
    vexpected = vwhole.process(vpcm) + vwhole.flush()

    vchunked = pcm_resample.resampler_for(44100)
    vactual = b"".join(vchunked.process(vpcm[vstart : vstart + 4096]) for vstart in range(0, len(vpcm), 4096))
    vactual += vchunked.flush()
    assert vactual == vexpected


def test_odd_byte_boundaries_do_not_corrupt_the_stream():
    vpcm = sine_pcm(44100, 0.2)
    vresampler = pcm_resample.resampler_for(44100)
    vout = b"".join(vresampler.process(vpcm[vstart : vstart + 1023]) for vstart in range(0, len(vpcm), 1023))
    vout += vresampler.flush()
    assert len(vout) % 2 == 0
    assert contracts.pcm_duration_seconds(len(vout), 24000) == pytest.approx(0.2, abs=0.01)


def test_upsampling_is_refused():
    with pytest.raises(pcm_resample.PcmResampleError, match="only downsamples"):
        pcm_resample.PcmResampler(24000, 44100)


def test_non_positive_rates_are_refused():
    with pytest.raises(pcm_resample.PcmResampleError, match="positive"):
        pcm_resample.PcmResampler(0, 24000)
