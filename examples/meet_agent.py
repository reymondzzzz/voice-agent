from __future__ import annotations

import asyncio
import collections
import contextlib
import dataclasses
import json
import logging
import os
import pathlib
import random
import time

import numpy
from livekit import agents, rtc
from livekit.agents import AgentServer, JobContext

from examples import voice_app
from examples.meet_addressing import MeetAddressing
from examples.meet_bridge import MEET_BOT_NAME_ATTRIBUTE, MEET_SPEAKER_ATTRIBUTE
from examples.meet_memory import MEET_CONTEXT_WINDOW_S, MeetMemory, MeetRole, MeetTurn
from examples.meet_tools import MEET_TOOLS, MEET_TOOLS_BY_NAME, SCIENCE_FACTS, MeetTool, ToolWeight
from voice_agent.pipeline import voice_contracts
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.realtime import events
from voice_agent.realtime.qwen.session import QwenOmniSession

logger = logging.getLogger("meet-agent")

MEET_VOICE_MODEL = "qwen3.8-omni-flash-realtime"
MEET_DELEGATE_MODEL = "z-ai/glm-5.3"
MEET_LANGUAGE = "ru"
MEET_LANGUAGE_NAME = "Russian"
BARGE_IN_DBFS = -40.0
BARGE_IN_S = 0.15
MEET_DEFAULT_BOT_NAME = "Karen"
MEET_TRANSCRIPT_DIR = pathlib.Path("meet-transcripts")
ROOM_SAMPLE_RATE_HZ = voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
PLAYBACK_QUEUE_MS = 20_000
QUIET_BEFORE_SPEAKING_S = 1.5
QUIET_BEFORE_DELIVERY_S = 3.0
QUIET_POLL_S = 0.25
TURN_SILENCE_MS = 1200
GATE_CONTEXT_TURNS = 12
DELIVERY_ATTEMPTS = 2
DELIVERY_PLAYOUT_TIMEOUT_S = 60.0
BOT_SETTLE_S = 0.5
ROUTE_TIMEOUT_S = 5.0
FLOOR_TIMEOUT_S = 10.0
KEEPALIVE_S = 240.0
FILLER_PHRASES = ("Секунду.", "Так...", "Сейчас посмотрю.", "Хм, сейчас.")
KAREN_EVENTS_TOPIC = "karen"
KAREN_RULES = (
    "You are an assistant attending a group meeting by voice. You hear everyone, but almost everything is said "
    "between the participants and is not for you. Reply only to the single line addressed to you, which is the "
    "last message; never answer or act on anything else you heard, though you may use it as context. Answer in "
    "one or two short spoken sentences. You know nothing about the current time or weather: call the tool for "
    "either before answering. A science fact must come from science_fact, never from your own knowledge. For "
    "anything that needs research, analysis, drafting or careful checking beyond what was said, call research "
    "with a self-contained question. A tool that runs in the background returns at once: then say in a few words "
    "that you are on it. Only say that you started, are running or will return with work if you called a tool for "
    "it in this reply or it is listed below as running; otherwise say you have not started anything. Never guess "
    "a result that has not arrived. A short filler such as 'Секунду' has already been said before your reply, so "
    "start with the substance, not with another filler. Speak like a colleague in the room: brief, warm and "
    "plain, no announcements about yourself or your tools. When you bring back a background result, open with a "
    "few words that tie it to the question, the way a person would say 'about the deadline, ...'. Sometimes you "
    "are asked an internal routing question about who a line was meant for: answer it with the single word asked "
    "for, and never say RESPOND or IGNORE aloud."
)

@dataclasses.dataclass
class PendingResult:
    vrequester: str
    vgoal: str
    vanswer: str
    vattempts: int = 0


def delivery_line(vresults: list[PendingResult]) -> str:
    vresumed = any(vresult.vattempts for vresult in vresults)
    vitems = "\n".join(f"- for {vresult.vrequester}, who asked: {vresult.vgoal}: {vresult.vanswer}" for vresult in vresults)
    if vresumed:
        vhow = (
            "You were cut off while telling this. It is quiet now, and anything you were asked in between is already "
            "answered: pick the thread back up the way a person does ('so, about ...') and tell all of it."
        )
    else:
        vhow = "Tell all of it now, in one go."
    return (
        f"[background results ready]\n{vitems}\n{vhow} Keep talking from one item to the next without stopping or "
        f"asking whether to go on, a sentence or two for each, addressing each person by name."
    )


def loudness_dbfs(vpcm: bytes) -> float:
    vsamples = numpy.frombuffer(vpcm, dtype="<i2").astype(numpy.float32) / 32768.0
    if not vsamples.size:
        return -120.0
    return float(20 * numpy.log10(numpy.sqrt(numpy.mean(vsamples * vsamples)) + 1e-6))


def reply_latency(vtrace: dict[str, float], vfirst_audio_at: float | None) -> dict[str, float]:
    """Where a reply's wait went, by stage: speech end, transcript, decision, request, first audio chunk."""

    vstages = {}
    if "speech_end" in vtrace and "filler" in vtrace:
        vstages["filler"] = vtrace["filler"] - vtrace["speech_end"]
    if "speech_end" in vtrace and "heard" in vtrace:
        vstages["heard"] = vtrace["heard"] - vtrace["speech_end"]
    if "heard" in vtrace and "decided" in vtrace:
        vstages["decide"] = vtrace["decided"] - vtrace["heard"]
    if "requested" in vtrace and vfirst_audio_at is not None:
        vstages["voice"] = vfirst_audio_at - vtrace["requested"]
    if "speech_end" in vtrace and vfirst_audio_at is not None:
        vstages["total"] = vfirst_audio_at - vtrace["speech_end"]
    return vstages


class CaptureAudio:
    def __init__(self) -> None:
        self.vpcm = bytearray()

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        self.vpcm.extend(vpcm)

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        self.vpcm.clear()


async def synthesize_fillers(vapi_key: str) -> list[bytes]:
    """Karen's own voice saying each filler, made on a throwaway session so her meeting session never holds them."""

    vsink = CaptureAudio()
    vsession = QwenOmniSession(
        vapi_key=vapi_key,
        vconversation_id="fillers",
        vsession_id="fillers",
        vepoch_provider=lambda: 0,
        vaudio_sink=vsink,
        vmodel=MEET_VOICE_MODEL,
        vinstructions="You read short phrases aloud exactly as given, in a calm, natural voice.",
        vtools=[],
        vauto_response=False,
    )
    await vsession.start()
    vevents = vsession.events()
    vfillers: list[bytes] = []
    try:
        for vphrase in FILLER_PHRASES:
            await vsink.clear()
            await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation(), vinstructions=f"Say exactly this in {MEET_LANGUAGE_NAME}, nothing else: {vphrase}"))
            while not isinstance(await anext(vevents), events.AssistantSpeechStopped):
                pass
            if vsink.vpcm:
                vfillers.append(bytes(vsink.vpcm[: len(vsink.vpcm) // 2 * 2]))
    finally:
        await vsession.close()
    return vfillers


class RoomAudioSink:
    """Qwen's voice, into the LiveKit room the Meet bridge plays back."""

    def __init__(self, vsource: rtc.AudioSource) -> None:
        self.vsource = vsource
        self.vmuted = False
        self.vfirst_audio_at: float | None = None
        self._vstray = b""

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        if self.vmuted:
            return
        if self.vfirst_audio_at is None:
            self.vfirst_audio_at = time.monotonic()
        # Deltas can end mid-sample; the odd byte belongs to the next delta.
        vpcm = self._vstray + vpcm
        vwhole = len(vpcm) - len(vpcm) % 2
        self._vstray = vpcm[vwhole:]
        if vwhole:
            await self.vsource.capture_frame(rtc.AudioFrame(vpcm[:vwhole], vsample_rate_hz, 1, vwhole // 2))

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        self._vstray = b""
        self.vsource.clear_queue()


class MeetCall:
    """Karen in one meeting: one Qwen Omni session hears, transcribes, routes, speaks and calls tools; GLM does
    delegated work. The session lives until DashScope closes it, and is then reopened seeded with the text log
    of the last ten minutes."""

    def __init__(self, vroom: rtc.Room) -> None:
        self.vroom = vroom
        self.vsource = rtc.AudioSource(ROOM_SAMPLE_RATE_HZ, 1, queue_size_ms=PLAYBACK_QUEUE_MS)
        self.vsink = RoomAudioSink(self.vsource)
        self.vsession: QwenOmniSession | None = None
        self.vmemory = MeetMemory()
        self.vaddressing: MeetAddressing | None = None
        self.vdelegate_llm = voice_app.build_llm(MEET_DELEGATE_MODEL)
        self.vregistry = TaskRegistry()
        self.vsupervisor = TaskSupervisor(
            vconversation_id=vroom.name,
            vsession_id=vroom.name,
            vregistry=self.vregistry,
            vrunner=self.run_task,
            von_finished=self.on_task_finished,
        )
        self.vuser_speaking = False
        self.vlast_human_speech = 0.0
        self.vlast_bot_reply_done = 0.0
        self.vlast_bot_activity = 0.0
        self.vreply_parts: list[str] = []
        self.vcaller_name = ""
        self.vinterrupted = False
        self.vplayed = asyncio.Event()
        self.vpending: collections.deque[PendingResult] = collections.deque()
        self.vpending_added = asyncio.Event()
        self.vfloor = asyncio.Lock()
        self.vrouting: asyncio.Future[str] | None = None
        self.vroute_parts: list[str] = []
        self.vdiscard_next = False
        self.vtool_followup = False
        self.vfacts: list[str] = []
        self.vtrace: dict[str, float] = {}
        self.vloud_s = 0.0
        self.vfillers: list[bytes] = []
        self.vlast_response_at = time.monotonic()
        self._vtasks: set[asyncio.Task[None]] = set()

    def publish(self, **vevent: object) -> None:
        if self.vroom.isconnected():
            self.spawn(self.vroom.local_participant.publish_data(json.dumps(vevent, ensure_ascii=False), reliable=True, topic=KAREN_EVENTS_TOPIC))

    def spawn(self, vcoro) -> None:
        vtask = asyncio.create_task(vcoro)
        self._vtasks.add(vtask)
        vtask.add_done_callback(self._vtasks.discard)

    def meet_attribute(self, vname: str) -> str:
        return next((vp.attributes[vname] for vp in self.vroom.remote_participants.values() if vname in vp.attributes), "")

    def addressing(self) -> MeetAddressing:
        if self.vaddressing is None:
            self.vaddressing = MeetAddressing(self.meet_attribute(MEET_BOT_NAME_ATTRIBUTE) or MEET_DEFAULT_BOT_NAME, self.route)
        return self.vaddressing

    async def take_floor(self) -> None:
        # A realtime session generates one response at a time: routing steps, answers and deliveries take turns.
        try:
            await asyncio.wait_for(self.vfloor.acquire(), FLOOR_TIMEOUT_S)
        except TimeoutError:
            logger.warning("response floor held for %ss; taking it over", FLOOR_TIMEOUT_S)
            self.release_floor()
            await self.vfloor.acquire()

    def release_floor(self) -> None:
        if self.vfloor.locked():
            self.vfloor.release()

    async def route(self, vprompt: str) -> str:
        """Asks Karen's own session whether the latest line was for her, as a text-only response.

        Same session on purpose: it heard the audio and knows what Karen last said. A separate judge session only
        sees the text; automatic responses cannot be kept silent at all (2/9 on a test dialogue), while this
        routing step scored 9/9.
        """
        await self.take_floor()
        vsession = self.vsession
        assert vsession is not None
        self.vrouting = asyncio.get_running_loop().create_future()
        self.vroute_parts = []
        # Qwen 3.8 ignores the text-only request and speaks the verdict too, about a second of "IGNORE".
        self.vsink.vmuted = True
        try:
            await vsession.request_response(events.ResponseRequest(vcorrelation=vsession.correlation(), vinstructions=vprompt, vtext_only=True))
            return await asyncio.wait_for(self.vrouting, ROUTE_TIMEOUT_S)
        except (TimeoutError, ConnectionResetError):
            # Unsure means silent: a missed question can be repeated, an unasked-for reply cannot be taken back.
            logger.warning("routing step got no answer; staying silent")
            self.vdiscard_next = True
            return ""
        finally:
            self.vrouting = None
            self.vsink.vmuted = self.vdiscard_next
            self.release_floor()

    async def open_session(self) -> None:
        vsession = QwenOmniSession(
            vapi_key=os.environ["DASHSCOPE_API_KEY"],
            vconversation_id=self.vroom.name,
            vsession_id=self.vroom.name,
            vepoch_provider=lambda: 0,
            vaudio_sink=self.vsink,
            vinstructions=self.instructions(),
            vmodel=MEET_VOICE_MODEL,
            vtools=[vtool.schema() for vtool in MEET_TOOLS],
            vauto_response=False,
            vsilence_ms=TURN_SILENCE_MS,
            vtranscription_language=MEET_LANGUAGE,
        )
        await vsession.start()
        # Start first, publish second: the audio pump reads self.vsession on every frame.
        vprevious, self.vsession = self.vsession, vsession
        if self.vrouting is not None and not self.vrouting.done():
            self.vrouting.set_result("")
        self.vtool_followup = False
        self.release_floor()
        self.spawn(self.pump_events(vsession))
        if vprevious is not None:
            await vprevious.close()

    async def aclose(self) -> None:
        await self.vsupervisor.aclose()
        if self.vsession is not None:
            vsession, self.vsession = self.vsession, None
            await vsession.close()

    def on_track_subscribed(self, vtrack: rtc.Track, _vpublication: rtc.RemoteTrackPublication, vparticipant: rtc.RemoteParticipant) -> None:
        if vtrack.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info("listening to %s", vparticipant.identity)
            self.vcaller_name = vparticipant.name or vparticipant.identity
            self.spawn(self.pump_audio(vtrack))

    async def pump_audio(self, vtrack: rtc.Track) -> None:
        async for vevent in rtc.AudioStream.from_track(track=vtrack, sample_rate=ROOM_SAMPLE_RATE_HZ, num_channels=1):
            await self.forward_frame(bytes(vevent.frame.data))

    async def forward_frame(self, vpcm: bytes) -> None:
        await self.watch_for_barge_in(vpcm)
        if self.vsession is None:
            return
        try:
            await self.vsession.send_audio(events.InputAudioChunk(vpcm=vpcm, vsample_rate_hz=ROOM_SAMPLE_RATE_HZ))
        except ConnectionResetError:
            # DashScope closes a session every few minutes and pump_events replaces it; until then a frame has
            # nowhere to go. Losing 20ms is fine, losing the pump leaves Karen deaf for the rest of the meeting.
            pass

    async def watch_for_barge_in(self, vpcm: bytes) -> None:
        # Qwen's VAD reports speech only after a round trip to the endpoint, and Karen kept talking meanwhile. A
        # person audible for BARGE_IN_S while her audio is queued is enough: the bridge carries only the others.
        vframe_s = len(vpcm) / 2 / ROOM_SAMPLE_RATE_HZ
        if self.vsource.queued_duration == 0 or self.vsink.vmuted or loudness_dbfs(vpcm) < BARGE_IN_DBFS:
            self.vloud_s = 0.0
            return
        self.vloud_s += vframe_s
        if self.vloud_s < BARGE_IN_S:
            return
        self.vloud_s = 0.0
        logger.info("barge-in: someone is speaking over Karen")
        # Qwen's VAD will report this speech about half a second from now; until it does, count it here, or the
        # delivery queue sees a quiet room, re-sends the talked-over result into the speech, and Qwen cancels that
        # response without ever finishing it, which left the floor locked.
        self.vuser_speaking = True
        self.vlast_human_speech = time.monotonic()
        await self.vsink.clear()
        self.vinterrupted = True
        self.vplayed.set()
        if self.vsession is not None and self.vfloor.locked():
            await self.vsession.interrupt(events.InterruptRequest(vcorrelation=self.vsession.correlation(), vreason="barge_in"))

    async def pump_events(self, vsession: QwenOmniSession) -> None:
        async for vevent in vsession.events():
            if vsession is self.vsession:
                await self.on_event(vevent)
        if vsession is self.vsession:
            logger.warning("qwen session ended unexpectedly; opening a new one")
            await self.open_session()

    async def on_event(self, vevent: events.RealtimeEvent) -> None:
        if isinstance(vevent, events.UserSpeechStarted):
            self.vuser_speaking = True
            if self.vsource.queued_duration > 0:
                await self.vsink.clear()
                self.vinterrupted = True
                self.vplayed.set()
        elif isinstance(vevent, events.UserSpeechStopped):
            self.vuser_speaking = False
            self.vlast_human_speech = time.monotonic()
        elif isinstance(vevent, events.UserTranscriptFinal) and vevent.vtext.strip():
            await self.on_heard(vevent.vtext.strip())
        elif isinstance(vevent, events.AssistantTranscript):
            if self.vrouting is not None:
                self.vroute_parts.append(vevent.vtext)
                return
            self.vlast_bot_activity = time.monotonic()
            self.vreply_parts.append(vevent.vtext)
        elif isinstance(vevent, events.AssistantSpeechStopped):
            self.vlast_response_at = time.monotonic()
            if self.vrouting is not None:
                if not self.vrouting.done():
                    self.vrouting.set_result("".join(self.vroute_parts))
                return
            vreply = "".join(self.vreply_parts).strip()
            self.vreply_parts.clear()
            if self.vdiscard_next:
                # The tail of a routing step that timed out: a verdict, not something Karen said.
                self.vdiscard_next = False
                self.vsink.vmuted = False
                return
            self.vlast_bot_activity = time.monotonic()
            if vreply:
                self.vlast_bot_reply_done = time.monotonic()
                vlatency = reply_latency(self.vtrace, self.vsink.vfirst_audio_at)
                self.vtrace = {}
                logger.info("latency %s", " ".join(f"{vname}={vvalue:.2f}s" for vname, vvalue in vlatency.items()))
                self.remember(MeetTurn(time.time(), self.addressing().vbot_name, vreply, MeetRole.BOT), vlatency=vlatency)
                self.spawn(self.rearm_after_playout())
            if self.vtool_followup:
                self.vtool_followup = False
            else:
                self.release_floor()
        elif isinstance(vevent, events.RealtimeToolCallRequested):
            await self.on_tool_call(vevent)
        elif isinstance(vevent, events.RealtimeSessionError):
            logger.warning("qwen error: %s", vevent.vmessage)

    async def on_heard(self, vtext: str) -> None:
        vspeaker = self.meet_attribute(MEET_SPEAKER_ATTRIBUTE) or self.vcaller_name or "someone"
        vcontext = "\n".join(vturn.line() for vturn in self.vmemory.vturns[-GATE_CONTEXT_TURNS:])
        vturn = MeetTurn(time.time(), vspeaker, vtext)
        self.remember(vturn)
        vtrace = {"speech_end": self.vlast_human_speech, "heard": time.monotonic()}
        # Judged off the event pump: waiting on the gate model must not delay the next speech-started event.
        self.spawn(self.consider(vturn, vcontext, vtrace))

    async def consider(self, vturn: MeetTurn, vcontext: str, vtrace: dict[str, float]) -> None:
        if not await self.addressing().is_addressed(vturn.vspeaker, vturn.vtext, vcontext):
            return
        vtrace["decided"] = time.monotonic()
        if await self.play_filler():
            vtrace["filler"] = time.monotonic()
        logger.info("addressed speaker=%s text=%s", vturn.vspeaker, vturn.vtext)
        self.publish(type="addressed", ts=vturn.vat)
        # Waiting results are not folded into this answer: asked to do both, Qwen told the fact and skipped the
        # weather tool. They follow the moment the answer has played, since Karen then still holds the floor.
        await self.take_floor()
        self.vtrace = vtrace
        await self.speak(f"[{vturn.vspeaker}, to {self.addressing().vbot_name}] {vturn.vtext}")

    async def play_filler(self) -> bool:
        # Only into silence: a filler in the middle of Karen's own sentence is worse than none.
        if not self.vfillers or self.vsink.vmuted or self.vfloor.locked() or self.vsource.queued_duration > 0:
            return False
        await self.vsink.write(random.choice(self.vfillers), ROOM_SAMPLE_RATE_HZ)
        return True

    async def load_fillers(self) -> None:
        try:
            self.vfillers = await synthesize_fillers(os.environ["DASHSCOPE_API_KEY"])
        except Exception:
            # Fillers are a nicety: a meeting without them still works.
            logger.warning("could not synthesize fillers; Karen answers without them", exc_info=True)
            return
        logger.info("synthesized %d fillers", len(self.vfillers))

    async def keep_session_alive(self) -> None:
        # DashScope closes a session after 300s without a response, and Karen's meeting context goes with it. A
        # silent one-word routing-style response keeps it open through a stretch nobody talks to her.
        while True:
            await asyncio.sleep(KEEPALIVE_S / 8)
            if self.vsession is None or self.vfloor.locked() or time.monotonic() - self.vlast_response_at < KEEPALIVE_S:
                continue
            logger.info("keepalive response after %ds without one", int(time.monotonic() - self.vlast_response_at))
            await self.route("Internal keepalive. Reply with exactly one word: OK.")

    async def speak(self, vline: str) -> None:
        # Per-response instructions make DashScope stop calling tools, so the labelled log goes in the session prompt.
        # The line itself is sent as a message too: with only its audio in the session, Qwen answers every question
        # it has heard, and invents work for the ones it cannot answer. The caller holds the floor.
        assert self.vsession is not None
        self.vlast_bot_activity = time.monotonic()
        self.vtrace["requested"] = self.vlast_bot_activity
        self.vsink.vfirst_audio_at = None
        self.publish(type="state", state="thinking")
        await self.vsession.update_instructions(self.instructions())
        vcorrelation = self.vsession.correlation()
        await self.vsession.add_context(events.SessionContextUpdate(vscope=events.ContextScope.SPOKEN_HISTORY, vtext=vline, vcorrelation=vcorrelation, vrole=events.RealtimeRole.USER))
        await self.vsession.request_response(events.ResponseRequest(vcorrelation=vcorrelation))

    def instructions(self) -> str:
        vparts = [
            KAREN_RULES,
            f"In this meeting people call you {self.addressing().vbot_name}. Always speak {MEET_LANGUAGE_NAME}, whatever "
            f"language a line or a note is written in: speech recognition sometimes writes a {MEET_LANGUAGE_NAME} "
            f"sentence as another language.",
        ]
        vrunning = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        if vrunning:
            vlines = "\n".join(f"- {vrecord.vgoal} (asked by {vrecord.vspec.vcontext['requester']})" for vrecord in vrunning)
            vparts.append(f"Background work still running, result not known yet:\n{vlines}")
        vparts.append(
            f"Who said what in the last {int(MEET_CONTEXT_WINDOW_S // 60)} minutes (you heard the audio; this "
            f"names the speakers, and [tool] lines are the tools you called):\n{self.vmemory.transcript()}"
        )
        return "\n\n".join(vparts)

    async def on_tool_call(self, vcall: events.RealtimeToolCallRequested) -> None:
        self.vlast_bot_activity = time.monotonic()
        logger.info("tool %s(%s)", vcall.vtool_name, vcall.varguments)
        vtool = MEET_TOOLS_BY_NAME.get(vcall.vtool_name)
        if vtool is None:
            vresult = f"Error: unknown tool {vcall.vtool_name}"
        elif vtool.vweight is ToolWeight.LIGHT:
            vresult = await vtool.vrun(self, vcall.varguments)
        else:
            vresult = self.start_background(vtool, vcall.varguments)
        vargs = ", ".join(f"{vname}={vvalue!r}" for vname, vvalue in vcall.varguments.items())
        self.remember(MeetTurn(time.time(), "tool", f"{vcall.vtool_name}({vargs}) → {vresult}", MeetRole.NOTE))
        assert self.vsession is not None
        # Qwen 3.8 often says "I'm on it" in the same response that starts background work; asking it to respond
        # again to the tool result made it say so twice. A light tool's result still needs its spoken answer.
        vrespond = vtool is None or vtool.vweight is ToolWeight.LIGHT or not "".join(self.vreply_parts).strip()
        # The answer to a tool call is a second response that still belongs to this turn: keep the floor for it.
        self.vtool_followup = vrespond
        await self.vsession.send_tool_result(events.ToolResultPayload(vtool_call_id=vcall.vtool_call_id, vresult={"result": vresult}, vcorrelation=self.vsession.correlation()), vrespond=vrespond)

    def requester(self) -> str:
        return self.addressing().vengaged_speaker or "the room"

    def next_fact(self) -> str:
        if not self.vfacts:
            self.vfacts = random.sample(SCIENCE_FACTS, len(SCIENCE_FACTS))
        return self.vfacts.pop()

    def start_background(self, vtool: MeetTool, varguments: dict[str, object]) -> str:
        vgoal = vtool.goal(varguments)
        vrequester = self.requester()
        vrecord = self.vsupervisor.create_record(
            vspec=TaskSpec(
                vgoal=vgoal,
                vmode=TaskMode.BACKGROUND,
                vcontext={"requester": vrequester, "tool": vtool.vname, "arguments": varguments},
            ),
            vconversation_epoch=0,
        )
        if varguments:
            # Asking the same question again replaces the first; asking for another fact wants another fact.
            self.vsupervisor.supersede_duplicates(vrecord)
        self.vsupervisor.start_background(vrecord)
        logger.info("background task=%s tool=%s requester=%s goal=%s", vrecord.vtask_id, vtool.vname, vrequester, vgoal)
        self.publish(type="task", id=vrecord.vtask_id, goal=vgoal, requester=vrequester, status="running")
        return "Started in the background; the answer arrives later."

    async def run_task(self, vrecord: TaskRecord) -> TaskResult:
        vtool = MEET_TOOLS_BY_NAME[str(vrecord.vspec.vcontext["tool"])]
        vanswer = await vtool.vrun(self, dict(vrecord.vspec.vcontext["arguments"]))  # type: ignore[call-overload]
        return TaskResult(vtask_id=vrecord.vtask_id, vpayload={}, vsummary=vanswer)

    async def on_task_finished(self, vrecord: TaskRecord) -> None:
        if vrecord.vstatus is TaskStatus.COMPLETED and vrecord.vresult is not None:
            vanswer = vrecord.vresult.vsummary
        elif vrecord.vstatus is TaskStatus.FAILED:
            vanswer = f"the work failed ({vrecord.verror})"
        else:
            return
        vrequester = str(vrecord.vspec.vcontext["requester"])
        logger.info("task finished task=%s status=%s", vrecord.vtask_id, vrecord.vstatus.value)
        self.publish(type="task", id=vrecord.vtask_id, goal=vrecord.vgoal, requester=vrequester, status=vrecord.vstatus.value)
        self.remember(MeetTurn(time.time(), "background result", vanswer, MeetRole.NOTE))
        self.vpending.append(PendingResult(vrequester, vrecord.vgoal, vanswer))
        self.vpending_added.set()

    def is_quiet(self, vpause_s: float) -> bool:
        # Only people's silence counts. If Karen answered last she still has the floor and goes straight on, even
        # while that answer is still playing: the next reply's audio queues behind it, so there is no gap at all.
        if self.vuser_speaking or time.monotonic() - self.vlast_bot_activity <= BOT_SETTLE_S:
            return False
        if self.vlast_bot_reply_done > self.vlast_human_speech:
            return True
        return self.vsource.queued_duration == 0 and time.monotonic() - self.vlast_human_speech > vpause_s

    async def wait_until_quiet(self, vpause_s: float = QUIET_BEFORE_SPEAKING_S) -> None:
        # is_quiet already measures the pause since people last spoke; a further sustained window only delayed
        # Karen when she held the floor after her own answer.
        while True:
            if self.is_quiet(vpause_s):
                return
            await asyncio.sleep(QUIET_POLL_S)

    async def deliver_pending(self) -> None:
        # One worker, and everything waiting is told in one turn: once Karen has the floor she says all she has,
        # the way a person would, instead of pausing between results. News nobody is waiting on still waits for
        # the people to go quiet, and a turn that gets talked over goes back whole and is finished at the next pause.
        while True:
            await self.vpending_added.wait()
            self.vpending_added.clear()
            while self.vpending:
                await self.wait_until_quiet(QUIET_BEFORE_DELIVERY_S)
                await self.take_floor()
                # Read the queue only once Karen has the floor, so a result that finished meanwhile joins this turn.
                vresults = list(self.vpending)
                self.vpending.clear()
                self.addressing().engage(vresults[-1].vrequester)
                self.vtrace = {}
                await self.tell(delivery_line(vresults), vresults)

    async def tell(self, vline: str, vresults: list[PendingResult]) -> None:
        # The caller holds the floor. Results told in this turn go back to the front if someone talks over it.
        self.vinterrupted = False
        self.vplayed.clear()
        await self.speak(vline)
        if not vresults:
            return
        for vresult in vresults:
            vresult.vattempts += 1
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.vplayed.wait(), DELIVERY_PLAYOUT_TIMEOUT_S)
        vretry = [vresult for vresult in vresults if vresult.vattempts < DELIVERY_ATTEMPTS] if self.vinterrupted else []
        if vretry:
            logger.info("%d background result(s) talked over; will come back to them", len(vretry))
            self.vpending.extendleft(reversed(vretry))
            self.vpending_added.set()

    async def rearm_after_playout(self) -> None:
        await self.vsource.wait_for_playout()
        self.vplayed.set()
        self.publish(type="state", state="listening")

    def remember(self, vturn: MeetTurn, *, vlatency: dict[str, float] | None = None) -> None:
        self.vmemory.add(vturn)
        self.publish(type="turn", speaker=vturn.vspeaker, role=vturn.vrole.value, text=vturn.vtext, ts=vturn.vat, latency=vlatency or {})
        self.vmemory.forget_before(time.time())
        logger.info("meet turn speaker=%s text=%s", vturn.vspeaker, vturn.vtext)
        MEET_TRANSCRIPT_DIR.mkdir(exist_ok=True)
        with (MEET_TRANSCRIPT_DIR / f"{self.vroom.name}.jsonl").open("a", encoding="utf-8") as vfile:
            vfile.write(json.dumps({"ts": vturn.vat, "speaker": vturn.vspeaker, "role": vturn.vrole.value, "text": vturn.vtext}, ensure_ascii=False) + "\n")


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    voice_app.mirror_flexus_livekit_env()
    vcall = MeetCall(ctx.room)
    ctx.room.on("track_subscribed", vcall.on_track_subscribed)
    ctx.add_shutdown_callback(vcall.aclose)
    vcall.spawn(vcall.load_fillers())
    await vcall.open_session()
    await ctx.connect()
    await ctx.room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("karen", vcall.vsource),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    await ctx.room.local_participant.set_attributes({"active_agent_id": "karen", "active_agent_name": MEET_DEFAULT_BOT_NAME})
    vcall.spawn(vcall.keep_session_alive())
    vcall.spawn(vcall.deliver_pending())


if __name__ == "__main__":
    voice_app.mirror_flexus_livekit_env()
    agents.cli.run_app(server)
