from __future__ import annotations

import asyncio
import os
import pathlib

import numpy as np
import pytest
from livekit import rtc

from examples import meet_bridge, token_server, voice_app

FIXTURE_URL = (pathlib.Path(__file__).resolve().parent / "meet_fixture.html").as_uri()
ROOM = "voice-meet-bridge-test"
MEET_TONE_HZ = 440
AGENT_TONE_HZ = 660
AGENT_RATE_HZ = 24000


def test_meet_speaker_drops_the_bot_and_joins_overlapping_speakers() -> None:
    assert meet_bridge.meet_speaker(["Voice Agent", "Anna", "Bob"], "Voice Agent") == "Anna, Bob"
    assert meet_bridge.meet_speaker(["Voice Agent"], "Voice Agent") == ""
    assert meet_bridge.meet_speaker(["Kirill Starkov", "Kirill Starkov"], "Karen") == "Kirill Starkov"


def test_meet_opens_in_english_whatever_the_account_language() -> None:
    assert meet_bridge.english_meet_url("https://meet.google.com/pte-bavf-guh") == "https://meet.google.com/pte-bavf-guh?hl=en"
    assert meet_bridge.english_meet_url("https://meet.google.com/abc?authuser=1&hl=ru") == "https://meet.google.com/abc?authuser=1&hl=en"


def test_meet_audio_is_republished_without_dtx_at_a_high_bitrate() -> None:
    voptions = meet_bridge.meet_track_options()
    assert voptions.dtx is False
    assert voptions.audio_encoding.max_bitrate == meet_bridge.MEET_PUBLISH_BITRATE == 64_000


def peak_hz(vpcm: bytes) -> float:
    vsamples = np.frombuffer(vpcm, np.int16).astype(float)
    vspectrum = np.abs(np.fft.rfft(vsamples * np.hanning(len(vsamples))))
    return (int(np.argmax(vspectrum[1:])) + 1) * meet_bridge.MEET_SAMPLE_RATE_HZ / len(vsamples)


class FakeAgent:
    def __init__(self) -> None:
        self.vroom = rtc.Room()
        self.vheard_hz = 0.0
        self.vspeaker = ""
        self.vdone = asyncio.Event()
        self._vtasks: set[asyncio.Task[None]] = set()

    def spawn(self, vcoro) -> None:
        vtask = asyncio.create_task(vcoro)
        self._vtasks.add(vtask)
        vtask.add_done_callback(self._vtasks.discard)

    async def listen(self, vtrack: rtc.Track) -> None:
        vpcm = b""
        async for vevent in rtc.AudioStream.from_track(track=vtrack, sample_rate=meet_bridge.MEET_SAMPLE_RATE_HZ, num_channels=1):
            vpcm += bytes(vevent.frame.data)
            if len(vpcm) >= meet_bridge.MEET_SAMPLE_RATE_HZ * 2 * 3:
                self.vheard_hz = peak_hz(vpcm[-meet_bridge.MEET_SAMPLE_RATE_HZ * 2 :])
                return

    async def speak(self, vsource: rtc.AudioSource) -> None:
        vsample = 0
        while True:
            vtimes = np.arange(vsample, vsample + 240) / AGENT_RATE_HZ
            vsample += 240
            vtone = (np.sin(2 * np.pi * AGENT_TONE_HZ * vtimes) * 8000).astype(np.int16)
            await vsource.capture_frame(rtc.AudioFrame(vtone.tobytes(), AGENT_RATE_HZ, 1, 240))

    async def run(self) -> None:
        self.vroom.on("track_subscribed", lambda vtrack, *_: self.spawn(self.listen(vtrack)))
        await self.vroom.connect(os.environ["LIVEKIT_URL"], token_server.mint_caller_token(ROOM, "fake-agent"))
        vsource = rtc.AudioSource(AGENT_RATE_HZ, 1)
        await self.vroom.local_participant.publish_track(rtc.LocalAudioTrack.create_audio_track("agent", vsource), rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self.spawn(self.speak(vsource))
        while not (self.vheard_hz and self.vspeaker):
            await asyncio.sleep(0.1)
            self.vspeaker = next((vp.attributes.get(meet_bridge.MEET_SPEAKER_ATTRIBUTE, "") for vp in self.vroom.remote_participants.values()), "")
        self.vdone.set()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bridge_carries_audio_both_ways_and_names_the_speaker(monkeypatch: pytest.MonkeyPatch) -> None:
    voice_app.mirror_flexus_livekit_env()
    vagent = FakeAgent()
    vmicrophone_hz: list[float] = []

    async def open_fixture(vpage, _vmeet_url: str, _vbot_name: str) -> None:
        await vpage.goto(FIXTURE_URL)

    async def wait_for_agent(vpage) -> None:
        await asyncio.wait_for(vagent.vdone.wait(), 30)
        vmicrophone_hz.append(await vpage.evaluate("window.microphonePeakHz()"))

    monkeypatch.setattr(meet_bridge, "join_meet", open_fixture)
    monkeypatch.setattr(meet_bridge, "wait_until_call_ends", wait_for_agent)
    vagent_run = asyncio.create_task(vagent.run())
    await meet_bridge.run_bridge(FIXTURE_URL, ROOM, "Voice Agent", vheadless=True)
    await vagent_run
    await vagent.vroom.disconnect()

    assert abs(vagent.vheard_hz - MEET_TONE_HZ) < 5
    assert vagent.vspeaker == "Anna"
    assert abs(vmicrophone_hz[0] - AGENT_TONE_HZ) < 10
