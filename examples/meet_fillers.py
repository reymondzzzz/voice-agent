from __future__ import annotations

import asyncio
import base64
import enum
import json
import os
import pathlib
import random
import wave
from collections import deque

import aiohttp
import numpy as np
from dotenv import load_dotenv

from voice_agent.realtime.qwen import protocol

FILLER_DIR = pathlib.Path(__file__).parent / "meet" / "fillers"
FILLER_SILENCE_DBFS = -45.0
FILLER_RECENT = 3


class FillerKind(enum.Enum):
    THINKING = "thinking"
    CHECKING = "checking"


FILLER_LINES = {
    FillerKind.THINKING: {"hm": "Хмм…", "tak": "Так…", "aga": "Ага…", "nu": "Ну…", "o": "О…"},
    FillerKind.CHECKING: {"sekundu": "Секунду…", "glyanu": "Сейчас гляну…", "smotryu": "Так, смотрю…", "minutku": "Минутку…", "posmotryu": "Щас посмотрю…"},
}


class FillerDeck:
    """Clips in her own voice for the silence before an answer; never one of the last few, so they do not repeat."""

    def __init__(self, vclips: dict[FillerKind, list[bytes]]) -> None:
        self.vclips = vclips
        self.vrecent: deque[bytes] = deque(maxlen=FILLER_RECENT)

    def pick(self, vkind: FillerKind) -> bytes:
        vfresh = [vclip for vclip in self.vclips[vkind] if vclip not in self.vrecent] or self.vclips[vkind]
        vclip = random.choice(vfresh)
        self.vrecent.append(vclip)
        return vclip


def load_filler_deck() -> FillerDeck:
    vclips: dict[FillerKind, list[bytes]] = {}
    for vkind, vlines in FILLER_LINES.items():
        vclips[vkind] = []
        for vname in vlines:
            with wave.open(str(FILLER_DIR / f"{vkind.value}-{vname}.wav")) as vwav:
                vclips[vkind].append(vwav.readframes(vwav.getnframes()))
    return FillerDeck(vclips)


def trim_silence(vpcm: bytes) -> bytes:
    vsamples = np.frombuffer(vpcm, np.int16)
    vwindow = protocol.OUTPUT_SAMPLE_RATE_HZ // 100
    vframes = vsamples[: len(vsamples) // vwindow * vwindow].reshape(-1, vwindow).astype(np.float32) / 32768
    vloud = np.flatnonzero(20 * np.log10(np.sqrt(np.mean(vframes**2, axis=1)) + 1e-9) > FILLER_SILENCE_DBFS)
    if not len(vloud):
        return b""
    return vsamples[vloud[0] * vwindow : (vloud[-1] + 2) * vwindow].tobytes()


async def record_fillers() -> None:
    load_dotenv(".env.local")
    FILLER_DIR.mkdir(parents=True, exist_ok=True)
    async with aiohttp.ClientSession() as vhttp:
        vws = await vhttp.ws_connect(
            protocol.connection_url(protocol.SINGAPORE_WS_URL, "qwen3.5-omni-plus-realtime"),
            headers={"Authorization": f"Bearer {os.environ['DASHSCOPE_API_KEY']}"},
            max_msg_size=0,
        )
        await vws.send_str(json.dumps(protocol.session_update_frame(protocol.DEFAULT_VOICE, "Ты озвучиваешь короткие реплики.", [], vsilence_ms=900, vauto_response=False)))
        for vkind, vlines in FILLER_LINES.items():
            for vname, vline in vlines.items():
                await vws.send_str(json.dumps({"type": "response.create", "response": {"modalities": ["text", "audio"], "instructions": f"Произнеси только «{vline}», тихо и естественно, как человек, который задумался. Больше ничего."}}))
                vaudio = bytearray()
                async for vmessage in vws:
                    vframe = json.loads(vmessage.data)
                    if vframe.get("type") == "response.audio.delta":
                        vaudio += base64.b64decode(vframe["delta"])
                    elif vframe.get("type") in ("response.done", "error"):
                        break
                with wave.open(str(FILLER_DIR / f"{vkind.value}-{vname}.wav"), "wb") as vwav:
                    vwav.setnchannels(1)
                    vwav.setsampwidth(2)
                    vwav.setframerate(protocol.OUTPUT_SAMPLE_RATE_HZ)
                    vwav.writeframes(trim_silence(bytes(vaudio)))
        await vws.close()


if __name__ == "__main__":
    asyncio.run(record_fillers())
