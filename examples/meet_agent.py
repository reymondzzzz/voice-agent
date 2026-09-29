from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from langchain.agents import create_agent
from langchain_core.tools import BaseTool, tool
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext, StopResponse, llm
from livekit.agents.voice.events import AgentStateChangedEvent, ConversationItemAddedEvent
from livekit.plugins import langchain

from examples import small_agents, voice_app
from examples.livekit_providers import FlexusOpenRouterTTS
from examples.meet_addressing import MeetAddressing
from examples.meet_bridge import MEET_BOT_NAME_ATTRIBUTE, MEET_SPEAKER_ATTRIBUTE
from examples.meet_memory import MeetMemory, MeetRole, MeetTurn, background_brief, fold_prompt
from examples.spoken_only_graph import SpokenOnlyGraph
from voice_agent.agent.tasks.models import TaskMode, TaskRecord, TaskResult, TaskSpec, TaskStatus
from voice_agent.agent.tasks.registry import TaskRegistry
from voice_agent.agent.tasks.supervisor import TaskSupervisor

logger = logging.getLogger("meet-agent")

MEET_REASONING_MODEL = "anthropic/claude-sonnet-5.5"
MEET_DEFAULT_BOT_NAME = "Karen"
MEET_TRANSCRIPT_DIR = pathlib.Path("meet-transcripts")
KAREN_PROFILE_ID = "voice_boss"
QUIET_BEFORE_DELIVERY_S = 1.5
DELIVERY_POLL_S = 0.25

KAREN_RULES = (
    "You are an assistant attending a group meeting by voice. The user messages are the meeting "
    "transcript, each prefixed with its speaker; most were said between the participants, not to you. "
    "Answer only the latest message, which was said to you, in one or two short spoken sentences, "
    "using what the room discussed. Use get_current_time and get_current_weather directly. For anything "
    "that needs research, analysis, drafting or careful checking beyond what was said, call "
    "delegate_task with a self-contained goal and say in a few words that you are on it; the answer "
    "arrives later as a background result. Never guess a result that has not arrived."
)


class MeetCall:
    def __init__(self, vsession: AgentSession, vroom: rtc.Room) -> None:
        self.vsession = vsession
        self.vroom = vroom
        self.vmemory = MeetMemory()
        self.vaddressing: MeetAddressing | None = None
        self.vfast_llm = voice_app.build_llm()
        self.vreasoning_llm = voice_app.build_llm(MEET_REASONING_MODEL)
        self.vregistry = TaskRegistry()
        self.vsupervisor = TaskSupervisor(
            vconversation_id=vroom.name,
            vsession_id=vroom.name,
            vregistry=self.vregistry,
            vrunner=self.run_task,
            von_finished=self.on_task_finished,
        )
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
            vreply = await self.vfast_llm.ainvoke(fold_prompt(self.vmemory.vnotes, vturns))
            self.vmemory.fold(vturns, str(vreply.content))
            logger.info("folded %d turns into notes", len(vturns))
        finally:
            self.vfolding = False

    def briefing(self) -> str:
        vparts = [f"In this meeting people call you {self.addressing().vbot_name}."]
        if self.vmemory.vnotes:
            vparts.append(f"Notes from earlier in the meeting:\n{self.vmemory.vnotes}")
        vrunning = [vrecord for vrecord in self.vregistry.all() if vrecord.vstatus in (TaskStatus.PENDING, TaskStatus.RUNNING)]
        if vrunning:
            vlines = "\n".join(f"- {vrecord.vgoal} (asked by {vrecord.vspec.vcontext['requester']})" for vrecord in vrunning)
            vparts.append(f"Background work still running, result not known yet:\n{vlines}")
        return "\n\n".join(vparts)

    def context(self) -> list[llm.ChatItem]:
        vitems: list[llm.ChatItem] = [llm.ChatMessage(role="system", content=[self.briefing()], created_at=0.0)]
        for vturn in self.vmemory.vturns:
            vtext = vturn.vtext if vturn.vrole is MeetRole.BOT else vturn.line()
            vitems.append(llm.ChatMessage(role=vturn.vrole.value, content=[vtext], created_at=vturn.vat))
        return vitems

    def delegate(self, vgoal: str) -> str:
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
        vreply = await self.vreasoning_llm.ainvoke(str(vrecord.vspec.vcontext["brief"]))
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

    async def deliver_when_quiet(self, vrequester: str, vgoal: str) -> None:
        # A result is news nobody is waiting on mid-sentence: wait for a real pause instead of cutting in.
        vquiet_since = time.monotonic()
        while time.monotonic() - vquiet_since < QUIET_BEFORE_DELIVERY_S:
            await asyncio.sleep(DELIVERY_POLL_S)
            if self.vsession.agent_state != "listening" or self.vsession.user_state == "speaking":
                vquiet_since = time.monotonic()
        self.addressing().engage(vrequester, time.monotonic())
        self.vsession.generate_reply(
            chat_ctx=llm.ChatContext(self.context()),
            instructions=f"The background result for {vrequester} about {vgoal!r} has just arrived. Tell {vrequester} in one or two short spoken sentences.",
        )

    def on_agent_state_changed(self, vev: AgentStateChangedEvent) -> None:
        if vev.old_state == "speaking" and vev.new_state == "listening":
            self.addressing().bot_finished_speaking(time.monotonic())

    def on_conversation_item_added(self, vev: ConversationItemAddedEvent) -> None:
        if vev.item.type == "message" and vev.item.role == "assistant" and vev.item.text_content:
            self.remember(MeetTurn(time.time(), self.addressing().vbot_name, vev.item.text_content, MeetRole.BOT))


def build_delegate_tool(vcall: MeetCall) -> BaseTool:
    @tool
    async def delegate_task(goal: str) -> str:
        """Hand slow work to a stronger background model and keep talking.

        Use it for research, analysis, drafting, or checking anything the meeting has not already
        settled. goal must be self-contained, for example "check whether the October 15 deadline
        leaves Dmitry his three days for the webhooks". Returns at once; the answer arrives later as
        a background result, so never state it before then.
        """
        return vcall.delegate(goal)

    return delegate_task


class KarenAgent(Agent):
    def __init__(self, vcall: MeetCall) -> None:
        vtools = [small_agents.get_current_time, small_agents.get_current_weather, build_delegate_tool(vcall)]
        super().__init__(
            instructions="",
            llm=langchain.LLMAdapter(graph=SpokenOnlyGraph(create_agent(vcall.vfast_llm, vtools, system_prompt=KAREN_RULES))),
            tts=FlexusOpenRouterTTS(small_agents.resolve_example_profile(KAREN_PROFILE_ID)),
            allow_interruptions=True,
        )
        self.vcall = vcall

    async def on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None:
        vspeaker = self.vcall.meet_attribute(MEET_SPEAKER_ATTRIBUTE) or "someone"
        vtext = new_message.text_content or ""
        vaddressed = self.vcall.addressing().is_addressed(vspeaker, vtext, time.monotonic())
        vcontext = self.vcall.context()
        self.vcall.remember(MeetTurn(time.time(), vspeaker, vtext))
        if not vaddressed:
            raise StopResponse()
        # The meeting memory, not LiveKit's growing history, is what the model sees on every reply.
        turn_ctx.items = vcontext
        new_message.content = [f"[{vspeaker}] {vtext}"]


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    voice_app.mirror_flexus_livekit_env()
    vsession = voice_app.build_agent_session()
    vcall = MeetCall(vsession, ctx.room)
    vsession.on("agent_state_changed", vcall.on_agent_state_changed)
    vsession.on("conversation_item_added", vcall.on_conversation_item_added)
    ctx.add_shutdown_callback(vcall.vsupervisor.aclose)
    await vsession.start(agent=KarenAgent(vcall), room=ctx.room)


if __name__ == "__main__":
    voice_app.mirror_flexus_livekit_env()
    agents.cli.run_app(server)
