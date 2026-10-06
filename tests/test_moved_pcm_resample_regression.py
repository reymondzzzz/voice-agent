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


def test_a_constant_signal_stays_constant_through_the_clamp():
    vresampler = voice_pcm_resample.resampler_for(44100)
    vout = b"".join(vresampler.process(constant_pcm(37)) for _ in range(40))
    vsamples = numpy.frombuffer(vout, dtype="<i2")
    assert vsamples.size > 0
    assert vsamples[-20:].max() <= 1000
