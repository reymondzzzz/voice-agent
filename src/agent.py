from __future__ import annotations

import asyncio
import logging

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from livekit import agents
from livekit.agents import Agent, AgentServer, AgentSession, JobContext
from livekit.agents.voice.events import AgentStateChangedEvent
from livekit.plugins import langchain, openai, silero

import handoff
from graph import build_call_agent_tool, build_persona_graph
from personas import Persona, resolve_persona, resolve_voice_profile


logger = logging.getLogger("voice-agent")

load_dotenv(".env.local")

ENTRY_AGENT_ID = "boss"
LLM_MODEL = "openai:gpt-4.1-mini"
STT_MODEL = "whisper-1"


def build_persona_agent(vpersona: Persona, vpending: handoff.PendingHandoff) -> Agent:
    vprofile = resolve_voice_profile(vpersona.vprofile_id)
    vgraph = build_persona_graph(
        vpersona,
        init_chat_model(LLM_MODEL),
        [build_call_agent_tool(vpersona, vpending)],
    )
    return Agent(
        instructions="",
        llm=langchain.LLMAdapter(graph=vgraph),
        tts=openai.TTS(model=vprofile.vmodel, voice=vprofile.vvoice, speed=vprofile.vspeed),
    )


async def commit_handoff(vsession: AgentSession, vpending: handoff.PendingHandoff) -> None:
    vauth = vpending.take()
    if vauth is None:
        return
    vtarget = resolve_persona(vauth.vtarget_agent_id)
    vsession.update_agent(build_persona_agent(vtarget, vpending))
    logger.info("handoff committed source=%s target=%s", vauth.vsource_agent_id, vauth.vtarget_agent_id)
    await vsession.generate_reply(
        instructions=(
            f"You are {vauth.vtarget_name} and have just been handed this live call. "
            f"Greet the caller in one sentence and confirm this scoped subject: {vauth.vhandoff_summary}"
        )
    )


server = AgentServer()


@server.rtc_session()
async def entrypoint(ctx: JobContext) -> None:
    vpending = handoff.PendingHandoff()
    vcommits: set[asyncio.Task[None]] = set()
    vsession = AgentSession(
        stt=openai.STT(model=STT_MODEL),
        vad=silero.VAD.load(),
    )

    @vsession.on("agent_state_changed")
    def on_agent_state_changed(vev: AgentStateChangedEvent) -> None:
        if vev.old_state != "speaking" or vev.new_state != "listening" or not vpending.armed():
            return
        vcommit = asyncio.create_task(commit_handoff(vsession, vpending))
        vcommits.add(vcommit)
        vcommit.add_done_callback(vcommits.discard)

    await vsession.start(agent=build_persona_agent(resolve_persona(ENTRY_AGENT_ID), vpending), room=ctx.room)
    await vsession.generate_reply(instructions="Greet the caller in one sentence and ask what they need.")


if __name__ == "__main__":
    agents.cli.run_app(server)
