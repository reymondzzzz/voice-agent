from __future__ import annotations

import argparse
import asyncio
import base64
import logging
import os
import pathlib
import re
import secrets

from livekit import api, rtc
from playwright.async_api import Page, async_playwright

from examples import voice_app

logger = logging.getLogger("meet-bridge")

MEET_DIR = pathlib.Path(__file__).resolve().parent / "meet"
MEET_SAMPLE_RATE_HZ = 48000
MEET_FRAME_SAMPLES = 960
AGENT_FRAME_MS = 20
BRIDGE_IDENTITY = "meet-bridge"
MEET_SPEAKER_ATTRIBUTE = "meet_speaker"
MEET_BOT_NAME_ATTRIBUTE = "meet_bot_name"
ADMISSION_TIMEOUT_S = 300
IN_CALL_SELECTOR = "[data-participant-id], [data-self-name]"
JOIN_BUTTON_NAME = re.compile(r"^(Ask to join|Ask to join anyway|Join now|Join anyway)$")
REFUSED_TEXT = re.compile(r"denied your request|No one (has )?responded|You can.t join this video call|Check your meeting code|Invalid video call name")
# Meet localises the lobby by Accept-Language and fingerprints automation flags at join time.
CHROME_ARGS = [
    "--autoplay-policy=no-user-gesture-required",
    "--use-fake-ui-for-media-stream",
    "--use-fake-device-for-media-stream",
    "--lang=en-US",
    "--disable-blink-features=AutomationControlled",
]


class MeetJoinRefused(RuntimeError):
    pass


def mint_bridge_token(vroom_name: str) -> str:
    return (
        api.AccessToken(os.environ["LIVEKIT_API_KEY"], os.environ["LIVEKIT_API_SECRET"])
        .with_identity(BRIDGE_IDENTITY)
        .with_name("Google Meet")
        .with_grants(api.VideoGrants(room_join=True, room=vroom_name, can_update_own_metadata=True))
        .to_jwt()
    )


def meet_speaker(vnames: list[str], vbot_name: str) -> str:
    return ", ".join(vname for vname in vnames if vname != vbot_name)


async def pump_agent_audio(vtrack: rtc.Track, vpage: Page, vjoined: asyncio.Event) -> None:
    async for vevent in rtc.AudioStream.from_track(track=vtrack, sample_rate=MEET_SAMPLE_RATE_HZ, num_channels=1, frame_size_ms=AGENT_FRAME_MS):
        if not vjoined.is_set():
            continue
        vpcm = base64.b64encode(bytes(vevent.frame.data)).decode()
        await vpage.evaluate("vpcm => window.vmeetPlay(vpcm)", vpcm)


async def join_meet(vpage: Page, vmeet_url: str, vbot_name: str) -> None:
    await vpage.goto(vmeet_url)
    vjoin = vpage.get_by_role("button", name=JOIN_BUTTON_NAME).first
    await vjoin.wait_for(timeout=30_000)
    # Only an anonymous guest is asked for a name; a signed-in profile joins under its account name.
    vname_input = vpage.locator('input[type="text"][aria-label="Your name"]')
    if await vname_input.count():
        await vname_input.press_sequentially(vbot_name, delay=60)
    vcamera = vpage.locator('[aria-label="Turn off camera"]')
    if await vcamera.count():
        await vcamera.first.click()
    await vjoin.click()
    logger.info("asked to join, waiting for the host to admit %s", vbot_name)
    for _ in range(ADMISSION_TIMEOUT_S):
        if await vpage.locator(IN_CALL_SELECTOR).count():
            return
        vrefusal = REFUSED_TEXT.search(await vpage.locator("body").inner_text())
        if vrefusal:
            raise MeetJoinRefused(vrefusal.group(0))
        await asyncio.sleep(1)
    raise MeetJoinRefused(f"not admitted within {ADMISSION_TIMEOUT_S}s")


async def wait_until_call_ends(vpage: Page) -> None:
    await vpage.wait_for_selector(IN_CALL_SELECTOR, state="detached", timeout=0)


async def run_bridge(vmeet_url: str, vroom_name: str, vbot_name: str, *, vheadless: bool, vprofile: pathlib.Path | None = None) -> None:
    voice_app.mirror_flexus_livekit_env()
    vroom = rtc.Room()
    vsource = rtc.AudioSource(MEET_SAMPLE_RATE_HZ, 1)
    vjoined = asyncio.Event()
    vlast_speaker = ""

    async def on_capture(vpcm: str) -> None:
        await vsource.capture_frame(rtc.AudioFrame(base64.b64decode(vpcm), MEET_SAMPLE_RATE_HZ, 1, MEET_FRAME_SAMPLES))

    async def on_speakers(vnames: list[str]) -> None:
        nonlocal vlast_speaker
        vspeaker = meet_speaker(vnames, vbot_name)
        if not vspeaker or vspeaker == vlast_speaker:
            return
        vlast_speaker = vspeaker
        logger.info("meet speaker=%s", vspeaker)
        await vroom.local_participant.set_attributes({MEET_SPEAKER_ATTRIBUTE: vspeaker})

    async with async_playwright() as vplaywright:
        if vprofile is None:
            vbrowser = await vplaywright.chromium.launch(channel="chrome", headless=vheadless, args=CHROME_ARGS, ignore_default_args=["--enable-automation"])
            vcontext = await vbrowser.new_context(locale="en-US", permissions=["microphone", "camera"])
        else:
            # Playwright's mock keychain cannot decrypt cookies the signed-in Chrome stored, and Chrome deletes what it
            # cannot decrypt: one run with it signs the profile out for good.
            vcontext = await vplaywright.chromium.launch_persistent_context(
                str(vprofile), channel="chrome", headless=vheadless, args=CHROME_ARGS, ignore_default_args=["--enable-automation", "--use-mock-keychain", "--password-store=basic"],
                locale="en-US", permissions=["microphone", "camera"],
            )
            vbrowser = vcontext
        await vcontext.expose_function("vmeetCapture", on_capture)
        await vcontext.expose_function("vmeetSpeakers", on_speakers)
        await vcontext.add_init_script(path=MEET_DIR / "bridge.js")
        vpage = await vcontext.new_page()
        vpumps: set[asyncio.Task[None]] = set()

        def on_track_subscribed(vtrack: rtc.Track, _vpublication: rtc.RemoteTrackPublication, vparticipant: rtc.RemoteParticipant) -> None:
            if vtrack.kind != rtc.TrackKind.KIND_AUDIO:
                return
            logger.info("playing %s into meet", vparticipant.identity)
            vpump = asyncio.create_task(pump_agent_audio(vtrack, vpage, vjoined))
            vpumps.add(vpump)
            vpump.add_done_callback(vpumps.discard)

        vroom.on("track_subscribed", on_track_subscribed)
        await vroom.connect(os.environ["LIVEKIT_URL"], mint_bridge_token(vroom_name))
        await vroom.local_participant.publish_track(
            rtc.LocalAudioTrack.create_audio_track("meet-audio", vsource),
            rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
        )
        await vroom.local_participant.set_attributes({MEET_BOT_NAME_ATTRIBUTE: vbot_name})
        logger.info("bridge joined room=%s", vroom_name)
        try:
            await join_meet(vpage, vmeet_url, vbot_name)
            vjoined.set()
            logger.info("admitted to %s", vmeet_url)
            await wait_until_call_ends(vpage)
            logger.info("call ended")
        finally:
            for vpump in vpumps:
                vpump.cancel()
            await vbrowser.close()
            await vroom.disconnect()


def main() -> None:
    vparser = argparse.ArgumentParser(description="Bridge a Google Meet call into a LiveKit room the example agent serves.")
    vparser.add_argument("meet_url")
    vparser.add_argument("--room", default=f"voice-meet-{secrets.token_hex(3)}")
    vparser.add_argument("--name", default="Karen")
    vparser.add_argument("--headless", action="store_true")
    vparser.add_argument("--profile", type=pathlib.Path, help="Chrome profile directory signed in to a Google account; Meet turns away anonymous automated guests")
    vargs = vparser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_bridge(vargs.meet_url, vargs.room, vargs.name, vheadless=vargs.headless, vprofile=vargs.profile))


if __name__ == "__main__":
    main()
