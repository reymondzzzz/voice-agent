# AGENTS.md

Rules only. What this repo is, and what it deliberately is not, lives in `docs/`:

- [docs/REALTIME_ARCHITECTURE.md](docs/REALTIME_ARCHITECTURE.md) — the realtime agent architecture: the two planes, lifecycles, invariants, and where a future speech model plugs in.
- [docs/MOVED.md](docs/MOVED.md) — what was moved out of the flexus `voice-agent` branch, what was left behind, and why.
- [docs/EXAMPLES.md](docs/EXAMPLES.md) — the Boss / Alice / Bob LangGraph agents and how to run them.
- [docs/voice-agent/implementation_plan.md](docs/voice-agent/implementation_plan.md) — the original design, moved verbatim. Still the authority on the pipeline.
- [docs/voice-agent/task_backlog.md](docs/voice-agent/task_backlog.md) — the M0–M7 backlog, moved verbatim.
- [docs/feature-voice-agent/](docs/feature-voice-agent/) — provider contracts, the latency harness, benchmark results, and 28 dev-log iterations.

## Commands

```bash
# THE ONE COMMAND. Nothing lands without it.
uv run python scripts/verify.py --stage commit

# Installed as a git hook by `uv run pre-commit install` (pre-commit and pre-push).
# A failing run prints the check output plus the AGENTS.md rule you broke, verbatim.
# Do not bypass it with --no-verify; fix the cause.

# The individual gates. Run these directly only when debugging one of them.
uv run ruff check .
uv run pytest -q -m "not integration and not provider"
uv lock --check

# Tiers that cost money or need infrastructure, never in the gate:
uv run pytest -q -m provider        # real OpenRouter calls
uv run pytest -q -m integration     # needs redis

# The example agents. Console mode needs no LiveKit server.
uv run python -m examples.voice_app console
uv run python -m examples.voice_app dev
```

Environment traps: secrets load from `.env.local` (`cp .env.example .env.local`), and the names are
the ones `voice_contracts.VOICE_ENV_VARS` declares — `FLEXUS_VOICE_LIVEKIT_*`, not `LIVEKIT_*`.
`examples/voice_app.py` mirrors them into the `LIVEKIT_*` names the SDK reads. `pytest` puts the
repo root on the path, so imports are `voice_agent.pipeline.…` exactly as on the branch.

## Rules (enforced, numbered)

1. Lint is `ruff check .` with flexus's own narrow select set (`ASYNC`, `F401`, `F841`, `RUF100`,
   `RUF012`, `PLE`) at line-length 320. Do not widen it, add a formatter, or reformat moved code —
   the narrow set is what keeps this diffable against the branch. Enforced by the `ruff` check.
2. The unit tier stays green with no network, no credentials, and no redis:
   `pytest -m "not integration and not provider"`. Tests that spend money are marked `provider`;
   tests needing infrastructure are marked `integration`. Fix or delete a broken test, never
   skip-and-forget. Enforced by the `tests` check.
3. Dependencies change only through `uv add` / `uv remove`, and `uv.lock` co-commits. Only add a
   dependency the voice code or the examples actually import — the first attempt at this move
   dragged in cocoindex, strawberry, fastapi and telegram through transitive flexus imports, none
   of which have anything to do with voice. Enforced by the push-stage `uv-lock` check.
4. No LiveKit Cloud, ever. Media stays on a self-hosted server, and
   `voice_contracts.require_self_hosted_livekit_url` refuses a managed host at runtime — call it
   before connecting rather than trusting the environment. The cloud inference gateway
   (`livekit.agents.inference`) is equally off limits: speech goes through the moved OpenRouter
   providers. Enforced by that guard plus the `no-cloud` check; keep the check at zero.
5. `flexus_backend/**` is moved code, not new code. Change it only to fix something genuinely
   broken by the move, and keep every change small enough to read as a diff against the flexus
   `voice-agent` branch — that branch is still the upstream. New behavior belongs in `examples/`.
   Anything deleted or retargeted gets a row in `docs/MOVED.md`. Reviewers enforce.
6. Tools belong in the LangGraph graph, never on the persona `Agent`. `langchain.LLMAdapter`
   converts graph output with `_to_chat_chunk`, which only ever emits
   `ChoiceDelta(role="assistant", content=...)` and never `tool_calls` — so a LiveKit
   `@function_tool` on an agent whose `llm` is the adapter never fires, silently. A tool that must
   affect media arms state for the runtime to act on, which is what `call_agent` does.
   Reviewers enforce; nothing catches it but a tool call that never happens.
7. Commit a handoff only at a drained speech boundary — the `agent_state_changed` transition from
   `speaking` to `listening`. `update_agent` mid-speech clips the outgoing agent's last words and
   desyncs the caller-visible identity from the voice. Reviewers enforce.
8. Never tell the caller a transfer happened before it commits. `authorize_handoff` runs first and
   a refusal comes back as `Error: <reason>`, so the current agent stays on the call and explains
   that the transfer did not happen. "Connecting you" before the commit is fine; "connected" is not.
   Reviewers enforce.
9. Handoff context is an explicit bounded summary, never copied history: at most
   `MAX_HANDOFF_SUMMARY_CHARS` characters, no control characters, and the target starts with a
   fresh `chat_ctx`. Widening this to "just pass the transcript" leaks one agent's private
   instructions into another. Enforced by `tests/test_small_agents.py`.
10. Voice-domain names carry the `v` prefix (`vsession_id`, `vprofile`, `vauth`). Every moved
    module already does; dropping it in new code makes the two halves of this repo read as
    different codebases. Reviewers enforce.
11. Do not re-derive what the moved providers already handle. `open_openrouter_pcm_stream`
    validates the provider's declared PCM layout and resamples 44.1kHz down to the 24kHz room rate
    itself; `voice_stt` wraps PCM into WAV and bounds the utterance. Reach for
    `voice_pcm_resample` or a hand-rolled request only when adding a provider, never to redo one.
    Reviewers enforce.
12. `fish-audio/s2.1-pro` accepts exactly one voice id, `alloy` — every other id makes the provider
    return 400, which is why the moved `VOICE_PROFILE_REGISTRY` pins alloy on every profile. Agents
    are distinguished by `vtts_speed` until a provider with real voice ids is wired. Do not add a
    profile with an invented voice id. Enforced by `tests/test_small_agents.py`.

13. A side effect happens only through `ActionService`: prepare resolves and validates, policy
    decides whether confirmation is required, and commit executes the command that prepare stored.
    A LangGraph node or tool must never write storage directly, and a model argument must never
    reach an effect without passing a proposal. Enforced by `tests/voice_agent/test_actions.py`.
14. Audio never enters the semantic plane. Frames go to an `AudioSink`; LangGraph and the mailbox
    carry meaning only. A PCM buffer in a semantic event is a design error, not a detail.
    Reviewers enforce.
15. Documentation lands in the same commit as the change it describes, per the table below.
    Enforced by the `doc-sync` check, which fails a commit moving an area by
    `DOC_SYNC_LINE_THRESHOLD` lines or more without touching its doc.

## Layout

- `voice_agent/pipeline/**` — the moved pipeline: contracts, providers, PCM, queues,
  segmentation, interruption, latency tracing. 28 modules.
- `flexus_backend/{hallucitron,flexus_utils,services}/` — only the handful of support modules the
  pipeline imports. Do not grow these; a new import here means the flexus app is leaking back in.
- `tests/pipeline/**` — the moved tests, unchanged.
- `voice_agent/` — the realtime agent architecture. `realtime/` is the model-neutral speech
  boundary, `agent/` the semantic plane (state, graph, tasks, actions, delivery), `livekit/` the
  transport adapter. A provider SDK may only ever be imported from a `realtime/` adapter.
- `examples/` — the small LangGraph agents and the LiveKit entrypoint that demo the moved pipeline.
- `tests/` — tests for `examples/` only.
- `flexus_frontend/src/features/voice/**`, `components/ui/voice-orb.tsx` — the moved voice page and
  the procedural shader orb. No build tooling is moved with them; see `docs/MOVED.md`.
- `compose.voice.yml`, `dockerfiles/livekit.dev.yaml` — the self-hosted LiveKit stack, moved verbatim.

## Documentation

| You changed | You update |
| --- | --- |
| anything under `flexus_backend/` | `docs/MOVED.md`, with what changed and why |
| anything under `examples/` | `docs/EXAMPLES.md` |
| anything under `voice_agent/` | `docs/REALTIME_ARCHITECTURE.md` |
| a command in this file | run it first, then change it |

The `doc-sync` check fails a commit that moves an area by `DOC_SYNC_LINE_THRESHOLD` lines or more
without touching its doc. The moved `docs/voice-agent/` and `docs/feature-voice-agent/` are
history: correct them only where the move made them wrong, and never rewrite a dev log.

## PRs

Run `uv run python scripts/verify.py --stage push` before pushing. `uv.lock` co-stages with any
dependency change.
