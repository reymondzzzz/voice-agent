from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import time

from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, JobContext, StopResponse, llm, room_io
from livekit.agents.voice.events import UserStateChangedEvent

from examples import small_agents, voice_app
from examples.meet_addressing import MeetAddressing
from examples.meet_bridge import MEET_BOT_NAME_ATTRIBUTE, MEET_SPEAKER_ATTRIBUTE
from examples.meet_memory import MeetMemory, MeetRole, MeetTurn, background_brief, fold_prompt
from voice_agent.pipeline import voice_contracts
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor
from voice_agent.realtime import events
from voice_agent.realtime.qwen.session import QwenOmniSession
from voice_agent.realtime.session import RealtimeSessionState

logger = logging.getLogger("meet-agent")

MEET_TEXT_MODEL = "z-ai/glm-5.3"
MEET_DEFAULT_BOT_NAME = "Karen"
MEET_TRANSCRIPT_DIR = pathlib.Path("meet-transcripts")
ROOM_SAMPLE_RATE_HZ = voice_contracts.VOICE_ROOM_SAMPLE_RATE_HZ
PLAYBACK_QUEUE_MS = 20_000
QUIET_BEFORE_SPEAKING_S = 1.5
QUIET_POLL_S = 0.25

KAREN_RULES = (
    "You are an assistant attending a group meeting by voice, and you only speak when asked to. Answer in the "
    "language you were addressed in, in one or two short spoken sentences, using what the room discussed. Use "
    "get_current_time and get_current_weather directly. For anything that needs research, analysis, drafting or "
    "careful checking beyond what was said, call delegate_task with a self-contained goal and say in a few words "
    "that you are on it; the answer arrives later as a background result. Never guess a result that has not arrived."
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
    """Karen in one meeting.

    Hearing and speaking are split on purpose. The STT listener follows the whole meeting and forgets each
    utterance once it is text in `MeetMemory`. Qwen never receives meeting audio: it is asked to speak only
    when Karen is addressed, and each request carries the notes and the last few minutes, so its own
    history is Karen's replies and nothing else. DashScope acknowledges conversation.item.delete but the
    model still remembers the deleted audio, so keeping audio out is the only way to bound its context.
    """

    def __init__(self, vroom: rtc.Room) -> None:
        self.vroom = vroom
        self.vsource = rtc.AudioSource(ROOM_SAMPLE_RATE_HZ, 1, queue_size_ms=PLAYBACK_QUEUE_MS)
        self.vsink = RoomAudioSink(self.vsource)
        self.vsession: QwenOmniSession | None = None
        self.vmemory = MeetMemory()
        self.vaddressing: MeetAddressing | None = None
        self.vtext_llm = voice_app.build_llm(MEET_TEXT_MODEL)
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
        self.vfolding = False
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

    async def speaker(self) -> QwenOmniSession:
        if self.vsession is None or self.vsession.vstate is RealtimeSessionState.CLOSED:
            vsession = QwenOmniSession(
                vapi_key=os.environ["DASHSCOPE_API_KEY"],
                vconversation_id=self.vroom.name,
                vsession_id=self.vroom.name,
                vepoch_provider=lambda: 0,
                vaudio_sink=self.vsink,
                vinstructions=KAREN_RULES,
                vtools=KAREN_TOOLS,
                vauto_response=False,
            )
            await vsession.start()
            self.vsession = vsession
            self.spawn(self.pump_events(vsession))
        return self.vsession

    async def aclose(self) -> None:
        await self.vsupervisor.aclose()
        if self.vsession is not None:
            vsession, self.vsession = self.vsession, None
            await vsession.close()

    async def pump_events(self, vsession: QwenOmniSession) -> None:
        async for vevent in vsession.events():
            await self.on_event(vevent)
        if self.vsession is vsession:
            logger.info("qwen session ended; the next reply opens a new one")
            self.vsession = None

    async def on_event(self, vevent: events.RealtimeEvent) -> None:
        if isinstance(vevent, events.AssistantTranscript):
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

    def on_user_state_changed(self, vev: UserStateChangedEvent) -> None:
        self.vuser_speaking = vev.new_state == "speaking"
        if self.vuser_speaking and self.vsession is not None and self.vsource.queued_duration > 0:
            self.spawn(self.vsession.interrupt(events.InterruptRequest(vcorrelation=self.vsession.correlation(), vreason="barge_in")))

    async def on_heard(self, vtext: str) -> None:
        vspeaker = self.meet_attribute(MEET_SPEAKER_ATTRIBUTE) or "someone"
        vaddressed = self.addressing().is_addressed(vspeaker, vtext, time.monotonic())
        self.remember(MeetTurn(time.time(), vspeaker, vtext))
        if vaddressed:
            await self.respond(f"[{vspeaker}] {vtext}")

    async def respond(self, vline: str) -> None:
        # Only the line addressed to Karen becomes a conversation item; the meeting itself travels in the
        # session prompt, which is replaced each time rather than piling up in Qwen's history.
        vsession = await self.speaker()
        self.vlast_bot_activity = time.monotonic()
        await vsession.update_instructions(self.instructions())
        vcorrelation = vsession.correlation()
        await vsession.add_context(events.SessionContextUpdate(vscope=events.ContextScope.SPOKEN_HISTORY, vtext=vline, vcorrelation=vcorrelation, vrole=events.RealtimeRole.USER))
        await vsession.request_response(events.ResponseRequest(vcorrelation=vcorrelation))

    def instructions(self) -> str:
        vparts = [KAREN_RULES, f"In this meeting people call you {self.addressing().vbot_name}."]
        if self.vmemory.vnotes:
            vparts.append(f"Notes from earlier in the meeting:\n{self.vmemory.vnotes}")
        vrunning = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        if vrunning:
            vlines = "\n".join(f"- {vrecord.vgoal} (asked by {vrecord.vspec.vcontext['requester']})" for vrecord in vrunning)
            vparts.append(f"Background work still running, result not known yet:\n{vlines}")
        vparts.append(f"Meeting transcript since those notes, the last line being what was just said to you:\n{self.vmemory.transcript()}")
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
        vsession = await self.speaker()
        await vsession.send_tool_result(events.ToolResultPayload(vtool_call_id=vcall.vtool_call_id, vresult={"result": vresult}, vcorrelation=vsession.correlation()))

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
        vreply = await self.vtext_llm.ainvoke(str(vrecord.vspec.vcontext["brief"]))
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
        self.remember(MeetTurn(time.time(), "background result", f"For {vrequester}, about {vrecord.vgoal!r}: {vanswer}", MeetRole.NOTE))
        self.spawn(self.deliver_when_quiet(vrequester, vrecord.vgoal))

    def is_quiet(self) -> bool:
        return (
            not self.vuser_speaking
            and self.vsource.queued_duration == 0
            and time.monotonic() - self.vlast_bot_activity > QUIET_BEFORE_SPEAKING_S
        )

    async def deliver_when_quiet(self, vrequester: str, vgoal: str) -> None:
        # A result is news nobody is waiting on mid-sentence: wait for a real pause instead of cutting in.
        vquiet_since = time.monotonic()
        while time.monotonic() - vquiet_since < QUIET_BEFORE_SPEAKING_S:
            await asyncio.sleep(QUIET_POLL_S)
            if not self.is_quiet():
                vquiet_since = time.monotonic()
        self.addressing().engage(vrequester, time.monotonic())
        await self.respond(f"[background result for {vrequester} about {vgoal!r} is now in the transcript] Tell {vrequester} in one or two short spoken sentences.")

    async def rearm_after_playout(self) -> None:
        await self.vsource.wait_for_playout()
        self.addressing().bot_finished_speaking(time.monotonic())

    def remember(self, vturn: MeetTurn) -> None:
        self.vmemory.add(vturn)
        logger.info("meet turn speaker=%s text=%s", vturn.vspeaker, vturn.vtext)
        MEET_TRANSCRIPT_DIR.mkdir(exist_ok=True)
        with (MEET_TRANSCRIPT_DIR / f"{self.vroom.name}.jsonl").open("a", encoding="utf-8") as vfile:
            vfile.write(json.dumps({"ts": vturn.vat, "speaker": vturn.vspeaker, "role": vturn.vrole.value, "text": vturn.vtext}, ensure_ascii=False) + "\n")
        vexpired = self.vmemory.expired(time.time())
        if vexpired and not self.vfolding:
            self.vfolding = True
            self.spawn(self.fold(vexpired))

    async def fold(self, vturns: list[MeetTurn]) -> None:
        try:
            vreply = await self.vtext_llm.ainvoke(fold_prompt(self.vmemory.vnotes, vturns))
            self.vmemory.fold(vturns, str(vreply.content))
            logger.info("folded %d turns into notes", len(vturns))
        finally:
            self.vfolding = False


class MeetListener(Agent):
    def __init__(self, vcall: MeetCall) -> None:
        super().__init__(instructions="")
        self.vcall = vcall

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        vtext = (new_message.text_content or "").strip()
        if vtext:
            await self.vcall.on_heard(vtext)
        raise StopResponse()


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    voice_app.mirror_flexus_livekit_env()
    vcall = MeetCall(ctx.room)
    ctx.add_shutdown_callback(vcall.aclose)
    vlistener = voice_app.build_agent_session()
    vlistener.on("user_state_changed", vcall.on_user_state_changed)
    await vlistener.start(
        agent=MeetListener(vcall),
        room=ctx.room,
        room_options=room_io.RoomOptions(audio_output=False, text_output=False),
    )
    await ctx.room.local_participant.publish_track(
        rtc.LocalAudioTrack.create_audio_track("karen", vcall.vsource),
        rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE),
    )
    await vcall.speaker()


if __name__ == "__main__":
    voice_app.mirror_flexus_livekit_env()
    agents.cli.run_app(server)
