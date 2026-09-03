# Voice agent prototype contracts

Contract version: **1.2.0** (`voice_contracts.VOICE_CONTRACT_VERSION`)
Integrated tasks: VA-001 contracts, VA-002 STT, VA-003 TTS, VA-004 latency tracing,
VA-101 local LiveKit, VA-102 token plus explicit dispatch, VA-103 browser room client,
and VA-104 generic worker plus per-call actor skeleton.
Architecture source: commit `814c59a4cd12401100bf6e2e6908acf7bf83aad4`
(`docs/voice-agent/implementation_plan.md`, `docs/voice-agent/task_backlog.md`; read with
`git show`). That commit is not merged here, so this document repeats every value the
implementation lanes need instead of linking into it.

Executable source of truth: `voice_agent/pipeline/voice_contracts.py`.
Everything below is asserted by `tests/pipeline/test_voice_contracts.py`
and `flexus_backend/tests/_project/test_no_livekit_cloud_endpoints.py`. If a value here and
a value in the module disagree, the module wins and this document is a bug.

VA-001 shipped these values as data only. VA-102 added the first control-plane consumer:
`voice_prototype_session_start` imports `voice_contracts`, reads the LiveKit environment
variables, and answers 404 unless `FLEXUS_VOICE_ENABLED=1`. No image gained a dependency —
the `voice` extra is still absent from `requirements-ci.lock` — and no pre-existing code
path changed.
VA-104 added the first worker process that reads the values: the generic
`flexus-voice-agent` worker, which stays out of `requirements-ci.lock` and refuses to start
unless the same prototype gate is enabled.

## Versioning rule

`VOICE_CONTRACT_VERSION` is semver over the values in `voice_contracts.py`.

* patch — clarification or a new value that no lane could already depend on;
* minor — a new field, ID, env var, or cancellation reason;
* major — a changed sample rate, PCM layout, endpoint, ID format, or deadline, i.e. any
  change that invalidates a benchmark already recorded against the old contract.

Every change records the version bump and the reason in the changelog at the bottom.
Benchmark artifacts from VA-002/VA-003/VA-004 must carry the contract version they ran
against; results measured under a different major version are not comparable.

## Pinned dependency versions

| Component | Pin | Where it lives |
| --- | --- | --- |
| LiveKit server | `livekit/livekit-server:v1.13.6` | `LIVEKIT_SERVER_IMAGE` and the opt-in `compose.voice.yml` service |
| LiveKit Python RTC SDK | `livekit==1.1.15` | `setup.py` extra `voice` |
| LiveKit Python API SDK | `livekit-api==1.2.1` | `setup.py` extra `voice` |
| LiveKit Python agents framework | `livekit-agents==1.7.1` | `setup.py` extra `voice` |
| LiveKit browser SDK | `livekit-client@2.22.1` | `LIVEKIT_BROWSER_CLIENT_PIN`, `flexus_frontend/package.json`, and `flexus_frontend/pnpm-lock.yaml` |

`livekit==1.1.15` is not a choice. `livekit-agents==1.7.1` declares `livekit==1.1.15`
exactly, so the newer `livekit==1.1.16` makes the extra unresolvable. Bumping either pin
means checking the other; `pip install --dry-run -r <the three pins>` is the check.

The `voice` extra is intentionally excluded from the `uv pip compile` extra list that
produces `requirements-ci.lock`. CI and the production images therefore never install the
realtime media stack, which keeps prototype-only code out of production until its gate
passes. A test asserts the exclusion. Adding `--extra voice` to that command is a
deliberate production decision, not a lockfile refresh.

`voice_contracts.py` imports nothing from LiveKit. It is a data contract that must import
cleanly in CI, where the SDKs are absent.

## Self-hosted LiveKit only

`require_self_hosted_livekit_url(vlk_url)` is the single admission point for a LiveKit
signal URL. It accepts `ws://` and `wss://` with a real host, and raises `ValueError` for
any managed LiveKit host, for a non-signal scheme, and for a hostless URL. Local
development uses `ws://localhost:7880` (`LIVEKIT_LOCAL_DEV_URL`); production uses trusted
TLS on `wss://`.

The host is compared case-folded and with trailing dots stripped, because
`wss://flexus.livekit.cloud.` is the same DNS name as `wss://flexus.livekit.cloud` and
resolves identically. The comparison is on the parsed host boundary, not a substring, so a
self-hosted `notlivekit.cloudy.internal` is still accepted.

`test_no_livekit_cloud_endpoints.py` scans the git index with
`git grep --cached -I --ignore-case --fixed-strings` and fails if the managed LiveKit host
appears in any tracked non-`docs/` file other than the rejection guard and its unit test. Delegating the walk to git is what makes the scan total: it covers extensionless
files such as `Dockerfile.backend` and deploy entrypoints, any extension the repository
grows later, and any letter case, while skipping binaries without a suffix allowlist to
maintain. It also plants synthetic offenders in temporary repositories each run — including
an uppercase host in an extensionless file — so the scan cannot quietly become vacuous.

Managed LiveKit inference, enhanced noise cancellation, and adaptive interruption are out
of scope and are not runtime dependencies.

## OpenRouter speech endpoints

```text
POST https://openrouter.ai/api/v1/audio/transcriptions
POST https://openrouter.ai/api/v1/audio/speech
```

STT request fields: `model`, `input_audio` (`{data: base64, format}`), `language`.
The transcription endpoint is the same one `flexus_backend/services/workspace_stt.py`
already calls, and a test pins the two constants together so they cannot drift.

**OpenRouter accepts a `prompt` field on transcription and ignores it.**
`OPENROUTER_STT_PROMPT_VOCABULARY_SUPPORTED` is therefore `False`. The architecture plan
listed optional prompt vocabulary (Flexus, Boss, Sidra, agent names, project names) as an
input; that lever does not exist on this provider. VA-002 must measure named-entity
accuracy without it, and any vocabulary biasing has to wait for the local STT engine in
M7 or for a provider that honours the field.

TTS request fields: `model`, `input`, `voice`, `response_format`, `speed`. The contract
requests `response_format="pcm"` and consumes the response incrementally; waiting for a
complete MP3 is a contract violation, not a tuning choice.

The provisional STT default is configuration, not architecture:
`openai/gpt-4o-mini-transcribe`, overridable via `FLEXUS_VOICE_STT_MODEL`. TTS selection is
owned by the persisted semantic agent voice profile and the server registry; no process-level
model override may replace the profile frozen on an active leg. The binding constraint for STT is
the 1000 ms end-of-utterance-to-final-STT conversational p95 budget; VA-002 measured a
909 ms short-utterance p95 for `openai/gpt-4o-mini-transcribe`. The rejected provisional
default, `openai/whisper-large-v3-turbo`, measured 2927 ms at p95 and a 5.4 s worst short
request despite being 9.6x cheaper and producing stronger English and Spanish word error
rates. Selecting the measured default is a configuration change and does not bump
`VOICE_CONTRACT_VERSION`, which remains `1.0.0`.

The session actor sends `SttConfig.sttc_language` whenever the workspace or user has a
selected locale. Provider auto-detection is used only when neither has selected a locale.
In VA-002, explicit Russian hints eliminated the wrong-script transcripts observed under
auto-detection.

## PCM format and sample rates

| Value | Contract | Reason |
| --- | ---: | --- |
| encoding | `pcm_s16le` | signed 16-bit little-endian is what `rtc.AudioFrame` carries |
| channels | 1 | one human, one agent; stereo buys nothing and doubles the STT payload |
| room sample rate | 24000 Hz | `livekit-agents` 1.7.1 `AudioInputOptions`/`AudioOutputOptions` both default to 24000 mono |
| TTS sample rate | 24000 Hz | matches the room, so published frames need no resample |
| STT sample rate | 16000 Hz | whisper-family native rate; the actor downsamples once before upload |
| RTC frame | 50 ms | `livekit-agents` publishes `sample_rate // 20` samples per frame and reads input at `frame_size_ms=50` |
| RTC playout queue | 200 ms | `livekit-agents` 1.7.1 `room_io/_output.py` builds `rtc.AudioSource(..., queue_size_ms=200)`; the bounded-queue work in VA-302 sizes against it |
| max utterance | 30 s | bounds one STT request; 30 s of 16 kHz mono s16le is 960 000 bytes |

The playout queue is 200 ms, not the 1000 ms `rtc.AudioSource` documents as its default:
`livekit-agents` overrides it on the RoomIO publish path, and RoomIO is the path this
prototype uses. `RoomOptions` exposes no setting for it, so 200 ms is a fact about the
pinned SDK rather than a value VA-302 may tune — changing it means changing SDK version or
leaving RoomIO.

The room sample rate, the RTC frame, and the playout queue were all read out of
`livekit-agents==1.7.1`, so a test asserts all three as exact values *and* asserts the
agents pin they came from. Bumping the agents pin fails that test until someone re-reads
`room_io/types.py` and `room_io/_output.py` at the new version and updates the numbers
together. Relational assertions are not enough here: 1000 ms is also a whole number of
50 ms frames, so a divisibility check accepts the wrong value.

`pcm_frame_bytes(sample_rate, frame_ms)` and `pcm_duration_seconds(byte_count, sample_rate)`
are the only sanctioned conversions. `pcm_frame_bytes` refuses a rate that does not divide
into whole samples over the requested duration, so a silently truncated frame cannot reach
the room. At the pinned values one RTC frame is 2400 bytes.

OpenRouter's [TTS guide](https://openrouter.ai/docs/guides/overview/multimodal/tts)
identifies the endpoint as OpenAI Audio Speech compatible but does not repeat the layout of
its `pcm` response. The linked [OpenAI format
contract](https://developers.openai.com/api/docs/guides/text-to-speech#supported-output-formats)
defines raw PCM as 24 kHz signed 16-bit little-endian, and Kokoro's [reference
CLI](https://github.com/hexgrad/kokoro/blob/main/kokoro/__main__.py) writes its output as
24 kHz mono, two-byte PCM. VA-003 validates those facts together before calculating duration
and rejects any response metadata that contradicts the pinned rate, channels, width, sign,
or byte order. The adapter, not the session actor, owns any future resample needed to reach
24000 Hz mono s16le.

One yielded provider-adapter chunk is capped at 9600 bytes, the 200 ms RoomIO queue budget.
Smaller HTTP chunks are yielded immediately rather than buffered to an RTC frame. A single raw
provider chunk above one second of PCM or a response above the 60-second stream deadline is
rejected before more output is retained. The actor reframes accepted chunks through a four-item,
200 ms PCM queue, then awaits a separate four-frame, 200 ms LiveKit source queue.

The other actor-owned bounds are four text segments for 30 seconds and one TTS request for one
second. All queues are owner-fenced by turn and speech identifiers, enforce monotonically ordered
items, flush on cancellation, reject stale output, and expose content-free pressure snapshots.
LiveKit capture has a 200 ms deadline; a deadline failure clears queued audio and permanently
fences that sink instance so a later speech cannot inherit the blocked source.

## Speech segmentation

`VOICE_SEGMENT_MIN_CHARS = 40`, `VOICE_SEGMENT_MAX_CHARS = 160`,
`VOICE_SEGMENT_MAX_BUFFER_MS = 250`. These are the architecture plan's starting candidates
and belong to the VA-004 latency benchmark. Tool-call JSON, Markdown syntax, URLs, code
blocks, and hidden reasoning are never spoken.

## Cancellation

Every stop carries one reason from `VOICE_CANCEL_REASONS`:

| Reason | Meaning |
| --- | --- |
| `barge_in` | committed human interruption (VA-303 phase 2A) |
| `handoff` | the active leg changed agent; the old leg's output is obsolete |
| `session_end` | the call ended or the lease was lost |
| `deadline` | a stage exceeded its budget below |
| `provider_error` | STT/TTS/LLM failed and the turn cannot continue |
| `stale_turn` | output arrived for a superseded `turn_id`/`speech_id` |

Budgets, ordered and asserted in that order by test:

```text
VOICE_CANCEL_STOP_PUBLISH_DEADLINE_S     = 0.2   stop emitting RTC frames after cancel
VOICE_CANCEL_PROVIDER_CLOSE_DEADLINE_S   = 1.0   close the provider stream after cancel
VOICE_TTS_FIRST_BYTE_DEADLINE_S          = 3.0   first PCM byte or fail the segment
VOICE_ACTOR_INIT_DEADLINE_S              = 5.0   bounded per-call initialization before the call is admitted
VOICE_STT_REQUEST_DEADLINE_S             = 15.0  one utterance transcription
VOICE_TTS_STREAM_DEADLINE_S              = 60.0  one whole synthesized segment
```

These are provisional prototype budgets. VA-004 and VA-305 replace them with measured
values; replacing them is a major version bump because recorded benchmarks stop being
comparable.

Cancellation semantics that are contract, not tuning: stopping speech never cancels or
rolls back a tool operation. VA-403 keeps the executor call-scoped and shielded from turn
cancellation. Explicit operation cancellation is a separate request-idempotent path and is allowed
only for active work whose server declaration is cancellable; committing, background, unknown, and
parallel mixed work fail closed. A consumer rejects any item whose `turn_id` or `speech_id` is no
longer current instead of playing it; a false interruption resumes without creating a durable user
message.

## Identifiers

Every log, metric, and trace carries the relevant subset of
`VOICE_CORRELATION_ID_FIELDS`:

```text
vsession_id  vleg_seq  voice_utterance_id  turn_id  run_id  speech_id  segment_index
conversation_id  agent_id  workspace_id  livekit_room_sid  provider_generation_id
```

Names, transcript text, and raw audio are never metric labels and never log bodies.

Room names, participant identities, and session IDs are opaque: `new_opaque_voice_id`
returns `vroom_`, `vpart_`, or `vsess_` followed by 32 hex characters from
`secrets.token_hex`. A room name that embeds a workspace, agent, or person is a privacy
leak into LiveKit metadata, so it is rejected rather than discouraged.

There are two validators and the difference matters. `is_opaque_voice_id` answers "is this
*an* opaque voice ID"; `is_opaque_voice_id_of(vid_value, vid_prefix)` answers "is this
opaque ID *of that kind*", and raises for an unknown kind. Only the second is safe at a
boundary: the three prefixes share one shape, so the loose check happily accepts a session
ID where a room name belongs and would let a caller scope a token to the wrong identifier.
Every mint and every parse uses the prefix-specific form.

## Prototype token and explicit dispatch

`VOICE_AGENT_WORKER_NAME = "flexus-voice-agent"` is the single generic worker name. It is a
contract because the worker registers under it and the control plane dispatches to it; there
is no per-agent worker and Boss is not a worker name.

`VOICE_PARTICIPANT_TOKEN_TTL_S = 120` bounds the browser's join token, and
`VOICE_JOB_METADATA_TTL_S = 120` bounds the worker's job metadata. The dispatch call's own
`roomAdmin` token lives 30 seconds (`voice_livekit_tokens.VOICE_DISPATCH_ADMIN_TOKEN_TTL_S`)
and never leaves the backend. LiveKit tolerates roughly a minute of clock skew past `exp`,
so treat these as bounds, not as fences accurate to the second.

A LiveKit access token is an HS256 JWT with `iss` (API key), `sub`/`identity`, `iat`, `nbf`,
`exp`, and a `video` grant. `voice_livekit_tokens.py` builds it directly rather than through
`livekit-api`, because the `voice` extra is deliberately absent from `requirements-ci.lock`
and a module-scope SDK import would crash a backend image that never installs it. The claim
shape is pinned against `livekit/protocol`'s `auth.ClaimGrants`/`VideoGrant` and asserted by
`tests/pipeline/test_voice_livekit_tokens.py`.

The participant grant is exactly:

```json
{"roomJoin": true, "room": "vroom_...", "canSubscribe": true, "canPublish": true,
 "canPublishSources": ["microphone"], "canPublishData": false, "canUpdateOwnMetadata": false}
```

No `roomAdmin`, `roomCreate`, `roomList`, `roomRecord`, `agent`, `hidden`, or `recorder`, no
participant `name`, and no `metadata`. The room is created by the dispatch call, so the
client never needs `roomCreate`.

Explicit dispatch is `POST {http origin}/twirp/livekit.AgentDispatchService/CreateDispatch`
with `{"agentName", "room", "metadata"}`, authenticated by the `roomAdmin` token —
`EnsureAdminPermission` in `livekit-server` requires `roomAdmin` scoped to that exact room,
which is why the participant token cannot dispatch anything.

`metadata` is the only thing the worker receives, and it carries only:

```json
{"vjob_audience": "flexus-voice-agent-job", "vjob_version": 1,
 "vsession_id": "vsess_...", "vroom_name": "vroom_...", "vjob_expires_ts": 0}
```

signed with `flexus_backend/signed_payload.py` (HMAC-SHA256 over canonical JSON under
`SESSION_SECRET_KEY`). `vjob_audience` is what stops a confirmation token or a branch-currency
token from being replayed as job metadata, and `vjob_expires_ts` stops a leaked dispatch from
being replayed later. Workspace, group, user, conversation, and agent identifiers are
absent by construction — the worker will fetch authoritative session data from Flexus, per
the architecture plan, and must never trust LiveKit metadata for identity or permission.

## Worker and call actor

One dispatched room owns exactly one `VoiceCallActor`. The worker is generic: the same
`flexus-voice-agent` image serves every Flexus agent, and
`flexus_backend/tests/_project/test_generic_voice_worker.py` fails if a voice runtime
source names one.

```text
VOICE_WORKER_DISPATCH_NAME        = "flexus-voice-agent"   LiveKit explicit-dispatch pool name
VOICE_WORKER_DEFAULT_MAX_SESSIONS = 4                      unmeasured prototype slot count; VA-603 measures pod capacity
```

Dispatch metadata is the signed, expiring `voice_job_metadata` payload shown above. Admission
and entrypoint validation both require its opaque room to equal the actual dispatched room.
It contains no agent, display name, permission, workspace, group, human, leg, or conversation;
the worker loads those values from the locked Flexus session. The actor uses the durable active
leg sequence rather than assuming the first leg.

Lifecycle states are `VoiceCallState`, transcribed from the architecture plan. The
transition rule is the plan's own: `CREATING` reaches only `CONNECTING` or `CLOSING`,
`CONNECTING` reaches only `LISTENING` or `CLOSING`, any active state reaches any other
active state or `CLOSING`, and `CLOSED` is terminal. Initialization is bounded by
`VOICE_ACTOR_INIT_DEADLINE_S`; a call that misses it is never admitted.

Cancellation is scoped. Turn-scoped work is cancelled by `cancel_turn` and by the next
`begin_turn` (reason `stale_turn`); call-scoped work survives both and is cancelled only by
`aclose`. Turn IDs are `<vsession_id>.<vleg_seq>.<vturn_seq>` with `.speech` and
`.utterance` suffixes: monotonic, greppable, and free of room names, people, and agents.

Capacity is explicit, not CPU-derived. `worker_capacity` reports max/active/available slots
from the LiveKit server's own active job list, `session_load` feeds `load_fnc` with
`load_threshold=1.0`, and `decide_admission` refuses a second dispatch for a room that
already owns an actor (terminal) before it refuses a full or draining worker (retryable on
another replica).

## Environment variables

| Variable | Required | Purpose |
| --- | --- | --- |
| `FLEXUS_VOICE_ENABLED` | no | prototype gate; session creation stays refused unless `1` |
| `FLEXUS_VOICE_LIVEKIT_URL` | yes | self-hosted signal URL, `ws`/`wss`, managed hosts rejected |
| `FLEXUS_VOICE_LIVEKIT_API_KEY` | yes | mints short-lived room-scoped access tokens |
| `FLEXUS_VOICE_LIVEKIT_API_SECRET` | yes | server-side only, never sent to a client |
| `FLEXUS_VOICE_REDIS_DB` | no | Redis database index isolating actor leases; defaults to `VOICE_DEFAULT_REDIS_DB` (`13`) |
| `FLEXUS_VOICE_STT_MODEL` | no | OpenRouter transcription model ID override |
| `FLEXUS_VOICE_WORKER_MAX_SESSIONS` | no | per-replica voice session slots; defaults to `VOICE_WORKER_DEFAULT_MAX_SESSIONS` |
| `OPENROUTER_API_KEY` | yes | shared existing credential, reused for voice STT and TTS |

`VOICE_ENV_VARS` is the machine-readable version of this table. A test fails if any entry
is missing from `.env.example`, so the two cannot drift. The LiveKit secret and the
OpenRouter key stay server-side; neither appears in a token, in LiveKit metadata, in a
client response, or in a log.

## Rollout and rollback

VA-001 rolled out nothing: it added a data module, two test files, an unused `setup.py`
extra, and documentation, and no running service imported `voice_contracts` yet. None of
the three tasks adds a migration, a schema change, a lockfile change, or an image change,
so each rolls back by reverting its commit.

VA-101 adds only an opt-in local Compose stack.

VA-102 adds the first runtime path: one GraphQL mutation, `voice_prototype_session_start`.
It runs the standard `whos_that` → `authorize_for_group` → require group-scoped
`agent:run` and `conversation:create` → `require_human_or_403` chain, then refuses with
404 unless `FLEXUS_VOICE_ENABLED=1`. That
variable is empty in `.env.example` and unset in every deployment, so the mutation exists in
the schema and answers 404 everywhere. It writes no row, enqueues no ARQ job, and changes no
existing behavior; rollback is clearing the variable, or reverting the commit.
VA-104 adds the `flexus-voice-agent`
worker process, which no deployment starts: it is not in `compose.yml`, not in an image,
and refuses to run without `FLEXUS_VOICE_ENABLED=1`. Rollback is stopping the process.

## Changelog

| Version | Change |
| --- | --- |
| 1.2.0 | Added the isolated voice Redis default (`13`) used by durable actor leases. The runtime lease TTL and reconnect window remain implementation values owned by VA-203. |
| 1.1.0 | Added `run_id`; the `vsess_` ID kind and prefix validation; token and metadata TTLs; the generic worker name; actor initialization deadline; worker dispatch name, capacity default, and capacity environment variable. All changes are additive, so 1.0.0 benchmark artifacts remain comparable. |
| 1.0.0 | Initial contract: LiveKit pins, OpenRouter endpoints, PCM/sample rates, cancellation reasons and budgets, correlation and opaque IDs, environment variables. |
