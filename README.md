# voice-agent

The flexus voice agent, moved out of the flexus `voice-agent` branch and given three small
LangGraph agents to drive it: Boss routes the call, Alice knows the weather, Bob knows the time,
and any of them can hand the live call to any other with the voice changing on the switch.

The pipeline is flexus's own code, moved rather than rewritten — 155 of 156 moved files are
byte-identical to the branch. What was left behind, and why, is in
[docs/MOVED.md](docs/MOVED.md).

```bash
uv sync
cp .env.example .env.local      # FLEXUS_VOICE_LIVEKIT_* and OPENROUTER_API_KEY
uv run python scripts/verify.py --stage commit
uv run python -m examples.voice_app console
```

## The conversation architecture

`voice_agent/` is the realtime agent platform: the speech actor stays responsive while LangGraph
runs the thinking. See it without a microphone or a model:

```bash
uv run python -m voice_agent.demo
```

That plays the milestone interaction — start a research task, change subject, start a second task,
watch the first finish *quietly* because the topic moved on, then ask for it and get it instantly.

- [AGENTS.md](AGENTS.md) — the rules and the one command that gates a commit.
- [docs/REALTIME_ARCHITECTURE.md](docs/REALTIME_ARCHITECTURE.md) — the two planes, sequence diagrams, invariants, extension points and trade-offs.
- [docs/EXAMPLES.md](docs/EXAMPLES.md) — the three agents and how the handoff works.
- [docs/MOVED.md](docs/MOVED.md) — the move: what came, what did not.
- [docs/voice-agent/implementation_plan.md](docs/voice-agent/implementation_plan.md) — the original design, verbatim.
