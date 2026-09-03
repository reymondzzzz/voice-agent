# Architecture

One process, one room, one caller, one active agent at a time. LiveKit owns media and the room;
LangGraph owns reasoning and tool execution; the thin layer between them is what this repo is.

## The pieces

`src/agent.py` builds an `AgentServer` and registers a single `@server.rtc_session()` entrypoint.
Per call it creates one `AgentSession` (shared `stt` and `vad`) and starts it with a persona
`Agent`. Every persona `Agent` carries its **own** `tts`, which is the entire mechanism behind
voice switching: `session.update_agent(other)` swaps the agent and the voice together.

`src/graph.py` builds each persona's brain with `langchain.agents.create_agent(...)`, producing a
compiled graph with `model` and `tools` nodes. That graph is handed to
`livekit.plugins.langchain.LLMAdapter(graph=...)` and used as the agent's `llm`, so the persona's
`instructions` on the LiveKit side are empty — the system prompt lives in the graph.

`src/handoff.py` is pure and synchronous: it authorizes a handoff or raises
`VoiceHandoffRefused`, and holds the decision in a `PendingHandoff`. No LiveKit imports, no I/O,
so every refusal path is unit-testable with no credentials.

## The constraint that shapes everything

`LLMAdapter` is a one-way street for text. Its `_to_chat_chunk` builds only
`ChoiceDelta(role="assistant", content=...)`; it has no branch that produces `tool_calls`, and the
plugin says so in a comment on `chat()`: *"these are unused, since tool execution takes place in
langgraph"*.

The consequence is not obvious and it is expensive to rediscover: **the ordinary LiveKit handoff
pattern does not work here.** LiveKit's documented handoff returns a new `Agent` from a
`@function_tool`, but a `@function_tool` on an agent whose `llm` is the adapter is never invoked —
the graph executes tools internally and only prose comes back out. There is no error; the tool
simply never fires.

So handoff is split in two, and the seam is `PendingHandoff`:

1. **Authorize, inside the graph.** The `call_agent` tool validates the request and *arms* the
   pending handoff. It returns JSON to the model, which keeps talking.
2. **Commit, in the media layer.** `agent.py` listens for `agent_state_changed` and, on the
   `speaking` → `listening` transition with a handoff armed, calls `update_agent` with the target
   persona and then `generate_reply` for the target's greeting.

Waiting for that transition is what makes the switch land on a drained speech boundary instead of
clipping the outgoing agent mid-word.

This is the same split the flexus `voice-agent` branch arrived at — a LangGraph `voice_call_agent`
tool that only returns an authorization, and a coordinator that performs the leg switch. Reading
the adapter explains why: the architecture is forced, not stylistic.

## Deliberate omissions

There is no session/leg persistence, no reconnect recovery, and no provider abstraction here yet.
All three exist in the flexus branch and are tracked in [EXTRACTION.md](EXTRACTION.md). The skeleton
keeps a fresh `chat_ctx` per agent, which matches the decision to pass a scoped summary rather than
copy history — see [HANDOFF.md](HANDOFF.md).

## Verified vs. not

Verified against the installed `livekit-agents==1.7.1`: `AgentSession.update_agent`, the per-agent
`tts` parameter, the `agent_state_changed` states, `AgentServer.rtc_session`, `cli.run_app`, the
adapter's text-only conversion, and `create_agent` replacing the deprecated `create_react_agent`.

Not verified: a full spoken call. That needs a self-hosted LiveKit and an `OPENAI_API_KEY`, and no
such run has happened in this repo. The unit tier deliberately proves the authorization and voice
mapping without either. First live run: [RUNBOOKS.md](RUNBOOKS.md).
