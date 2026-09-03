import asyncio

import pytest

from voice_agent.pipeline import voice_tts_prefetch

SEGMENTS = ("first.", "second.", "third.", "fourth.")


class _Recorder:
    def __init__(self, vfail_on: str | None = None) -> None:
        self.vopened: list[str] = []
        self.vclosed: list[str] = []
        self.vfail_on = vfail_on

    async def vopen(self, vsegment: str) -> str:
        self.vopened.append(vsegment)
        if vsegment == self.vfail_on:
            raise RuntimeError("provider refused")
        return vsegment

    async def vclose(self, vstream) -> None:
        self.vclosed.append(vstream)


def _prefetcher(vrecorder: _Recorder, vdepth: int = 2) -> voice_tts_prefetch.VoiceTtsPrefetcher:
    return voice_tts_prefetch.VoiceTtsPrefetcher(vrecorder.vopen, vrecorder.vclose, vdepth)


@pytest.mark.asyncio
async def test_it_opens_at_most_the_configured_depth_ahead() -> None:
    vrecorder = _Recorder()
    vprefetcher = _prefetcher(vrecorder)

    vprefetcher.prefetch(SEGMENTS, True)
    await asyncio.sleep(0)

    assert vrecorder.vopened == ["first.", "second."]


@pytest.mark.asyncio
async def test_a_taken_segment_reuses_its_prefetched_stream() -> None:
    vrecorder = _Recorder()
    vprefetcher = _prefetcher(vrecorder)

    vprefetcher.prefetch(SEGMENTS, True)
    assert await vprefetcher.take(0, "first.") == "first."
    vprefetcher.prefetch(SEGMENTS, True)
    assert await vprefetcher.take(1, "second.") == "second."
    await asyncio.sleep(0)

    assert vrecorder.vopened == ["first.", "second.", "third."]


@pytest.mark.asyncio
async def test_prefetching_stops_while_it_is_not_allowed() -> None:
    vrecorder = _Recorder()
    vprefetcher = _prefetcher(vrecorder)

    vprefetcher.prefetch(SEGMENTS, False)
    await asyncio.sleep(0)

    assert vrecorder.vopened == []


@pytest.mark.asyncio
async def test_discard_closes_every_stream_it_opened_exactly_once() -> None:
    vrecorder = _Recorder()
    vprefetcher = _prefetcher(vrecorder)

    vprefetcher.prefetch(SEGMENTS, True)
    await asyncio.sleep(0)
    await vprefetcher.discard()

    assert sorted(vrecorder.vclosed) == ["first.", "second."]


@pytest.mark.asyncio
async def test_a_failed_prefetch_is_swallowed_by_discard_but_reported_when_taken() -> None:
    vrecorder = _Recorder(vfail_on="second.")
    vprefetcher = _prefetcher(vrecorder)

    vprefetcher.prefetch(SEGMENTS, True)
    await asyncio.sleep(0)
    await vprefetcher.discard()

    assert vrecorder.vclosed == ["first."]

    vtaking = _prefetcher(_Recorder(vfail_on="first."))
    with pytest.raises(RuntimeError, match="provider refused"):
        await vtaking.take(0, "first.")


def test_a_non_positive_depth_is_refused() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        voice_tts_prefetch.VoiceTtsPrefetcher(_Recorder().vopen, _Recorder().vclose, 0)
