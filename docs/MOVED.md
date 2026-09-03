# What was moved, and what was not

Source: `/Users/kirillstarkov/.codex/worktrees/dcb6/flexus`, branch `voice-agent`, HEAD `ada048ffe`.

**155 of the 156 moved files are byte-identical to that branch.** One is modified, listed below.
The branch stays upstream: keep changes here small enough to read as a diff against it.

## Moved

| Area | Files | Lines | Note |
| --- | ---: | ---: | --- |
| `voice_agent/pipeline/**` | 28 | 7,071 | The pipeline: contracts, OpenRouter TTS/STT, PCM resampling, output/stream queues, speech segmentation, interruption policy, actor lease, latency tracing, benchmarks. |
| `flexus_backend/{hallucitron,flexus_utils}/**` | 5 | — | Only what the pipeline imports: `openrouter_shared`, `hallu_structs`, `asyncio_task_logging`. |
| `flexus_backend/services/{workspace_stt,migration_provider_types}.py` | 2 | 194 | Needed by `test_voice_contracts.py`, which cross-checks the STT endpoint constant. |
| `tests/pipeline/**` | 24 | — | The moved tests, unchanged but for the one file below. |
| `flexus_frontend/src/features/voice/**` + `components/ui/voice-orb.tsx`, `voiceOrbShader.ts` | 36 | 2,912 | The voice page and the procedural shader orb. |
| `docs/voice-agent/`, `docs/feature-voice-agent/` | 57 | 2,837 | Implementation plan, backlog, provider contracts, benchmark results, 28 dev-log iterations. |
| `compose.voice.yml`, `dockerfiles/livekit.dev.yaml` | 2 | — | The self-hosted LiveKit stack, pinning `livekit/livekit-server:v1.13.6`. |

520 tests pass offline: `uv run pytest -q -m "not integration and not provider"`.

## The one modified file

`tests/pipeline/test_voice_contracts.py` — two tests asserted flexus's
packaging, which does not exist here:

- `test_setup_py_voice_extra_matches_the_pinned_python_sdks` → rewritten as
  `test_pyproject_pins_match_the_declared_python_sdks`, checking the same
  `voice_contracts.LIVEKIT_PYTHON_PINS` against `pyproject.toml` instead of `setup.py`, ignoring
  extras markers. Same intent, same coverage.
- `test_voice_extra_is_absent_from_the_ci_lock_command` → deleted. It read `requirements-ci.lock`,
  which this repo does not have; `uv.lock` is the equivalent and has no extras command to assert.

`.env.example` is generated from `voice_contracts.VOICE_ENV_VARS` rather than copied from
`.env.voice.example`, because `test_every_declared_env_var_appears_in_env_example` reads
`.env.example` and the declared set includes vars the branch's voice example omitted.

## Not moved, and why

`flexus_backend/tests/conftest.py` installs prisma and langgraph stubs and a permissive-authz
fixture for the whole flexus suite. The voice tests request none of its fixtures and import
nothing from `tests/`, so it was dropped rather than dragging prisma generation in.

These ten voice modules exist only to talk to flexus's Postgres, RBAC and GraphQL. They are real
voice work and worth re-grounding later, but each needs an identity and persistence model this
repo does not have:

| Module | Lines | Needs |
| --- | ---: | --- |
| `voice_handoff_ops.py` | 214 | `flexus_v1.utils_permission`, `prisma_stubs`, the `flexus_voice_session*` tables. Four authorization checks: workspace, group hierarchy, human access to the target conversation, expert existence. |
| `voice_session_ops.py` | 397 | Postgres session rows. |
| `voice_leg_ops.py` | 255 | Postgres leg rows; the durable spine of a handoff. |
| `voice_return_ops.py` | 64 | Postgres; the committed half of "back to Boss". |
| `voice_transcript_ops.py` | 228 | Postgres plus the langgraph agent runtime. |
| `voice_confirmation_runtime.py` | 228 | `langgraph_runtime.execution` — this is the import that pulled in cocoindex. |
| `voice_control_plane.py` | 255 | The flexus GraphQL surface. |
| `voice_provider_policy.py` | 190 | `langgraph_runtime` bulkheads and flexus metrics. Retry/bulkhead wrapping around the providers. |
| `voice_tool_operations.py` | 118 | `langgraph_runtime.execution`. |
| `voice_job_metadata.py` | 58 | flexus job metadata. |

`flexus_backend/services/service_voice_agent.py` (923 lines) is the runnable worker that ties the
pipeline to those ten. It was not moved for the same reason; `examples/voice_app.py` is a much
smaller stand-in built on `livekit-agents` instead. The `prisma/` schema and `setup.py` were
copied and then removed once the DB-coupled modules were dropped — nothing left reads them.

The frontend files are moved as source only. No `package.json`, build config, or app shell came
with them, so the page does not build here; `realtimeRoomClient.ts` and `voiceOrbShader.ts` are
the reusable parts, and the orb shader is self-contained GLSL.

## The dependency lesson

The first attempt moved the transitive import closure — 187 modules, then the whole backend at
1,325 modules and 378k lines — and installed cocoindex, strawberry-graphql, fastapi, prisma,
pymongo and python-telegram-bot to satisfy it. None of that is voice. The closure explodes because
`voice_confirmation_runtime` imports the agent runtime, and because namespace packages use
relative imports (`from . import errors`) that pull in every sibling.

Cutting the ten coupled modules drops the footprint to 34 modules whose only third-party needs are
httpx, jsonschema, PyJWT, livekit, numpy, redis and scipy. Before adding a dependency, check
whether it arrived through a module that is not actually voice.
