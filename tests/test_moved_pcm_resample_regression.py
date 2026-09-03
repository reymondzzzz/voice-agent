from __future__ import annotations

import struct

import numpy
import pytest

from voice_agent.pipeline import voice_pcm_resample


def constant_pcm(vsamples: int, vvalue: int = 1000) -> bytes:
    return struct.pack(f"<{vsamples}h", *([vvalue] * vsamples))


@pytest.mark.parametrize("vchunk_samples", [37, 74, 111, 148])
def test_chunk_sizes_that_land_on_an_exact_sample_boundary_do_not_overrun(vchunk_samples):
    vresampler = voice_pcm_resample.resampler_for(44100)
    vout = b"".join(vresampler.process(constant_pcm(vchunk_samples)) for _ in range(8))
    vout += vresampler.flush()
    assert len(vout) % 2 == 0


def test_the_clamped_right_index_only_applies_where_the_fraction_is_zero():
    vstep = 44100 / 24000
    for vsize in range(2, 400):
        vphase = 0.0
        vcount = int(numpy.floor((vsize - 1 - vphase) / vstep)) + 1
        vpositions = vphase + vstep * numpy.arange(vcount)
        vleft = numpy.floor(vpositions).astype(numpy.int64)
        voverrun = vleft + 1 > vsize - 1
        assert numpy.all((vpositions - vleft)[voverrun] == 0.0)


def test_a_constant_signal_stays_constant_through_the_clamp():
    vresampler = voice_pcm_resample.resampler_for(44100)
    vout = b"".join(vresampler.process(constant_pcm(37)) for _ in range(40))
    vsamples = numpy.frombuffer(vout, dtype="<i2")
    assert vsamples.size > 0
    assert vsamples[-20:].max() <= 1000
