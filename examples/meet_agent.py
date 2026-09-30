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
from examples.meet_addressing import MeetAddressing, mentions_name
from examples.meet_bridge import MEET_BOT_NAME_ATTRIBUTE, MEET_SPEAKER_ATTRIBUTE
from examples.meet_memory import MEET_CONTEXT_WINDOW_S, MeetMemory, MeetRole, MeetTurn
from examples.meet_tools import MEET_TOOLS, MEET_TOOLS_BY_NAME, SCIENCE_FACTS, MeetTool, ToolWeight
from voice_agent.pipeline import voice_contracts
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.realtime import events
from voice_agent.realtime.qwen import protocol
from voice_agent.realtime.qwen.session import QwenOmniSession

logger = logging.getLogger("meet-agent")

MEET_VOICE_MODEL = "qwen3.5-omni-plus-realtime"
MEET_DELEGATE_MODEL = "z-ai/glm-5.3"
MEET_LANGUAGE = "ru"
MEET_LANGUAGE_NAME = "Russian"
BARGE_IN_DBFS = -40.0
BARGE_IN_S = 0.15
# Meet's noise gate clips the start of every utterance, where the name sits. "Мэгги" came through 7 of 10 times with
# 120ms clipped; "Карен" 4 ("Арен", "Лен"), "Грета" 1 ("Рита"). See docs/EXAMPLES.md before renaming.
MEET_DEFAULT_BOT_NAME = "Мэгги"
MEET_TRANSCRIPT_DIR = pathlib.Path("meet-transcripts")
ROOM_SAMPLE_RATE_HZ = voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
# Meet audio is taken straight at the rate Qwen listens at: resampling 48k to 24k and again to 16k cost accuracy.
HEARING_SAMPLE_RATE_HZ = protocol.INPUT_SAMPLE_RATE_HZ
PLAYBACK_QUEUE_MS = 20_000
QUIET_BEFORE_SPEAKING_S = 1.5
QUIET_BEFORE_DELIVERY_S = 3.0
QUIET_POLL_S = 0.25
# From the end of the words to the transcript: 1.0s at 500ms, 1.1s at 600, about 1.4s at 900. Committing on our own
# silence detection was only 0.1s faster (Qwen then takes 0.5s to transcribe) and let noise through as lines
# ("Что", "так"), 27% word errors against 17%. 500 splits three Meet phrases mid-sentence; the continuation
# handling below joins them again.
TURN_SILENCE_MS = 500
# A line from someone who is still talking waits for the rest; if nothing comes (it was noise), it is answered alone.
CONTINUATION_HOLD_S = 8.0
# A result told this soon after her answer, or while it still plays, is heard as more of the same answer.
GOING_ON_S = 1.5
FOLLOW_UP_WINDOW_S = 8.0
# LiveKit's false-interruption window is 2s; this one starts at the VAD event, itself TURN_SILENCE_MS after the words.
CONTINUATION_WINDOW_S = 2.5
GATE_CONTEXT_TURNS = 12
DELIVERY_ATTEMPTS = 2
DELIVERY_PLAYOUT_TIMEOUT_S = 60.0
BOT_SETTLE_S = 0.5
ROUTE_TIMEOUT_S = 5.0
FLOOR_TIMEOUT_S = 10.0
KEEPALIVE_S = 240.0
TURN_TIMEOUT_S = 30.0
LIVE_VALUE_NOTE = " (live value at the moment of this call; for any later question, call the tool again instead of repeating it)"
# Quoted, because asked about "your last reply" she also weighed earlier ones: after "сейчас подберу факт, секунду"
# (backed by a tool), "мне нужно уточнить город" came back YES 5 of 5 times; quoted, 20 of 20 checks were right.
PROMISE_CHECK = (
    "Internal check about this one reply of yours, and nothing earlier: «{reply}». Does it tell the person you are "
    "looking something up, checking it or will come back with an answer? Asking them something, such as which city "
    "they mean, is NO. Reply with exactly one word: YES or NO."
)
PROMISE_CORRECTION = (
    "[internal] Your last reply promised to look something up or check it, but you started no tool. Call the right "
    "tool now; do not repeat the promise."
)
KAREN_EVENTS_TOPIC = "karen"
KAREN_RULES = (
    "You are Мэгги (Maggie), a woman, an AI assistant attending a group meeting by voice. Speak of yourself in the feminine "
    "(я рада, я нашла). People address you as Мэгги; the name in a line like 'Мэгги, ...' is you, never call "
    "anyone else Мэгги, and call people by their first name as the transcript writes it, never a diminutive or nickname (Кирилл, not Кирюш). If a line misspells your own name, that is speech recognition, not the person: never mention it. You hear everyone, but almost everything is said "
    "between the participants and is not for you. Reply only to the single line addressed to you, which is the "
    "last message; never answer or act on anything else you heard, though you may use it as context. Answer in "
    "one or two short spoken sentences. You know nothing about the current time or weather: call the tool for "
    "either before answering, every time: an earlier time or weather value is already out of date. A science fact must come from science_fact, never from your own knowledge. For "
    "anything that needs research, analysis, drafting or careful checking beyond what was said, call research "
    "with a self-contained question. Time and weather come back at once: call the tool without saying anything "
    "first, not even that you are checking, and answer with the result. A tool that runs in the background returns at once: then say in a few words "
    "that you are on it. Only say that you started, are running or will return with work if you called a tool for "
    "it in this reply or it is listed below as running; otherwise say you have not started anything. Never guess "
    "a result that has not arrived. Speak like a colleague in the room: brief, warm and "
    "plain, no announcements about yourself or your tools. Sound like a person, not a script: open the "
    "way people do in conversation, with a short reaction that belongs to the sentence when it fits ('хм, ...', "
    "'о, ...', 'ну смотри, ...', 'ой, хороший вопрос, ...', only examples), never one you already used in this "
    "meeting, and often none. When you bring back a background result, open with a "
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
    # What she got out of the reply that carried this before she was cut off.
    vheard: str = ""


def result_items(vresults: list[PendingResult]) -> str:
    return "\n".join(f"- for {vresult.vrequester}, who asked: {vresult.vgoal}: {vresult.vanswer}" for vresult in vresults)


def meanwhile_line(vresults: list[PendingResult]) -> str:
    # Asked this way, 10 of 10 answers still called their tool first and ran on into the result in one breath.
    return (
        f"[meanwhile, background results came in]\n{result_items(vresults)}\nDeal with the line above first, calling "
        f"its tool if it needs one, then retell these results in your own words in the same reply, as one flowing answer."
    )


def delivery_line(vresults: list[PendingResult], *, vgoing_on: bool) -> str:
    vresumed = any(vresult.vattempts for vresult in vresults)
    vitems = result_items(vresults)
    vgo_on = (
        "You are still talking: go straight on from your last sentence as part of the same answer, with no greeting, "
        "no name and no fresh start. "
    )
    if vgoing_on and not vresumed:
        # Her answer is still playing: this reply queues right behind it, so it has to sound like the same answer.
        vhow = (
            "You are still talking: go straight on from your last sentence as part of the same answer, with no "
            "greeting, no name and no fresh start. If you just said you were looking for it, pick that up the way a "
            "person does when it turns up ('о, а вот и факт: …', 'уже нашла: …'); otherwise link it ('а ещё…', 'кстати…')."
        )
    elif vresumed:
        vheard = next((vresult.vheard for vresult in vresults if vresult.vheard), "")
        vwhere = f"after saying only «{vheard}…»" if vheard else "before they heard any of it"
        vhow = (vgo_on if vgoing_on else "") + (
            f"You were cut off {vwhere}. It is quiet now, and anything you were asked in between is already answered: "
            f"pick the thread back up the way a person does ('so, about ...') and say what they have not heard yet. "
            f"Say only what these results hold: anything else you are working on has not come back, so do not mention "
            f"it as ready or make it up."
        )
    else:
        vhow = "Tell all of it now, in one go."
    return (
        f"[background results ready]\n{vitems}\n{vhow} Keep talking from one item to the next without stopping or "
        f"asking whether to go on, a sentence or two for each, addressing each person by name, retold in your own words "
        f"and never opened with a stock phrase like 'вот научный факт'."
    )


def heard_part(vtext: str, vheard_s: float, vspoken_s: float) -> str:
    # Speech runs at a roughly even pace, so the share of her audio that played is about the share of her words.
    if vspoken_s <= 0 or vheard_s <= 0:
        return ""
    vcut = vtext[: round(len(vtext) * min(1.0, vheard_s / vspoken_s))]
    return vcut if vcut == vtext or " " not in vcut else vcut.rsplit(" ", 1)[0]


def is_spoken_verdict(vtext: str) -> bool:
    vword = "".join(vchar for vchar in vtext.upper() if vchar.isalpha())
    return bool(vword) and any(vverdict.startswith(vword) or vword.startswith(vverdict) for vverdict in ("RESPOND", "IGNORE")) and len(vword) >= 3


def parse_yes(vreply: str) -> bool:
    vupper = vreply.upper()
    return "YES" in vupper and "NO" not in vupper.replace("YES", "")


def loudness_dbfs(vpcm: bytes) -> float:
    vsamples = numpy.frombuffer(vpcm, dtype="<i2").astype(numpy.float32) / 32768.0
    if not vsamples.size:
        return -120.0
    return float(20 * numpy.log10(numpy.sqrt(numpy.mean(vsamples * vsamples)) + 1e-6))


def reply_latency(vtrace: dict[str, float], vfirst_audio_at: float | None) -> dict[str, float]:
    """Where a reply's wait went, by stage: speech end, transcript, decision, request, first audio chunk."""

    vstages = {}
    if "speech_end" in vtrace and "heard" in vtrace:
        vstages["heard"] = vtrace["heard"] - vtrace["speech_end"]
    if "heard" in vtrace and "decided" in vtrace:
        vstages["decide"] = vtrace["decided"] - vtrace["heard"]
    if "requested" in vtrace and vfirst_audio_at is not None:
        vstages["voice"] = vfirst_audio_at - vtrace["requested"]
    if "speech_end" in vtrace and vfirst_audio_at is not None:
        vstages["total"] = vfirst_audio_at - vtrace["speech_end"]
    return vstages


class RoomAudioSink:
    """Qwen's voice, into the LiveKit room the Meet bridge plays back."""

    def __init__(self, vsource: rtc.AudioSource) -> None:
        self.vsource = vsource
        self.vmuted = False
        self.vfirst_audio_at: float | None = None
        self.vqueued_at_first_audio = 0.0
        # Seconds of her audio handed to the room so far; what is still queued at a cut was never heard.
        self.vtimeline_s = 0.0
        self._vstray = b""

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
        if self.vmuted:
            return
        if self.vfirst_audio_at is None:
            self.vfirst_audio_at = time.monotonic()
            self.vqueued_at_first_audio = self.vsource.queued_duration
        # Deltas can end mid-sample; the odd byte belongs to the next delta.
        vpcm = self._vstray + vpcm
        vwhole = len(vpcm) - len(vpcm) % 2
        self._vstray = vpcm[vwhole:]
        if vwhole:
            self.vtimeline_s += vwhole / 2 / vsample_rate_hz
            await self.vsource.capture_frame(rtc.AudioFrame(vpcm[:vwhole], vsample_rate_hz, 1, vwhole // 2))

    async def flush(self) -> None:
        pass

    async def clear(self) -> None:
        self._vstray = b""
        self.vsource.clear_queue()

    async def cut(self) -> float:
        # Where on her timeline the room stopped hearing her.
        vplayed_to = max(0.0, self.vtimeline_s - self.vsource.queued_duration)
        await self.clear()
        return vplayed_to


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
        self.vlast_bot_played = 0.0
        self.vspeech_started_at = 0.0
        self.vlast_bot_activity = 0.0
        self.vreply_parts: list[str] = []
        self.vcaller_name = ""
        self.vpump: asyncio.Task | None = None
        # Each reply she owns gets a number; a result counts as told only when the reply that carried it played out.
        # Shared flags let an earlier "сейчас гляну" finishing its playout mark the answer after it as heard.
        self.vreply_seq = 0
        self.vopen_replies: set[int] = set()
        self.vplayed_replies: set[int] = set()
        self.vcut_replies: set[int] = set()
        self.vplayout = asyncio.Condition()
        self.vpending: collections.deque[PendingResult] = collections.deque()
        self.vpending_added = asyncio.Event()
        self.vfloor = asyncio.Lock()
        self.vrouting: asyncio.Future[str] | None = None
        self.vroute_parts: list[str] = []
        self.vneeds_followup = False
        self.vturn_tools = 0
        self.vturn_spoke = False
        self.vturn_reply = ""
        self.vturn_results: list[PendingResult] = []
        self.vverdict_leaked = False
        self.vturn_done = asyncio.Event()
        self.vfacts: list[str] = []
        self.vanswering: MeetTurn | None = None
        self.vanswering_heard_at = 0.0
        self.vcontinued: MeetTurn | None = None
        self.vdropping = False
        # The response this call asked for last; a completion from any other response is stale and must not end
        # the turn or release the floor that a newer response holds.
        self.vowned = ""
        self.vawaiting = False
        self.vresults_followup = False
        self.vheard_speakers: dict[str, str] = {}
        # Per reply: where its audio starts and ends on the sink's timeline, what it said, and the words heard if cut.
        self.vreply_audio: dict[int, tuple[float, float | None, str]] = {}
        self.vheard_by_reply: dict[int, str] = {}
        self.vtrace: dict[str, float] = {}
        self.vloud_s = 0.0
        self.vlast_response_at = time.monotonic()
        self._vtasks: set[asyncio.Task[None]] = set()

    def publish(self, **vevent: object) -> None:
        if self.vroom.isconnected():
            self.spawn(self.vroom.local_participant.publish_data(json.dumps(vevent, ensure_ascii=False), reliable=True, topic=KAREN_EVENTS_TOPIC))

    def spawn(self, vcoro) -> asyncio.Task:
        vtask = asyncio.create_task(vcoro)
        self._vtasks.add(vtask)
        vtask.add_done_callback(self._vtasks.discard)
        return vtask

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
            # The previous owner's response is stopped, so it neither keeps talking nor ends the new owner's turn.
            if self.vsession is not None and self.vowned:
                await self.vsession.interrupt(events.InterruptRequest(vcorrelation=self.vsession.correlation(), vreason="floor_takeover"))
            self.vowned, self.vawaiting = "", False
            self.vturn_done.set()
            self.release_floor()
            await self.vfloor.acquire()

    async def cut_her_off(self) -> None:
        # Whatever was still queued never reached the room. A reply whose audio had all played before that point was
        # heard in full even if the next one was queued behind it: counting it as cut repeated a weather answer
        # that had just been said. The first unfinished reply is the one interrupted, cut at the words heard.
        vplayed_to = await self.vsink.cut()
        vinterrupted = None
        for vreply in sorted(self.vopen_replies):
            vstart, vend, vtext = self.vreply_audio.get(vreply, (vplayed_to, None, ""))
            if vend is not None and vend <= vplayed_to:
                self.vplayed_replies.add(vreply)
                continue
            self.vcut_replies.add(vreply)
            if vinterrupted is None:
                vinterrupted = vreply
                vtext = vtext or "".join(self.vreply_parts).strip()
                vspoken_s = (vend if vend is not None else self.vsink.vtimeline_s) - vstart
                self.vheard_by_reply[vreply] = heard_part(vtext, vplayed_to - vstart, vspoken_s)
                logger.info("cut off after %.1fs of %.1fs: «%s…»", max(0.0, vplayed_to - vstart), vspoken_s, self.vheard_by_reply[vreply])
                self.publish(type="cut", heard=self.vheard_by_reply[vreply])
        self.vopen_replies.clear()
        await self.note_playout()

    async def request(self, vrequest: events.ResponseRequest) -> None:
        assert self.vsession is not None
        self.vowned, self.vawaiting = "", True
        await self.vsession.request_response(vrequest)

    def end_turn(self) -> None:
        self.vturn_done.set()
        self.release_floor()

    async def request_followup(self) -> None:
        assert self.vsession is not None
        await self.request(events.ResponseRequest(vcorrelation=self.vsession.correlation()))

    async def answer_tool_results(self) -> None:
        # Results already waiting ride along in the same response, so Karen gives one answer instead of two in a row.
        assert self.vsession is not None
        self.vresults_followup = True
        vsaying, self.vturn_results = self.vturn_results, []
        vresults = self.take_waiting_results(vsaying)
        if vresults:
            vline = f"{delivery_line(vresults, vgoing_on=False)} Answer the question you just looked up first, then go straight on to this."
            await self.vsession.add_context(events.SessionContextUpdate(vscope=events.ContextScope.SPOKEN_HISTORY, vtext=vline, vcorrelation=self.vsession.correlation(), vrole=events.RealtimeRole.USER))
        await self.request_followup()

    def hand_back_results(self) -> None:
        # The reply that was to carry them will not be heard: the one playing now, or the one still being made.
        vreply = self.vreply_seq + 1 if self.vawaiting else self.vreply_seq
        self.vopen_replies.discard(vreply)
        self.vcut_replies.add(vreply)
        self.spawn(self.note_playout())

    async def note_playout(self) -> None:
        async with self.vplayout:
            self.vplayout.notify_all()

    def take_waiting_results(self, vsaying: list[PendingResult]) -> list[PendingResult]:
        # Whoever takes the waiting results says them, with `vsaying` (tool results this reply is about to say);
        # until the reply has played out, not just been generated, none of them counts as told. A result already
        # cut off once stays queued for its own "so, about ..." retelling: folded into the next answer, which also
        # started a fact search, the model skipped the talked-over weather and it counted as told.
        vresults = [vresult for vresult in self.vpending if not vresult.vattempts]
        vresumed = [vresult for vresult in self.vpending if vresult.vattempts]
        self.vpending.clear()
        self.vpending.extend(vresumed)
        if vresults or vsaying:
            self.spawn(self.requeue_if_talked_over(vsaying + vresults, self.vreply_seq + 1))
        return vresults

    async def silence_leaked_verdict(self) -> None:
        # The routing steps share this session, so a reply can open with a spoken "RESPOND". Only its audio is
        # dropped: Qwen says the verdict and then calls the tool in the same response, and interrupting it cancelled
        # the call, so each retry leaked again and a tool turn took ten seconds.
        self.vverdict_leaked = True
        self.vsink.vmuted = True
        await self.vsink.clear()

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
            await self.request(events.ResponseRequest(vcorrelation=vsession.correlation(), vinstructions=vprompt, vtext_only=True))
            return await asyncio.wait_for(self.vrouting, ROUTE_TIMEOUT_S)
        except (TimeoutError, ConnectionResetError):
            # Unsure means silent: a missed question can be repeated, an unasked-for reply cannot be taken back.
            logger.warning("routing step got no answer; staying silent")
            # Cancelled and disowned: whenever its verdict or completion turns up, it is stale and ends nothing.
            with contextlib.suppress(ConnectionResetError):
                await vsession.interrupt(events.InterruptRequest(vcorrelation=vsession.correlation(), vreason="route_timeout"))
            self.vowned, self.vawaiting = "", False
            return ""
        finally:
            self.vrouting = None
            self.vsink.vmuted = False
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
        self.vheard_speakers.clear()
        self.vowned, self.vawaiting = "", False
        self.vneeds_followup = False
        self.vturn_done.set()
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
            # A restarted bridge leaves its old track behind for a while, and two pumps interleaved into one
            # session turned every line into noise ("Карен, как дела?" heard as "Армстронг"). Only the newest one.
            if self.vpump is not None:
                self.vpump.cancel()
            self.vpump = self.spawn(self.pump_audio(vtrack))

    async def pump_audio(self, vtrack: rtc.Track) -> None:
        async for vevent in rtc.AudioStream.from_track(track=vtrack, sample_rate=HEARING_SAMPLE_RATE_HZ, num_channels=1):
            await self.forward_frame(bytes(vevent.frame.data))

    async def forward_frame(self, vpcm: bytes) -> None:
        await self.watch_for_barge_in(vpcm)
        if self.vsession is None:
            return
        try:
            await self.vsession.send_audio(events.InputAudioChunk(vpcm=vpcm, vsample_rate_hz=HEARING_SAMPLE_RATE_HZ))
        except ConnectionResetError:
            # DashScope closes a session every few minutes and pump_events replaces it; until then a frame has
            # nowhere to go. Losing 20ms is fine, losing the pump leaves Karen deaf for the rest of the meeting.
            pass

    async def watch_for_barge_in(self, vpcm: bytes) -> None:
        # Qwen's VAD reports speech only after a round trip to the endpoint, and Karen kept talking meanwhile. A
        # person audible for BARGE_IN_S while her audio is queued is enough: the bridge carries only the others.
        vframe_s = len(vpcm) / 2 / HEARING_SAMPLE_RATE_HZ
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
        await self.cut_her_off()
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
            self.vspeech_started_at = time.monotonic()
            if self.vsource.queued_duration > 0:
                await self.cut_her_off()
            if self.is_continuation():
                await self.drop_answer_for_continuation()
        elif isinstance(vevent, events.UserSpeechStopped):
            self.vuser_speaking = False
            self.vlast_human_speech = time.monotonic()
            # Whoever Meet highlights as the words end, kept by the utterance's item: by the time its transcript
            # arrives someone else may be talking, and a transcript lost in a reconnect must not shift the rest.
            self.vheard_speakers[vevent.vitem_id] = self.current_speaker()
        elif isinstance(vevent, events.UserTranscriptFinal):
            vspeaker = self.vheard_speakers.pop(vevent.vitem_id, "") or self.current_speaker()
            if vevent.vtext.strip():
                await self.on_heard(vevent.vtext.strip(), vspeaker)
        elif isinstance(vevent, events.AssistantSpeechStarted):
            if self.vawaiting:
                self.vowned, self.vawaiting = vevent.vresponse_id, False
                self.vreply_seq += 1
                self.vreply_audio[self.vreply_seq] = (self.vsink.vtimeline_s, None, "")
                if self.vreply_seq not in self.vcut_replies:
                    self.vopen_replies.add(self.vreply_seq)
        elif isinstance(vevent, events.AssistantTranscript):
            if vevent.vresponse_id != self.vowned:
                return
            if self.vrouting is not None:
                self.vroute_parts.append(vevent.vtext)
                return
            self.vlast_bot_activity = time.monotonic()
            self.vreply_parts.append(vevent.vtext)
            if not self.vverdict_leaked and is_spoken_verdict("".join(self.vreply_parts)):
                await self.silence_leaked_verdict()
        elif isinstance(vevent, events.AssistantSpeechStopped):
            self.vlast_response_at = time.monotonic()
            if vevent.vresponse_id != self.vowned:
                logger.info("completion of an earlier response %s ignored", vevent.vresponse_id)
                return
            if self.vrouting is not None:
                if not self.vrouting.done():
                    self.vrouting.set_result("".join(self.vroute_parts))
                return
            vreply = "".join(self.vreply_parts).strip()
            self.vreply_parts.clear()
            self.vlast_bot_activity = time.monotonic()
            if self.vdropping:
                self.vdropping = False
                self.vsink.vmuted = False
                self.vneeds_followup = False
                self.end_turn()
                return
            if self.vverdict_leaked:
                self.vverdict_leaked = False
                self.vsink.vmuted = False
                vreply = ""
                if not self.vneeds_followup:
                    logger.info("routing verdict leaked into speech; asking for the answer again")
                    await self.request_followup()
                    return
            if self.vreply_seq in self.vreply_audio:
                self.vreply_audio[self.vreply_seq] = (self.vreply_audio[self.vreply_seq][0], self.vsink.vtimeline_s, vreply)
            if self.vresults_followup:
                # The follow-up that speaks tool results ended without a word: hand them back now rather than
                # after the playout timeout.
                self.vresults_followup = False
                if not vreply:
                    self.hand_back_results()
            if vreply:
                self.vturn_spoke = True
                self.vturn_reply = vreply
                self.vlast_bot_reply_done = time.monotonic()
                vlatency = reply_latency(self.vtrace, self.vsink.vfirst_audio_at)
                vgap = self.silence_since_her_last_reply()
                if vgap is not None:
                    vlatency["gap"] = vgap
                self.vtrace = {}
                logger.info("latency %s", " ".join(f"{vname}={vvalue:.2f}s" for vname, vvalue in vlatency.items()))
                self.remember(MeetTurn(time.time(), self.addressing().vbot_name, vreply, MeetRole.BOT), vlatency=vlatency)
                self.spawn(self.rearm_after_playout(self.vreply_seq))
            if self.vneeds_followup:
                self.vneeds_followup = False
                if not self.vuser_speaking:
                    # All of this response's tool results are in: one follow-up speaks them, in the same turn.
                    await self.answer_tool_results()
                    return
            if self.vturn_results:
                logger.info("%d tool result(s) not said; will tell them at the next pause", len(self.vturn_results))
                self.vpending.extend(self.vturn_results)
                self.vpending_added.set()
                self.vturn_results = []
            self.end_turn()
        elif isinstance(vevent, events.RealtimeToolCallRequested):
            await self.on_tool_call(vevent)
        elif isinstance(vevent, events.RealtimeSessionError):
            logger.warning("qwen error: %s", vevent.vmessage)
            if self.vawaiting and "already has an active response" in vevent.vmessage:
                # The request was refused, so no response will ever end this turn; end it here.
                self.vawaiting = False
                if self.vrouting is not None and not self.vrouting.done():
                    self.vrouting.set_result("")
                else:
                    self.end_turn()

    def silence_since_her_last_reply(self) -> float | None:
        # How long the room heard nothing between her previous reply and this one; None if someone spoke between.
        if self.vsink.vfirst_audio_at is None or not self.vlast_bot_played or self.vlast_human_speech > self.vlast_bot_played:
            return None
        if self.vsink.vqueued_at_first_audio > 0:
            return 0.0
        return max(0.0, self.vsink.vfirst_audio_at - self.vlast_bot_played)

    def current_speaker(self) -> str:
        return self.meet_attribute(MEET_SPEAKER_ATTRIBUTE) or self.vcaller_name or "someone"

    async def on_heard(self, vtext: str, vspeaker: str) -> None:
        vcontext = "\n".join(vturn.line() for vturn in self.vmemory.vturns[-GATE_CONTEXT_TURNS:])
        vturn = MeetTurn(time.time(), vspeaker, vtext)
        self.remember(vturn)
        vtrace = {"speech_end": self.vlast_human_speech, "heard": time.monotonic()}
        vcontinued, self.vcontinued = self.vcontinued, None
        if vcontinued is not None and vcontinued.vspeaker == vspeaker:
            vasked = MeetTurn(vturn.vat, vspeaker, f"{vcontinued.vtext} {vtext}")
            logger.info("rest of the request from %s: %s", vspeaker, vasked.vtext)
            self.publish(type="merged", into=vcontinued.vat, ts=vturn.vat, text=vasked.vtext)
            self.spawn(self.consider(vasked, vcontext, vtrace, vfollow_up=True))
            return
        if vcontinued is not None:
            self.spawn(self.consider(vcontinued, vcontext, {}, vfollow_up=True))
        vfollow_up = self.is_follow_up(vspeaker, vtext)
        # Judged off the event pump: waiting on the gate model must not delay the next speech-started event.
        self.spawn(self.consider(vturn, vcontext, vtrace, vfollow_up=vfollow_up))

    def is_continuation(self) -> bool:
        vturn = self.vanswering
        return vturn is not None and not self.vturn_tools and time.monotonic() - self.vanswering_heard_at <= CONTINUATION_WINDOW_S and self.current_speaker() == vturn.vspeaker

    async def drop_answer_for_continuation(self) -> None:
        # The pause was mid-sentence ("найди факт... и покажи погоду"): the answer to the first half is dropped and the
        # whole request answered once the rest is heard, the way LiveKit cancels a reply on a false end of turn.
        assert self.vanswering is not None and self.vsession is not None
        logger.info("%s kept talking; the answer waits for the rest", self.vanswering.vspeaker)
        vturn, self.vanswering = self.vanswering, None
        self.spawn(self.hold_for_rest(vturn))
        await self.cut_her_off()
        self.hand_back_results()
        if not self.vturn_done.is_set():
            self.vdropping = True
            self.vsink.vmuted = True
            await self.vsession.interrupt(events.InterruptRequest(vcorrelation=self.vsession.correlation(), vreason="continuation"))

    async def hold_for_rest(self, vturn: MeetTurn) -> None:
        self.vcontinued = vturn
        await asyncio.sleep(CONTINUATION_HOLD_S)
        if self.vcontinued is vturn:
            self.vcontinued = None
            logger.info("nothing more from %s; answering what was said", vturn.vspeaker)
            await self.consider(vturn, "", {}, vfollow_up=True)

    def is_follow_up(self, vspeaker: str, vtext: str) -> bool:
        # The person Karen just answered, speaking again right after her reply, is talking to her: that skips the
        # second-long routing step. Naming a colleague still goes through it, so turning to someone else works.
        if vspeaker != self.addressing().vengaged_speaker or not self.vlast_bot_played:
            return False
        if self.vspeech_started_at - self.vlast_bot_played > FOLLOW_UP_WINDOW_S:
            return False
        vcolleagues = {vturn.vspeaker.split()[0] for vturn in self.vmemory.vturns if vturn.vrole is MeetRole.PARTICIPANT and vturn.vspeaker != vspeaker}
        return not any(mentions_name(vtext, vcolleague) for vcolleague in vcolleagues)

    async def consider(self, vturn: MeetTurn, vcontext: str, vtrace: dict[str, float], *, vfollow_up: bool) -> None:
        if vfollow_up:
            logger.info("follow-up from %s, no routing step", vturn.vspeaker)
        elif not await self.addressing().is_addressed(vturn.vspeaker, vturn.vtext, vcontext):
            return
        vtrace["decided"] = time.monotonic()
        if self.vuser_speaking and self.current_speaker() == vturn.vspeaker:
            # Already talking again before she said a word: the pause was mid-sentence, so wait for the rest.
            logger.info("%s is still talking; waiting for the rest", vturn.vspeaker)
            self.spawn(self.hold_for_rest(vturn))
            return
        logger.info("addressed speaker=%s text=%s", vturn.vspeaker, vturn.vtext)
        self.publish(type="addressed", ts=vturn.vat)
        # Results that came in while she was busy go into this answer: told in a separate reply, she first said
        # "I have not got the fact yet" with the fact already waiting, then told it.
        vline = f"[{vturn.vspeaker}, to {self.addressing().vbot_name}] {vturn.vtext}"
        try:
            for vattempt in range(2):
                await self.take_floor()
                self.vtrace = vtrace
                self.vanswering, self.vanswering_heard_at = vturn, vtrace.get("heard", time.monotonic())
                vresults = [] if vattempt else self.take_waiting_results([])
                await self.speak(f"{vline}\n{meanwhile_line(vresults)}" if vresults else vline)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.vturn_done.wait(), TURN_TIMEOUT_S)
                if self.vcontinued is vturn:
                    return
                if self.vturn_spoke or self.vturn_tools:
                    break
                if vattempt:
                    return
                # Talked over before a word was said: the request is not forgotten, it is answered at the next pause.
                self.hand_back_results()
                logger.info("request from %s was cut off before an answer; will answer it at the next pause", vturn.vspeaker)
                await self.wait_until_quiet(QUIET_BEFORE_DELIVERY_S)
                vline = f"[{vturn.vspeaker} asked you this a moment ago and you have not answered yet] {vturn.vtext}"
                vtrace = {}
        finally:
            if self.vanswering is vturn:
                self.vanswering = None
        # With work running or a result waiting, "I'll tell you when it's ready" is true; checked anyway, it started a
        # second fact search.
        vbusy = self.vpending or any(vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING) for vrecord in self.vregistry.all())
        if self.vturn_spoke and not self.vturn_tools and not vbusy:
            await self.check_promise(self.vturn_reply)

    async def check_promise(self, vreply: str) -> None:
        # Qwen sometimes says "я начала проверку" and calls nothing. A reply that used no tool is asked, silently,
        # whether it promised work; if so, Karen is told to start it.
        if not parse_yes(await self.route(PROMISE_CHECK.format(reply=vreply))):
            return
        logger.info("reply promised work without a tool call; asking for the tool")
        await self.take_floor()
        await self.speak(PROMISE_CORRECTION)

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
        self.vturn_done.clear()
        self.vturn_tools = 0
        self.vturn_spoke = False
        self.vturn_reply = ""
        self.vturn_results = []
        self.vneeds_followup = False
        self.publish(type="state", state="thinking")
        await self.vsession.update_instructions(self.instructions())
        vcorrelation = self.vsession.correlation()
        await self.vsession.add_context(events.SessionContextUpdate(vscope=events.ContextScope.SPOKEN_HISTORY, vtext=vline, vcorrelation=vcorrelation, vrole=events.RealtimeRole.USER))
        await self.request(events.ResponseRequest(vcorrelation=vcorrelation))

    def instructions(self) -> str:
        vparts = [
            KAREN_RULES,
            f"Always speak {MEET_LANGUAGE_NAME}, whatever "
            f"language a line or a note is written in: speech recognition sometimes writes a {MEET_LANGUAGE_NAME} "
            f"sentence as another language.",
        ]
        vrunning = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        if vrunning:
            vlines = "\n".join(f"- {vrecord.vgoal} (asked by {vrecord.vspec.vcontext['requester']})" for vrecord in vrunning)
            vparts.append(f"Background work still running, result not known yet:\n{vlines}")
        # People's lines only: Karen's replies and tools are already conversation items, and listing her own
        # "сейчас найду" as text made her repeat the promise instead of calling the tool.
        vpeople = "\n".join(vturn.line() for vturn in self.vmemory.vturns if vturn.vrole is MeetRole.PARTICIPANT)
        vparts.append(f"Who said what in the last {int(MEET_CONTEXT_WINDOW_S // 60)} minutes (you heard the audio; this names the speakers):\n{vpeople}")
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
        if vtool is not None and vtool.vweight is ToolWeight.LIGHT:
            self.vturn_results.append(PendingResult(self.requester(), f"{vcall.vtool_name}({vargs})", vresult))
        self.remember(MeetTurn(time.time(), "tool", f"{vcall.vtool_name}({vargs}) → {vresult}", MeetRole.NOTE))
        assert self.vsession is not None
        self.vturn_tools += 1
        vlight = vtool is not None and vtool.vweight is ToolWeight.LIGHT
        # Qwen can call several tools in one response, and a response.create per result collided ("Conversation
        # already has an active response"), swallowing speech that arrived meanwhile. Results go back now; one
        # follow-up is asked for when this response ends. A heavy tool needs none if Karen already said she is on it.
        self.vneeds_followup = self.vneeds_followup or vtool is None or vlight or self.vverdict_leaked or not "".join(self.vreply_parts).strip()
        vfor_model = vresult + LIVE_VALUE_NOTE if vlight else vresult
        await self.vsession.send_tool_result(events.ToolResultPayload(vtool_call_id=vcall.vtool_call_id, vresult={"result": vfor_model}, vcorrelation=self.vsession.correlation()), vrespond=False)

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
                if not self.vpending:
                    # An answer to a tool call took them along while this worker waited for the floor.
                    self.release_floor()
                    break
                # Read the queue only once Karen has the floor, so a result that finished meanwhile joins this turn.
                vresults = list(self.vpending)
                self.vpending.clear()
                self.addressing().engage(vresults[-1].vrequester)
                self.vtrace = {}
                vgoing_on = self.vsource.queued_duration > 0 or time.monotonic() - self.vlast_bot_played < GOING_ON_S
                await self.tell(delivery_line(vresults, vgoing_on=vgoing_on), vresults)

    async def tell(self, vline: str, vresults: list[PendingResult]) -> None:
        # The caller holds the floor. Results told in this turn go back to the front if someone talks over it.
        vcarrier = self.vreply_seq + 1
        await self.speak(vline)
        await self.requeue_if_talked_over(vresults, vcarrier)

    async def requeue_if_talked_over(self, vresults: list[PendingResult], vcarrier: int) -> None:
        # `vcarrier` is the number the reply saying these results will get: the next one after the request.
        if not vresults:
            return
        for vresult in vresults:
            vresult.vattempts += 1
        async with self.vplayout:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.vplayout.wait_for(lambda: vcarrier in self.vplayed_replies or vcarrier in self.vcut_replies), DELIVERY_PLAYOUT_TIMEOUT_S)
        # Never played out is not delivered either: a reply that timed out or produced no audio goes back too.
        vcut = vcarrier in self.vcut_replies
        vheard = vcarrier in self.vplayed_replies and not vcut
        vretry = [] if vheard else [vresult for vresult in vresults if vresult.vattempts < DELIVERY_ATTEMPTS]
        for vresult in vretry:
            vresult.vheard = self.vheard_by_reply.get(vcarrier, "")
        if vretry:
            logger.info("%d background result(s) not heard; will come back to them", len(vretry))
            self.vpending.extendleft(reversed(vretry))
            self.vpending_added.set()

    async def rearm_after_playout(self, vreply: int) -> None:
        await self.vsource.wait_for_playout()
        self.vlast_bot_played = time.monotonic()
        if vreply in self.vopen_replies:
            self.vopen_replies.discard(vreply)
            self.vplayed_replies.add(vreply)
        await self.note_playout()
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
