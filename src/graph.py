from __future__ import annotations

import json

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph
from langchain.agents import create_agent

import handoff
from personas import Persona


def build_persona_graph(vpersona: Persona, vllm: BaseChatModel, vtools: list[BaseTool]) -> CompiledStateGraph:
    return create_agent(vllm, vtools, system_prompt=vpersona.vinstructions)


def build_call_agent_tool(vpersona: Persona, vpending: handoff.PendingHandoff) -> BaseTool:
    @tool
    def call_agent(target_agent_id: str, handoff_summary: str) -> str:
        """Hand this live voice call to another agent, who then speaks in their own voice.

        Use this when the caller asks for another agent by name instead of answering yourself.
        target_agent_id is that agent's id. handoff_summary is a concise scoped description of
        what the caller needs, under 500 characters. Returns an error string if the handoff is
        refused, in which case you stay on the call and explain that the transfer did not happen.
        """
        try:
            vauth = handoff.authorize_handoff(
                vpersona.vagent_id,
                target_agent_id,
                handoff_summary,
                vhandoff_pending=vpending.armed(),
            )
        except handoff.VoiceHandoffRefused as vexc:
            return f"Error: {vexc.vreason}"
        vpending.arm(vauth)
        return json.dumps(
            {
                "authorized": True,
                "target_agent_id": vauth.vtarget_agent_id,
                "target_name": vauth.vtarget_name,
                "handoff_summary": vauth.vhandoff_summary,
                "voice_profile_id": vauth.vprofile.vprofile_id,
            },
            ensure_ascii=False,
        )

    return call_agent
