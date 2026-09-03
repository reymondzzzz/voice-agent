from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from langchain.chat_models import init_chat_model
from livekit import agents, rtc
from livekit.agents import Agent, AgentServer, AgentSession, JobContext
from livekit.agents.voice.events import AgentStateChangedEvent
from livekit.plugins import langchain, silero

import contracts
import handoff
from graph import build_call_agent_tool, build_persona_graph
from openrouter_stt import OpenRouterSTT
from openrouter_tts import OpenRouterTTS
from personas import Persona, resolve_persona, resolve_voice_profile
from tools import persona_tools


logger = logging.getLogger("voice-agent")

load_dotenv(".env.local")

ENTRY_AGENT_ID = "boss"


def build_llm():
    return init_chat_model(
        f"openai:{contracts.VOICE_DEFAULT_LLM_MODEL}",
        base_url=contracts.OPENROUTER_AUDIO_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
    )


def build_persona_agent(vpersona: Persona, vpending: handoff.PendingHandoff) -> Agent:
    vprofile = resolve_voice_profile(vpersona.vprofile_id)
    vgraph = build_persona_graph(
        vpersona,
        build_llm(),
        [build_call_agent_tool(vpersona, vpending), *persona_tools(vpersona.vagent_id)],
    )
    return Agent(
        instructions="",
        llm=langchain.LLMAdapter(graph=vgraph),
        tts=OpenRouterTTS(vprofile),
    )


async def publish_active_agent(vroom: rtc.Room, vpersona: Persona) -> None:
    await vroom.local_participant.set_attributes(
        {"active_agent_id": vpersona.vagent_id, "active_agent_name": vpersona.vname}
    )


async def commit_handoff(vsession: AgentSession, vroom: rtc.Room, vpending: handoff.PendingHandoff) -> None:
    vauth = vpending.take()
    if vauth is None:
        return
    vtarget = resolve_persona(vauth.vtarget_agent_id)
    vsession.update_agent(build_persona_agent(vtarget, vpending))
    await publish_active_agent(vroom, vtarget)
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
    contracts.require_self_hosted_livekit_url(os.environ["LIVEKIT_URL"])
    vpending = handoff.PendingHandoff()
    vcommits: set[asyncio.Task[None]] = set()
    vsession = AgentSession(
        stt=OpenRouterSTT(),
        vad=silero.VAD.load(),
    )

    @vsession.on("agent_state_changed")
    def on_agent_state_changed(vev: AgentStateChangedEvent) -> None:
        if vev.old_state != "speaking" or vev.new_state != "listening" or not vpending.armed():
            return
        vcommit = asyncio.create_task(commit_handoff(vsession, ctx.room, vpending))
        vcommits.add(vcommit)
        vcommit.add_done_callback(vcommits.discard)

    ventry = resolve_persona(ENTRY_AGENT_ID)
    await vsession.start(agent=build_persona_agent(ventry, vpending), room=ctx.room)
    await publish_active_agent(ctx.room, ventry)
    await vsession.generate_reply(
        instructions=(
            "Greet the caller in one sentence, say you can reach Alice for weather or Bob for "
            "the time, and ask what they need."
        )
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
