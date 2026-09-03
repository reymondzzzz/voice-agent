# Runbooks

## First-time setup

```bash
uv sync
uv run pre-commit install          # installs both the pre-commit and pre-push hooks
cp .env.example .env.local         # then fill in OPENAI_API_KEY
uv run python scripts/verify.py --stage commit
```

`agent.py` reads `.env.local` at import time, not `.env`. A missing `OPENAI_API_KEY` does not break
the unit tier — that tier is deliberately credential-free — but it does break every mode below.

## Talking to the agent

```bash
uv run src/agent.py console        # terminal audio, no LiveKit server needed
uv run src/agent.py dev            # joins a room, hot reload, human-readable logs
uv run src/agent.py start          # production mode, JSON logs
uv run src/agent.py connect        # attach to one specific room for debugging
```

Start with `console`: it exercises the graph, the personas, and the handoff commit without any
media infrastructure, so it isolates a broken handoff from a broken room.

## Exercising the handoff

Say something like "call Sidra about the q3 sales report". Expect, in order: Boss answers in Boss's
voice, a `call_agent` tool call, Boss saying it is connecting you, then — only once Boss stops
speaking — a switch to Sidra's voice and a greeting that repeats the scoped subject back.

The log line `handoff committed source=boss target=sidra` marks the commit. If you hear Sidra's
greeting cut off Boss mid-word, the drain boundary broke (AGENTS.md rule 6). If the tool obviously
fired but no switch ever happened, check that the pending handoff was armed — a `@function_tool` on
the agent instead of a tool in the graph produces exactly this silence (rule 5).

To see a refusal, ask for an agent that does not exist. Boss should stay on the call and say the
transfer did not happen. It must never say "connected".

## Local self-hosted LiveKit

Not set up in this repo yet. The flexus branch has a working one — `compose.voice.yml` plus
`dockerfiles/livekit.dev.yaml` — pinning `livekit/livekit-server:v1.13.6` with a Redis, TCP 7880
and 7881, UDP 7882, and TURN on UDP 3478 with a 30000–30010 relay range, all bound to 127.0.0.1.
Port those two files rather than writing new ones; the loopback candidate and `node_ip` settings in
that config are what make a local room actually connect.

Once it runs, `.env.local` keeps `LIVEKIT_URL=ws://localhost:7880` with the matching key and secret.
Never point it at a cloud endpoint (rule 4).

## What has not been run

No live spoken call has been made from this repo. The gates, the unit tier, and module imports are
verified; `console`, `dev`, and the compose stack are not. The first person to run `console` should
record what actually happened here, including anything that broke.
