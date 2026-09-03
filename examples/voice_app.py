from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext
from livekit.agents.voice.events import AgentStateChangedEvent, UserInputTranscribedEvent
from livekit.plugins import langchain, silero

from examples import small_agents
from examples.livekit_providers import FlexusOpenRouterSTT, FlexusOpenRouterTTS
from voice_agent.pipeline import voice_contracts, voice_return_intent

logger = logging.getLogger("voice-agent-example")

load_dotenv(".env.local")

EXAMPLE_LLM_MODEL = "google/gemini-2.5-flash"


def mirror_flexus_livekit_env() -> None:
    for vflexus_name, vsdk_name in (
        ("FLEXUS_VOICE_LIVEKIT_URL", "LIVEKIT_URL"),
        ("FLEXUS_VOICE_LIVEKIT_API_KEY", "LIVEKIT_API_KEY"),
        ("FLEXUS_VOICE_LIVEKIT_API_SECRET", "LIVEKIT_API_SECRET"),
    ):
        vvalue = os.environ.get(vflexus_name)
        if vvalue and not os.environ.get(vsdk_name):
            os.environ[vsdk_name] = vvalue
    voice_contracts.require_self_hosted_livekit_url(os.environ["LIVEKIT_URL"])


def build_llm():
    return init_chat_model(
        f"openai:{EXAMPLE_LLM_MODEL}",
        base_url=voice_contracts.OPENROUTER_AUDIO_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def build_voice_agent(vagent: small_agents.ExampleAgent, vpending: small_agents.PendingHandoff) -> Agent:
    return Agent(
        instructions="",
        llm=langchain.LLMAdapter(graph=small_agents.build_agent_graph(vagent, build_llm(), vpending)),
        tts=FlexusOpenRouterTTS(small_agents.resolve_example_profile(vagent.vprofile_id)),
    )


async def publish_active_agent(vroom: rtc.Room, vagent: small_agents.ExampleAgent) -> None:
    await vroom.local_participant.set_attributes(
        {"active_agent_id": vagent.vagent_id, "active_agent_name": vagent.vname}
    )


class ExampleCall:
    def __init__(self, vsession: AgentSession, vroom: rtc.Room) -> None:
        self.vsession = vsession
        self.vroom = vroom
        self.vpending = small_agents.PendingHandoff()
        self.vactive = small_agents.resolve_example_agent(small_agents.ENTRY_AGENT_ID)
        self._vtasks: set[asyncio.Task[None]] = set()

    def spawn(self, vcoro) -> None:
        vtask = asyncio.create_task(vcoro)
        self._vtasks.add(vtask)
        vtask.add_done_callback(self._vtasks.discard)

    async def commit_handoff(self) -> None:
        vauth = self.vpending.take()
        if vauth is None:
            return
        vtarget = small_agents.resolve_example_agent(vauth.vtarget_agent_id)
        self.vactive = vtarget
        self.vsession.update_agent(build_voice_agent(vtarget, self.vpending))
        await publish_active_agent(self.vroom, vtarget)
        logger.info("handoff committed source=%s target=%s", vauth.vsource_agent_id, vtarget.vagent_id)
        await self.vsession.generate_reply(
            instructions=(
                f"You are {vtarget.vname} and have just been handed this live call. "
                f"Greet the caller in one sentence and confirm this scoped subject: {vauth.vhandoff_summary}"
            )
        )

    def on_agent_state_changed(self, vev: AgentStateChangedEvent) -> None:
        if vev.old_state == "speaking" and vev.new_state == "listening" and self.vpending.armed():
            self.spawn(self.commit_handoff())

    def on_user_input_transcribed(self, vev: UserInputTranscribedEvent) -> None:
        if not vev.is_final or self.vactive.vagent_id == small_agents.ENTRY_AGENT_ID:
            return
        vintent = voice_return_intent.classify_voice_return_command(vev.transcript)
        if vintent is not voice_return_intent.VoiceReturnIntent.RETURN_TO_ORIGINATOR:
            return
        if self.vpending.armed():
            return
        self.vpending.arm(
            small_agents.authorize_handoff(
                self.vactive.vagent_id,
                small_agents.ENTRY_AGENT_ID,
                f"the caller asked to go back to Boss while {self.vactive.vname} was speaking",
            )
        )
        self.vsession.interrupt()
        self.spawn(self.commit_handoff())


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    mirror_flexus_livekit_env()
    vsession = AgentSession(stt=FlexusOpenRouterSTT(), vad=silero.VAD.load())
    vcall = ExampleCall(vsession, ctx.room)

    vsession.on("agent_state_changed", vcall.on_agent_state_changed)
    vsession.on("user_input_transcribed", vcall.on_user_input_transcribed)

    await vsession.start(agent=build_voice_agent(vcall.vactive, vcall.vpending), room=ctx.room)
    await publish_active_agent(ctx.room, vcall.vactive)
    await vsession.generate_reply(
        instructions=(
            "Greet the caller in one sentence, say Alice can give the weather and Bob the time, "
            "and ask what they need."
        )
    )


if __name__ == "__main__":
    mirror_flexus_livekit_env()
    agents.cli.run_app(server)
