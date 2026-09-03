# AGENTS.md

Rules only. Architecture, designs, and registers live in `docs/`:

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the runtime: server, session, persona agents, and where LangGraph sits.
- [docs/HANDOFF.md](docs/HANDOFF.md) — agent-to-agent handoff with voice switching, as built here.
- [docs/ROOM_MODEL.md](docs/ROOM_MODEL.md) — the multi-agent room design and the floor problem it has to solve.
- [docs/EXTRACTION.md](docs/EXTRACTION.md) — the register of what to port from the flexus `voice-agent` branch, with verdicts.
- [docs/RUNBOOKS.md](docs/RUNBOOKS.md) — running the agent locally against a self-hosted LiveKit.

## Commands

```bash
# THE ONE COMMAND. Nothing lands without it.
uv run python scripts/verify.py --stage commit

# Installed as a git hook by `uv run pre-commit install` (pre-commit and pre-push).
# A failing run prints the check output plus the AGENTS.md rule you broke, verbatim.
# Do not bypass it with --no-verify. If you are reaching for that flag to make a
# failure go away, you are about to do the wrong thing — fix the cause.

# The individual gates it orchestrates. Run these directly only when debugging one of them.
uv run ruff check .
uv run pytest -q -m "not provider and not room"
uv lock --check

# Push stage runs everything in the commit stage plus the lockfile check.
uv run python scripts/verify.py --stage push

# Run the agent. Console mode needs no room; dev/start need a reachable LIVEKIT_URL.
uv run src/agent.py console
uv run src/agent.py dev
```

Environment traps: dependencies come from `uv` and the gates call `.venv/bin/ruff` and
`.venv/bin/pytest` directly, so `uv sync` must have run or `verify.py` exits telling you so.
Secrets load from `.env.local` (`cp .env.example .env.local`), which `agent.py` reads at import
time — not `.env`. `pytest` puts `src/` on the path, so modules import as `personas`, not
`src.personas`.

## Rules (enforced, numbered)

1. Lint is `ruff check .` with a deliberately narrow select set (`ASYNC`, `F401`, `F841`,
   `RUF100`, `RUF012`, `PLE`) at line-length 320. Do not widen the set, add a formatter, or
   reformat untouched code — the narrow set is what keeps diffs reviewable. A genuinely needed
   suppression carries `# noqa: <code>` and a reason; `RUF100` fails once it stops applying, so
   it cannot rot. Enforced by the `ruff` check.
2. The unit tier stays green with no network and no credentials: `pytest -m "not provider and not room"`.
   Behavior added under `src/` lands with its test in the mirrored `tests/test_<module>.py` in the
   same commit. Tests that cost money are marked `provider`; tests needing a live room are marked
   `room`. Fix or delete a broken test, never skip-and-forget. Enforced by the `tests` check.
3. Dependencies change only through `uv add` / `uv remove`, and `uv.lock` co-commits with the
   change. Never hand-edit the lockfile. Enforced by the push-stage `uv-lock` check.
4. No LiveKit Cloud, ever. `LIVEKIT_URL` points at a self-hosted server, and the cloud inference
   gateway (`livekit.agents.inference`, `inference.STT`, `inference.TTS`) is off limits — speech
   providers are called directly so they stay swappable and priced by us. Enforced by the
   `no-cloud` check, which scans `src/` and `.env.example`; it is a ratchet, keep it at zero.
5. Tools belong in the LangGraph graph, never on the persona `Agent`. `langchain.LLMAdapter`
   converts graph output with `_to_chat_chunk`, which only ever emits
   `ChoiceDelta(role="assistant", content=...)` and never `tool_calls` — so a LiveKit
   `@function_tool` registered on an agent whose `llm` is the adapter can never fire, silently.
   A tool that must affect media arms state for the runtime to act on instead, as
   `call_agent` does. Reviewers enforce this; nothing catches it but a call that never happens.
6. Commit a handoff only at a drained speech boundary — the `agent_state_changed` transition from
   `speaking` to `listening`. Calling `update_agent` mid-speech clips the outgoing agent's last
   words and desyncs the caller-visible identity from the voice. Reviewers enforce.
7. Never tell the caller a transfer happened before it commits. `authorize_handoff` runs first and
   a refusal comes back as `Error: <reason>`, so the current agent stays on the call and explains
   that the transfer did not happen. "Connecting you" before the commit is fine; "connected" is not.
   Reviewers enforce.
8. Handoff context is an explicit bounded summary, never copied history. The target agent starts
   with a fresh `chat_ctx` and receives only the scoped summary — at most
   `MAX_HANDOFF_SUMMARY_CHARS` characters, no control characters. Widening this to "just pass the
   transcript" leaks one agent's private instructions and history into another. Reviewers enforce.
9. Personas reference a semantic `vprofile_id`; only `VOICE_PROFILES` maps that to a provider,
   model, voice, and speed. Migrating providers changes the registry, not persona identity. An
   unknown id falls back to `voice_default` and must never raise — a missing voice degrades the
   call, it does not drop it. Enforced by `tests/test_personas.py`.
10. Voice-domain fields, dataclass attributes, and locals carry the `v` prefix (`vsession_id`,
    `vprofile`, `vauth`). It looks unusual and it is deliberate: roughly 9,000 lines in
    `docs/EXTRACTION.md` already use it, and dropping it here means every ported file has to be
    rewritten and every diff against flexus goes noisy. Reviewers enforce.
11. No comments that narrate code, and no docstrings on internal functions. The exception that is
    not optional: a LangGraph tool's docstring is the contract the model reads to decide whether
    to call it, so those are required and are written for the model, not for us. Reviewers enforce.

## Layout

The tree is the architecture. `src/` is flat and imports are flat — there is no package prefix.

- `src/personas.py` — persona and voice-profile registries. Pure data plus lookup; no I/O.
- `src/handoff.py` — handoff authorization and the `PendingHandoff` seam. Pure and synchronous, so
  every refusal is unit-testable offline. New refusal reasons go here as a `_require_*` helper
  raising `VoiceHandoffRefused`, never as an inline `if` at the call site.
- `src/graph.py` — the LangGraph layer: the persona graph and the tools it executes.
- `src/agent.py` — the only module that may import `livekit`. Media, session lifecycle, and the
  commit-on-drain wiring live here and nowhere else.
- `scripts/verify.py` — the gate. A new check is a `CheckSpec` with the number of the rule it
  enforces, so a failure can print that rule.

Nothing under `src/` may import from `tests/`, and no module but `agent.py` may touch the network.

## Testing

`uv run pytest -q -m "not provider and not room"` is the tier that gates a commit; it must pass
with no credentials in the environment. Tests live in `tests/`, one file per `src/` module.
Green means every test passes and none is skipped for a reason nobody wrote down.

Fakes go in the test that needs them, not in a shared conftest, until a second test needs the same
fake. A model double must implement `bind_tools` — `FakeListChatModel` does not, and
`create_agent` calls it during graph construction.

## Documentation

| You changed | You update |
| --- | --- |
| `src/handoff.py`, `src/personas.py` | `docs/HANDOFF.md` |
| `src/agent.py`, `src/graph.py` | `docs/ARCHITECTURE.md` |
| anything about several agents sharing one room | `docs/ROOM_MODEL.md` |
| ported or rejected a file from the flexus branch | `docs/EXTRACTION.md`, with the verdict |
| a command in this file | run it first, then change it |

The `doc-sync` check fails a commit that moves an area by `DOC_SYNC_LINE_THRESHOLD` lines or more
without touching its doc. Stale docs are bugs: fix or delete them on sight, and record a rejected
alternative rather than deleting the reasoning. Git remembers the text; it does not remember why.

## PRs

Run `uv run python scripts/verify.py --stage push` before pushing. `uv.lock` co-stages with any
dependency change. A branch that touches handoff or the room model carries the matching `docs/`
update in the same PR, not a follow-up.
