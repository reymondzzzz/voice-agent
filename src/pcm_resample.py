from __future__ import annotations

import math

import numpy
from scipy import signal

import contracts

RESAMPLE_FILTER_TAPS = 65
RESAMPLE_STOPBAND_MARGIN = 0.9


class PcmResampleError(ValueError):
    pass


class PcmResampler:
    def __init__(self, vsource_rate_hz: int, vtarget_rate_hz: int) -> None:
        if vsource_rate_hz <= 0 or vtarget_rate_hz <= 0:
            raise PcmResampleError("pcm resample rates must be positive")
        if vsource_rate_hz < vtarget_rate_hz:
            raise PcmResampleError("pcm resampler only downsamples")
        self._vstep = vsource_rate_hz / vtarget_rate_hz
        vcutoff = RESAMPLE_STOPBAND_MARGIN * vtarget_rate_hz / vsource_rate_hz
        self._vtaps = signal.firwin(RESAMPLE_FILTER_TAPS, vcutoff)
        self._vfilter_state = numpy.zeros(RESAMPLE_FILTER_TAPS - 1)
        self._vcarry = numpy.zeros(0)
        self._vbyte_carry = b""
        self._vphase = 0.0

    def process(self, vpcm: bytes) -> bytes:
        vbuffered = self._vbyte_carry + vpcm
        valigned_bytes = len(vbuffered) // 2 * 2
        self._vbyte_carry = vbuffered[valigned_bytes:]
        if not valigned_bytes:
            return b""
        vsamples = numpy.frombuffer(vbuffered[:valigned_bytes], dtype="<i2").astype(numpy.float64)
        vfiltered, self._vfilter_state = signal.lfilter(self._vtaps, 1.0, vsamples, zi=self._vfilter_state)
        return self._interpolate(vfiltered)

    def flush(self) -> bytes:
        vtail = numpy.zeros(RESAMPLE_FILTER_TAPS - 1)
        vfiltered, self._vfilter_state = signal.lfilter(self._vtaps, 1.0, vtail, zi=self._vfilter_state)
        vpcm = self._interpolate(vfiltered)
        self._vcarry = numpy.zeros(0)
        self._vbyte_carry = b""
        self._vphase = 0.0
        return vpcm

    def _interpolate(self, vfiltered) -> bytes:
        vavailable = numpy.concatenate((self._vcarry, vfiltered))
        if vavailable.size < 2:
            self._vcarry = vavailable
            return b""
        vcount = int(math.floor((vavailable.size - 1 - self._vphase) / self._vstep)) + 1
        if vcount <= 0:
            self._vcarry = vavailable
            return b""
        vpositions = self._vphase + self._vstep * numpy.arange(vcount)
        vleft = numpy.floor(vpositions).astype(numpy.int64)
        vfraction = vpositions - vleft
        vresampled = vavailable[vleft] * (1.0 - vfraction) + vavailable[vleft + 1] * vfraction
        vconsumed = int(vleft[-1])
        self._vcarry = vavailable[vconsumed:]
        self._vphase = vpositions[-1] + self._vstep - vconsumed
        return numpy.clip(numpy.rint(vresampled), -32768, 32767).astype("<i2").tobytes()


def resampler_for(vsource_rate_hz: int) -> PcmResampler | None:
    if vsource_rate_hz == contracts.VOICE_TTS_SAMPLE_RATE_HZ:
        return None
    return PcmResampler(vsource_rate_hz, contracts.VOICE_TTS_SAMPLE_RATE_HZ)
