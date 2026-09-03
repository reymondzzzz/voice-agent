from __future__ import annotations

import dataclasses
import datetime
import zoneinfo

from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool, tool
from langgraph.graph.state import CompiledStateGraph

from voice_agent.pipeline import voice_contracts, voice_profile_ops

ENTRY_AGENT_ID = "boss"
MAX_HANDOFF_SUMMARY_CHARS = 500
DEFAULT_TIMEZONE = "UTC"

DUMMY_WEATHER: dict[str, tuple[int, str]] = {
    "london": (14, "light rain"),
    "moscow": (9, "overcast"),
    "san francisco": (18, "fog"),
    "tokyo": (23, "clear skies"),
}
DUMMY_WEATHER_FALLBACK = (21, "clear skies")

EXAMPLE_VOICE_PROFILES: dict[str, voice_profile_ops.VoiceProfile] = {
    "voice_boss": voice_profile_ops.VoiceProfile("voice_boss", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 1.0),
    "voice_alice": voice_profile_ops.VoiceProfile("voice_alice", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 1.08),
    "voice_bob": voice_profile_ops.VoiceProfile("voice_bob", "openrouter", voice_contracts.VOICE_DEFAULT_TTS_MODEL, "alloy", 0.92),
}


@dataclasses.dataclass(frozen=True)
class ExampleAgent:
    vagent_id: str
    vname: str
    vprofile_id: str
    vinstructions: str


EXAMPLE_AGENTS: dict[str, ExampleAgent] = {
    "boss": ExampleAgent(
        vagent_id="boss",
        vname="Boss",
        vprofile_id="voice_boss",
        vinstructions=(
            "You are Boss, who answers a live voice call first and routes it. "
            "Reply in one or two short spoken sentences. "
            "Alice knows the weather, Bob knows the time. When the caller wants either, "
            "call call_agent with that agent's id and a short summary of what they asked for, "
            "instead of answering yourself."
        ),
    ),
    "alice": ExampleAgent(
        vagent_id="alice",
        vname="Alice",
        vprofile_id="voice_alice",
        vinstructions=(
            "You are Alice, the weather specialist on a live voice call. "
            "Reply in one or two short spoken sentences. "
            "Use get_current_weather, and say plainly that the reading is placeholder data. "
            "If the caller wants the time or another agent, call call_agent to hand the call over."
        ),
    ),
    "bob": ExampleAgent(
        vagent_id="bob",
        vname="Bob",
        vprofile_id="voice_bob",
        vinstructions=(
            "You are Bob, the timekeeper on a live voice call. "
            "Reply in one or two short spoken sentences. "
            "Use get_current_time for anything about the current time or date. "
            "If the caller wants the weather or another agent, call call_agent to hand the call over."
        ),
    ),
}


class HandoffRefused(RuntimeError):
    def __init__(self, vreason: str) -> None:
        super().__init__(vreason)
        self.vreason = vreason


@dataclasses.dataclass(frozen=True)
class HandoffAuthorization:
    vsource_agent_id: str
    vtarget_agent_id: str
    vtarget_name: str
    vhandoff_summary: str
    vprofile: voice_profile_ops.VoiceProfile


def resolve_example_agent(vagent_id: str) -> ExampleAgent:
    vagent = EXAMPLE_AGENTS.get(str(vagent_id or "").strip().lower())
    if vagent is None:
        raise HandoffRefused(f"target agent {vagent_id!r} not found")
    return vagent


def resolve_example_profile(vprofile_id: str) -> voice_profile_ops.VoiceProfile:
    return EXAMPLE_VOICE_PROFILES.get(vprofile_id) or EXAMPLE_VOICE_PROFILES["voice_boss"]


def authorize_handoff(vsource_agent_id: str, vtarget_agent_id: str, vhandoff_summary: str, *, vhandoff_pending: bool = False) -> HandoffAuthorization:
    if vhandoff_pending:
        raise HandoffRefused("a handoff is already pending on this call")
    vtarget = resolve_example_agent(vtarget_agent_id)
    if vtarget.vagent_id == vsource_agent_id:
        raise HandoffRefused("target agent is already active on this call")
    vsummary = str(vhandoff_summary or "").strip()
    if not vsummary:
        raise HandoffRefused("handoff summary is required")
    if len(vsummary) > MAX_HANDOFF_SUMMARY_CHARS:
        raise HandoffRefused(f"handoff summary exceeds {MAX_HANDOFF_SUMMARY_CHARS} characters")
    if any(ord(vchar) < 0x20 for vchar in vsummary):
        raise HandoffRefused("handoff summary contains control characters")
    return HandoffAuthorization(
        vsource_agent_id=vsource_agent_id,
        vtarget_agent_id=vtarget.vagent_id,
        vtarget_name=vtarget.vname,
        vhandoff_summary=vsummary,
        vprofile=resolve_example_profile(vtarget.vprofile_id),
    )


class PendingHandoff:
    def __init__(self) -> None:
        self._vauth: HandoffAuthorization | None = None

    def armed(self) -> bool:
        return self._vauth is not None

    def arm(self, vauth: HandoffAuthorization) -> None:
        self._vauth = vauth

    def take(self) -> HandoffAuthorization | None:
        vauth = self._vauth
        self._vauth = None
        return vauth


@tool
def get_current_weather(city: str) -> str:
    """Current weather for a city, as placeholder data for development.

    city is the place the caller asked about. The reading is invented, not observed, so tell the
    caller it is placeholder data instead of presenting it as a real forecast.
    """
    vcity = city.strip()
    vtemperature, vsky = DUMMY_WEATHER.get(vcity.lower(), DUMMY_WEATHER_FALLBACK)
    return f"{vcity or 'that location'}: {vtemperature} degrees Celsius, {vsky} (placeholder data)"


@tool
def get_current_time(timezone: str = "") -> str:
    """Current date and time in an IANA timezone such as Europe/London.

    timezone may be left empty, which means UTC. Returns an error string the caller can be told
    verbatim if the timezone is not a real one.
    """
    vname = timezone.strip() or DEFAULT_TIMEZONE
    try:
        vzone = zoneinfo.ZoneInfo(vname)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return f"Error: unknown timezone {vname!r}"
    return f"{datetime.datetime.now(vzone):%A %d %B %Y, %H:%M} in {vname}"


AGENT_TOOLS: dict[str, tuple[BaseTool, ...]] = {
    "boss": (),
    "alice": (get_current_weather,),
    "bob": (get_current_time,),
}


def build_call_agent_tool(vagent: ExampleAgent, vpending: PendingHandoff) -> BaseTool:
    @tool
    def call_agent(target_agent_id: str, handoff_summary: str) -> str:
        """Hand this live voice call to another agent, who then answers in their own voice.

        Use this instead of answering when the caller asks for another agent by name or wants
        something another agent owns. target_agent_id is that agent's id, handoff_summary is a
        short scoped description of what the caller needs. On refusal this returns a string
        starting with Error, and you stay on the call and say the transfer did not happen.
        """
        try:
            vauth = authorize_handoff(vagent.vagent_id, target_agent_id, handoff_summary, vhandoff_pending=vpending.armed())
        except HandoffRefused as vexc:
            return f"Error: {vexc.vreason}"
        vpending.arm(vauth)
        return f"Authorized. {vauth.vtarget_name} is taking the call about: {vauth.vhandoff_summary}"

    return call_agent


def build_agent_graph(vagent: ExampleAgent, vllm: BaseChatModel, vpending: PendingHandoff) -> CompiledStateGraph:
    vtools = [build_call_agent_tool(vagent, vpending), *AGENT_TOOLS.get(vagent.vagent_id, ())]
    return create_agent(vllm, vtools, system_prompt=vagent.vinstructions)
