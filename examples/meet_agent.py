from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time

from livekit import agents, rtc
from livekit.agents import AgentServer, JobContext

from examples import small_agents, voice_app
from examples.meet_addressing import MeetAddressing
from examples.meet_bridge import MEET_BOT_NAME_ATTRIBUTE, MEET_SPEAKER_ATTRIBUTE
from examples.meet_memory import MEET_CONTEXT_WINDOW_S, MeetMemory, MeetRole, MeetTurn, background_brief
from voice_agent.pipeline import voice_contracts
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.realtime import events
from voice_agent.realtime.qwen.session import QwenOmniSession

logger = logging.getLogger("meet-agent")

MEET_DELEGATE_MODEL = "z-ai/glm-5.3"
MEET_DEFAULT_BOT_NAME = "Karen"
MEET_TRANSCRIPT_DIR = pathlib.Path("meet-transcripts")
ROOM_SAMPLE_RATE_HZ = voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
PLAYBACK_QUEUE_MS = 20_000
QUIET_BEFORE_SPEAKING_S = 1.5
QUIET_POLL_S = 0.25

KAREN_RULES = (
    "You are an assistant attending a group meeting by voice. You hear everyone, but most of it is said between "
    "the participants, and you only speak when asked to. Answer in the language you were addressed in, in one or "
    "two short spoken sentences, using what the room discussed. Use get_current_time and get_current_weather "
    "directly. For anything that needs research, analysis, drafting or careful checking beyond what was said, "
    "call delegate_task with a self-contained goal and say in a few words that you are on it; the answer arrives "
    "later as a background result. Never guess a result that has not arrived."
)

KAREN_TOOLS: list[dict[str, object]] = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Current date and time in an IANA timezone such as Asia/Tokyo.",
        "parameters": {"type": "object", "properties": {"timezone": {"type": "string"}}, "required": []},
    },
    {
        "type": "function",
        "name": "get_current_weather",
        "description": "Current weather for a city (placeholder data).",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
    {
        "type": "function",
        "name": "delegate_task",
        "description": (
            "Hand slow work to a stronger background model and keep talking: research, analysis, drafting, or "
            "checking anything the meeting has not already settled. Returns at once; the answer arrives later."
        ),
        "parameters": {
            "type": "object",
            "properties": {"goal": {"type": "string", "description": "Self-contained description of the work, naming the facts it depends on"}},
            "required": ["goal"],
        },
    },
]

FAST_TOOLS = {
    "get_current_time": small_agents.get_current_time,
    "get_current_weather": small_agents.get_current_weather,
}


class RoomAudioSink:
    """Qwen's voice, into the LiveKit room the Meet bridge plays back."""

    def __init__(self, vsource: rtc.AudioSource) -> None:
        self.vsource = vsource
        self._vstray = b""

    async def write(self, vpcm: bytes, vsample_rate_hz: int) -> None:
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
    """Karen in one meeting: Qwen Omni hears, transcribes, speaks and calls tools; GLM does delegated work.

    Qwen keeps every utterance it hears server-side, and DashScope acknowledges conversation.item.delete
    without the model forgetting the audio. So its context is bounded by replacing the session once it has
    heard a full window, at a quiet moment, seeded with the text log of that window: the meeting it knows is
    always the last ten minutes, never the whole call.
    """

    def __init__(self, vroom: rtc.Room) -> None:
        self.vroom = vroom
        self.vsource = rtc.AudioSource(ROOM_SAMPLE_RATE_HZ, 1, queue_size_ms=PLAYBACK_QUEUE_MS)
        self.vsink = RoomAudioSink(self.vsource)
        self.vsession: QwenOmniSession | None = None
        self.vsession_started_at = 0.0
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
        self.vlast_bot_activity = 0.0
        self.vreply_parts: list[str] = []
        self._vtasks: set[asyncio.Task[None]] = set()

    def spawn(self, vcoro) -> None:
        vtask = asyncio.create_task(vcoro)
        self._vtasks.add(vtask)
        vtask.add_done_callback(self._vtasks.discard)

    def meet_attribute(self, vname: str) -> str:
        return next((vp.attributes[vname] for vp in self.vroom.remote_participants.values() if vname in vp.attributes), "")

    def addressing(self) -> MeetAddressing:
        if self.vaddressing is None:
            self.vaddressing = MeetAddressing(self.meet_attribute(MEET_BOT_NAME_ATTRIBUTE) or MEET_DEFAULT_BOT_NAME)
        return self.vaddressing

    async def open_session(self) -> None:
        vsession = QwenOmniSession(
            vapi_key=os.environ["DASHSCOPE_API_KEY"],
            vconversation_id=self.vroom.name,
            vsession_id=self.vroom.name,
            vepoch_provider=lambda: 0,
            vaudio_sink=self.vsink,
            vinstructions=self.instructions(),
            vtools=KAREN_TOOLS,
            vauto_response=False,
        )
        await vsession.start()
        # Start first, publish second: the audio pump reads self.vsession on every frame.
        vprevious, self.vsession = self.vsession, vsession
        self.vsession_started_at = time.monotonic()
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
            self.spawn(self.pump_audio(vtrack))

    async def pump_audio(self, vtrack: rtc.Track) -> None:
        async for vevent in rtc.AudioStream.from_track(track=vtrack, sample_rate=ROOM_SAMPLE_RATE_HZ, num_channels=1):
            if self.vsession is not None:
                await self.vsession.send_audio(events.InputAudioChunk(vpcm=bytes(vevent.frame.data), vsample_rate_hz=ROOM_SAMPLE_RATE_HZ))

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
        elif isinstance(vevent, events.UserSpeechStopped):
            self.vuser_speaking = False
        elif isinstance(vevent, events.UserTranscriptFinal) and vevent.vtext.strip():
            await self.on_heard(vevent.vtext.strip())
        elif isinstance(vevent, events.AssistantTranscript):
            self.vlast_bot_activity = time.monotonic()
            self.vreply_parts.append(vevent.vtext)
        elif isinstance(vevent, events.AssistantSpeechStopped):
            self.vlast_bot_activity = time.monotonic()
            vreply = "".join(self.vreply_parts).strip()
            self.vreply_parts.clear()
            if vreply:
                self.remember(MeetTurn(time.time(), self.addressing().vbot_name, vreply, MeetRole.BOT))
                self.spawn(self.rearm_after_playout())
        elif isinstance(vevent, events.RealtimeToolCallRequested):
            await self.on_tool_call(vevent)
        elif isinstance(vevent, events.RealtimeSessionError):
            logger.warning("qwen error: %s", vevent.vmessage)

    async def on_heard(self, vtext: str) -> None:
        vspeaker = self.meet_attribute(MEET_SPEAKER_ATTRIBUTE) or "someone"
        vaddressed = self.addressing().is_addressed(vspeaker, vtext, time.monotonic())
        self.remember(MeetTurn(time.time(), vspeaker, vtext))
        if vaddressed:
            await self.respond()

    async def respond(self) -> None:
        # Per-response instructions make DashScope stop calling tools, so the labelled log goes in the session prompt.
        assert self.vsession is not None
        self.vlast_bot_activity = time.monotonic()
        await self.vsession.update_instructions(self.instructions())
        await self.vsession.request_response(events.ResponseRequest(vcorrelation=self.vsession.correlation()))

    def instructions(self) -> str:
        vparts = [KAREN_RULES, f"In this meeting people call you {self.addressing().vbot_name}."]
        vrunning = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        if vrunning:
            vlines = "\n".join(f"- {vrecord.vgoal} (asked by {vrecord.vspec.vcontext['requester']})" for vrecord in vrunning)
            vparts.append(f"Background work still running, result not known yet:\n{vlines}")
        vparts.append(
            f"Who said what in the last {int(MEET_CONTEXT_WINDOW_S // 60)} minutes (you heard the audio; this "
            f"names the speakers). The last line is what was just said to you:\n{self.vmemory.transcript()}"
        )
        return "\n\n".join(vparts)

    async def on_tool_call(self, vcall: events.RealtimeToolCallRequested) -> None:
        self.vlast_bot_activity = time.monotonic()
        logger.info("tool %s(%s)", vcall.vtool_name, vcall.varguments)
        if vcall.vtool_name == "delegate_task":
            vresult = self.delegate(str(vcall.varguments.get("goal", "")))
        elif vcall.vtool_name in FAST_TOOLS:
            vresult = FAST_TOOLS[vcall.vtool_name].invoke(vcall.varguments)
        else:
            vresult = f"Error: unknown tool {vcall.vtool_name}"
        assert self.vsession is not None
        await self.vsession.send_tool_result(events.ToolResultPayload(vtool_call_id=vcall.vtool_call_id, vresult={"result": vresult}, vcorrelation=self.vsession.correlation()))

    def delegate(self, vgoal: str) -> str:
        if not vgoal.strip():
            return "Error: goal is empty"
        vrequester = self.addressing().vengaged_speaker or "the room"
        vrecord = self.vsupervisor.create_record(
            vspec=TaskSpec(
                vgoal=vgoal,
                vmode=TaskMode.BACKGROUND,
                vcontext={"requester": vrequester, "brief": background_brief(vgoal, vrequester, self.vmemory)},
            ),
            vconversation_epoch=0,
        )
        self.vsupervisor.supersede_duplicates(vrecord)
        self.vsupervisor.start_background(vrecord)
        logger.info("delegated task=%s requester=%s goal=%s", vrecord.vtask_id, vrequester, vgoal)
        return f"Started. Tell {vrequester} in a few words that you are on it; the answer will come later."

    async def run_task(self, vrecord: TaskRecord) -> TaskResult:
        vreply = await self.vdelegate_llm.ainvoke(str(vrecord.vspec.vcontext["brief"]))
        return TaskResult(vtask_id=vrecord.vtask_id, vpayload={}, vsummary=str(vreply.content))

    async def on_task_finished(self, vrecord: TaskRecord) -> None:
        if vrecord.vstatus is TaskStatus.COMPLETED and vrecord.vresult is not None:
            vanswer = vrecord.vresult.vsummary
        elif vrecord.vstatus is TaskStatus.FAILED:
            vanswer = f"the work failed ({vrecord.verror})"
        else:
            return
        vrequester = str(vrecord.vspec.vcontext["requester"])
        logger.info("task finished task=%s status=%s", vrecord.vtask_id, vrecord.vstatus.value)
        self.remember(
            MeetTurn(time.time(), "background result", f"For {vrequester}, about {vrecord.vgoal!r}: {vanswer} (tell {vrequester} now, in one or two short spoken sentences)", MeetRole.NOTE)
        )
        self.spawn(self.deliver_when_quiet(vrequester))

    def is_quiet(self) -> bool:
        return (
            not self.vuser_speaking
            and self.vsource.queued_duration == 0
            and time.monotonic() - self.vlast_bot_activity > QUIET_BEFORE_SPEAKING_S
        )

    async def wait_until_quiet(self) -> None:
        vquiet_since = time.monotonic()
        while time.monotonic() - vquiet_since < QUIET_BEFORE_SPEAKING_S:
            await asyncio.sleep(QUIET_POLL_S)
            if not self.is_quiet():
                vquiet_since = time.monotonic()

    async def deliver_when_quiet(self, vrequester: str) -> None:
        # A result is news nobody is waiting on mid-sentence: wait for a real pause instead of cutting in.
        await self.wait_until_quiet()
        self.addressing().engage(vrequester, time.monotonic())
        await self.respond()

    async def rearm_after_playout(self) -> None:
        await self.vsource.wait_for_playout()
        self.addressing().bot_finished_speaking(time.monotonic())

    async def renew_when_window_is_full(self) -> None:
        while self.vsession is not None:
            await asyncio.sleep(MEET_CONTEXT_WINDOW_S / 20)
            if time.monotonic() - self.vsession_started_at < MEET_CONTEXT_WINDOW_S:
                continue
            await self.wait_until_quiet()
            await self.open_session()
            logger.info("renewed qwen session with the last %ds of transcript", int(MEET_CONTEXT_WINDOW_S))

    def remember(self, vturn: MeetTurn) -> None:
        self.vmemory.add(vturn)
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
    vcall.spawn(vcall.renew_when_window_is_full())


if __name__ == "__main__":
    voice_app.mirror_flexus_livekit_env()
    agents.cli.run_app(server)
