# Extraction register: the flexus voice-agent branch

Source: `/Users/kirillstarkov/.codex/worktrees/dcb6/flexus`, branch `voice-agent`, HEAD `ada048ffe`.
About 9,300 lines of backend voice code plus a frontend feature and 28 dev-log iterations. This
repo is the standalone home for that work; this file is the register of what moves and what does
not. Update a row in the same commit that acts on it — never delete a row, mark its verdict.

**Verdicts.** `PORT` — domain-free, moves nearly as-is. `ADAPT` — valuable but coupled to flexus
Postgres/RBAC/GraphQL, needs re-grounding on this repo's much smaller model. `LEAVE` — flexus-
specific or already superseded here. `DONE` — already reimplemented in this repo.

**Confidence.** `read` — I read the file in full. `inferred` — verdict from its name, size, imports,
and the branch's commit history; confirm before acting.

## Ordered by what unblocks the most

| Priority | Item | Lines | Verdict | Confidence | Note |
| ---: | --- | ---: | --- | --- | --- |
| 1 | `services/voice/voice_contracts.py` | 171 | PORT | inferred | Stable provider interfaces. Port first: everything below types against it. |
| 2 | `services/voice/openrouter_tts.py` | 413 | PORT | inferred | Real TTS. Replaces this repo's OpenAI placeholder and is what rule 4 actually needs. |
| 3 | `services/voice/voice_stt.py` | 238 | PORT | inferred | Real STT, same reason. |
| 4 | `services/voice/voice_speech_segments.py` | 182 | PORT | inferred | Clause-boundary chunking with a spoken-word minimum. Four commits of tuning — do not re-derive. |
| 5 | `services/voice/voice_tts_prefetch.py` | 56 | PORT | inferred | Prefetches two segments ahead; paired with the above. |
| 6 | `services/voice/voice_pcm_resample.py` | 71 | PORT | inferred | Pure DSP. Needed because one provider returns 44.1kHz. |
| 7 | `services/voice/voice_livekit_pcm_sink.py` | 259 | PORT | inferred | Publishes PCM into the room. One of only two files importing `livekit.rtc`. |
| 8 | `services/voice/voice_livekit_tokens.py` | 95 | PORT | inferred | Self-hosted token minting. |
| 9 | `services/voice/voice_interruption_policy.py` | 52 | PORT | inferred | Barge-in thresholds, with measured benchmark JSON beside it. |
| 10 | `services/voice/voice_output_queue.py`, `voice_stream_queue.py` | 243 | PORT | inferred | Bounded buffers on the speech path. |
| 11 | `services/voice/voice_return_intent.py` | 39 | PORT | inferred | The "Boss, come back" recognizer. This repo has no return path; see HANDOFF.md. |
| 12 | `services/voice/voice_profile_ops.py` | 107 | ADAPT | read | Four-level profile inheritance over SQL. Port the inheritance and fallback order; drop the SQL. |
| 13 | `services/voice/voice_handoff_ops.py` | 214 | ADAPT | read | The four identity checks this repo cannot do. Re-ground, do not drop — see HANDOFF.md. |
| 14 | `services/voice/voice_leg_ops.py`, `voice_session_ops.py` | 652 | ADAPT | inferred | Leg/session state machine. The durable spine a room will need; heaviest Postgres coupling. |
| 15 | `services/voice/voice_call_actor.py`, `voice_actor_lease.py` | 348 | ADAPT | inferred | Per-call actor and its lease. Most likely basis for a room floor arbiter. |
| 16 | `services/voice/latency_trace.py` | 999 | ADAPT | inferred | Observability. Large; port the correlation-id spine, not all of it. |
| 17 | `services/voice/voice_echo_pipeline.py` | 998 | ADAPT | inferred | The pipeline itself. Check whether it is the M1 prototype before porting wholesale. |
| 18 | `services/service_voice_agent.py` | 923 | ADAPT | read (partly) | The worker entrypoint. This repo's `src/agent.py` is its seed; harvest the leg-switch wiring. |
| 19 | `voice_stt_bench.py`, `voice_tts_benchmark.py`, `voice_stt_corpus.py`, `voice_echo_types.py` | 818 | PORT | inferred | Benchmarks with recorded results in `docs/feature-voice-agent/benchmarks/*.json`. |
| — | `services/voice/voice_confirmation_*.py` | 565 | ADAPT | inferred | Tool-confirmation runtime; needs a tool policy this repo does not have yet. |
| — | `services/voice/voice_transcript_ops.py`, `voice_control_plane.py` | 483 | ADAPT | inferred | Persistence and the GraphQL control plane. Both assume the flexus API surface. |
| — | `langgraph_runtime/tools/domains/multi_agent/voice_call_agent.py` | 69 | DONE | read | Reimplemented as `call_agent` in `src/graph.py`, same JSON-plus-`Error:` contract. |
| — | `flexus_frontend/src/features/voice/**` | — | LEAVE | inferred | This repo is backend-only. `realtimeRoomClient.ts` and `voiceLegSwitch.test.ts` are the client contract if a UI is ever needed. |

## Documents worth reading before porting anything

| Item | Lines | Why |
| --- | ---: | --- |
| `docs/voice-agent/implementation_plan.md` | 1367 | The settled design. Sections "Live handoff", "Agent voice profiles", and "Concurrent room and process model" are already reflected in this repo's docs. |
| `docs/voice-agent/task_backlog.md` | 466 | M0–M7 with per-task done-when criteria. M5 covers handoff; M6 capacity; group calls are explicitly excluded. |
| `docs/feature-voice-agent/prototype_contracts.md` | 373 | Provider contracts, pairs with item 1. |
| `docs/feature-voice-agent/stt_adapter.md` | 295 | Pairs with item 3. |
| `docs/feature-voice-agent/latency_harness.md` | 219 | How the latency numbers were produced; re-run before trusting them for a room. |
| `docs/feature-voice-agent/dev_log/*.md` | 28 files | The "what broke" and "declined, with reasons" sections are the part the code cannot tell you. |

## Porting rules

Port with the tests. Every item above has tests under `tests/pipeline/` or
`tests/_project/`; an item ported without them counts as not ported. Keep the `v` prefix
(AGENTS.md rule 10) so diffs against the branch stay readable. When an `ADAPT` item loses a check
because this repo has no equivalent model, record the dropped check in `docs/HANDOFF.md` rather
than letting it disappear quietly — a dropped authorization check is a security regression, not a
simplification.
