# voice-agent

A realtime voice agent: a Google Meet assistant, «Мэгги», on one Qwen Omni realtime session, and a
LiveKit voice pipeline with three small LangGraph agents to drive it: Boss routes the call, Alice knows the weather, Bob knows the time,
and any of them can hand the live call to any other with the voice changing on the switch.

The speech pipeline lives in `voice_agent/pipeline/`.

```bash
uv sync
cp .env.example .env.local      # LIVEKIT_*, OPENROUTER_API_KEY, DASHSCOPE_API_KEY
uv run python scripts/verify.py --stage commit
uv run python -m examples.voice_app console
```

## Run Мэгги in a Google Meet

Мэгги joins a Meet as an ordinary participant: a Chrome window signed in to a second Google account
streams the call into a self-hosted LiveKit room, and the agent listens there on one Qwen Omni
realtime session. She answers only when spoken to; a task handed off while people keep talking
("Мэгги, поищи пока доку, а мы продолжим") runs in the background and only its result is said.

You need Google Chrome, `livekit-server` (`brew install livekit`) and, in `.env.local`,
`LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` (any pair you choose for the dev
server), `DASHSCOPE_API_KEY` (Qwen, Singapore region) and `OPENROUTER_API_KEY` (research on GLM).

```bash
set -a; source .env.local; set +a

# 1. LiveKit, local only
livekit-server --dev --bind 127.0.0.1 --keys "$LIVEKIT_API_KEY: $LIVEKIT_API_SECRET"

# 2. the agent (new terminal)
uv run python -m examples.meet_agent dev

# 3. the bridge into the call (new terminal); admit «Мэгги» in Meet when she asks to join
uv run python -m examples.meet_bridge "https://meet.google.com/xxx-xxxx-xxx" \
    --profile .meet-profile --room voice-meet-karen --name Мэгги

# 4. optional: watch turns, routing and tasks live
uv run python -m examples.token_server
open "http://localhost:8080/karen?observe=1&room=voice-meet-karen"
```

`--profile` is a Chrome profile directory signed in to the account she joins as; Meet turns away
anonymous automated guests. The first time, sign in to Google in the window the bridge opens; the
profile keeps the session. Use the same `--room` for the bridge and the observer page.

To restart her, stop the agent and the bridge, then start them again. The observer page keeps the
room open, so close it first, or delete the room, or the agent is not dispatched into it again.
Each room's transcript is written to `meet-transcripts/<room>.jsonl`.

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
- [docs/voice-agent/implementation_plan.md](docs/voice-agent/implementation_plan.md) — the original design, verbatim.
