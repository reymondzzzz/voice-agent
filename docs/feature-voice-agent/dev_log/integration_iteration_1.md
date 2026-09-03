# Integration iteration 1 — assemble the voice-agent prototype foundation

Date: 2026-08-31. Integration branch: `voice-agent`.

## Goal

Bring the independently developed voice-agent prototype lanes onto one branch, preserve
their evidence and development history, resolve integration drift against current Flexus,
and leave a clear boundary between what is executable now and what still has to be built.

This iteration integrates the planning foundation, provider measurements, local media
plane, browser room client, session-start control path, and generic per-call worker. It does
not claim the end-to-end spoken loop is complete.

## What was integrated

The branch starts with the Boss voice-agent architecture and task backlog from commit
`814c59a4c`, then combines these Paperclip lanes:

| Task | Result on `voice-agent` |
| --- | --- |
| VA-001 / PER-4 | Versioned prototype contracts, dependency pins, self-hosted-only guard, and environment contract |
| VA-002 / PER-5 | OpenRouter utterance STT adapter, corpus, latency/cost measurements, and provider evidence |
| VA-003 / PER-6 | Incremental bounded PCM TTS adapter, Boss/Sidra voice benchmark, cancellation, usage, and cost evidence |
| VA-004 / PER-7 | Common machine-readable latency trace and synthetic-call harness |
| VA-101 / PER-8 | Isolated self-hosted `livekit-server` development stack with dedicated Redis and local transport configuration |
| VA-102 / PER-9 | Authenticated prototype session start, room-scoped participant token, signed opaque job metadata, and explicit worker dispatch |
| VA-103 / PER-10 | Browser LiveKit room lifecycle, continuous microphone publication, remote audio playback, and effective capture diagnostics |
| VA-104 / PER-11 | One generic worker and one isolated warm call actor per dispatched room, with lifecycle, cancellation scopes, capacity, and correlation IDs |

PER-3 carried the delegated backlog context and PER-71 pointed at the integration base; they
added no separate runtime lane. Their branches were still checked for ancestry before the
Paperclip worktrees were removed.

The branch contains no managed LiveKit dependency. `livekit-server` is local and
self-hosted, and the application guard rejects managed LiveKit hostnames at admission.
OpenRouter is used only for the prototype STT and TTS provider measurements; local STT and
TTS remain the M7 direction after the runtime contracts are proven.

## What was tried

Each Paperclip branch was first treated as an independent evidence lane. The provider lanes
were integrated before the LiveKit runtime lanes so that the shared contracts and latency
schema existed before media and worker wiring. The local LiveKit stack was then integrated
before token dispatch, browser media, and the call actor, following the backlog dependency
graph.

The merge retained each lane's focused dev log and benchmark artifacts. Logs that arrived
with generic iteration filenames were namespaced by task (`va002_`, `va003_`, `va004_`,
`va101_`, `va102_`, `va103_`, and `va104_`) so one lane could not overwrite another lane's
history.

After all substantive branches were present, the combined voice backend and frontend tests
were run together. The final integration change adapted the session-start authorization to
the RBAC v3 API already present on the target branch.

## What broke

### Independent lanes used overlapping log names

Several branches created an `iteration_3.md` or `iteration_4.md`. Git could merge code from
those lanes, but those names did not identify which experiment or runtime task produced the
record and would collide as development continued.

The logs were renamed by VA task before the remaining branches were integrated. The content
was preserved; only its identity in the feature history changed.

### The prototype authorization was based on the superseded role API

VA-102 was developed against the earlier write-role authorization contract. The integration
base had already completed the RBAC v3 cutover, where the start mutation must require named
capabilities instead of `require_role_or_403(WRITE)`.

The mutation now follows the current chain:

```text
whos_that
  -> authorize_for_group
  -> require agent:run for the group
  -> require conversation:create for the group
  -> require a human caller
  -> apply the per-user session-start rate limit
```

The test asserts both capability checks and proves a denied caller never reaches the media
control plane. The prototype contract was updated with the same chain so documentation and
runtime behavior do not disagree.

### The experimental ARQ comparison was not suitable to commit as-is

The local `flexus_backend/experiments/voice_streaming/` directory remains untracked. It was
useful for the first ARQ-versus-warm-actor comparison, but its co-located tests and Markdown
do not satisfy repository placement rules. The production-safe result is represented by the
committed synthetic-call harness, latency trace, benchmark artifact, and mirrored tests.

## Decisions and why

### Realtime turns use a warm actor; ARQ remains for durable work

The synthetic comparison showed head-of-line blocking when every spoken turn enters the ARQ
admission path. A persistent session produced roughly 2.1–2.3 times faster p95 first-audio
latency in the measured scenarios. The resulting split is intentional:

- the LiveKit worker owns the latency-sensitive STT -> agent -> TTS stream for the life of a
  call;
- ARQ continues to own asynchronous, retryable, and durable background work delegated by
  Boss or another agent.

This is not a second agent runtime. The call actor must eventually invoke the existing
LangGraph executor contract directly, preserving its snapshots, tools, permissions,
checkpoints, messages, and completion semantics.

### One generic worker serves all Flexus agents

There is no Boss pod and no Sidra pod. A generic `flexus-voice-agent` worker accepts an
opaque room dispatch, creates one actor for that call, and later resolves the active agent
from authoritative Flexus state. Different voices belong to agent voice profiles and to a
conversation leg, not to separately deployed worker types.

This keeps future Boss-to-Sidra switching inside the same room and actor. Group communication
is deliberately deferred; the first handoff model has one active agent at a time. A future
design may add multi-agent participation without changing the current single-active-agent
contract silently.

### The media plane carries audio, not authority

The browser receives a short-lived token scoped to one opaque room. The worker receives only
signed, expiring, opaque metadata. Workspace, group, user, conversation, agent, permissions,
and tool policy do not come from LiveKit metadata. The durable milestone must load them from
Flexus by `vsession_id` before an agent turn can run.

### Barge-in cancels a turn, not the call

The actor separates turn-scoped and call-scoped work. VA-106 now commits a basic interruption
after 250 ms of contiguous speech while SPEAKING, cancels TTS/publication for the current
turn, and rejects stale frames without destroying the room, session lease, or background
operations. It ignores new PCM while STT is already TRANSCRIBING. The two-phase policy,
STT-phase behavior, and false-positive resume remain VA-303.

VA-403 narrows that wording further: barge-in cancels the speech turn's presentation, not its
executor operation. The executor is call-scoped and shielded while the superseding transcript uses
the existing durable queue boundary. Explicit operation cancellation is separate, request-idempotent,
and allowed only for declared-cancellable work. Late terminal outcomes stay attached to their
originating `turn_id` and cannot mutate a newer voice state.

### Capacity is measured in sessions per worker

One worker process holds multiple isolated calls. The prototype default is four concurrent
sessions per replica, but it is explicitly unmeasured. VA-106 established in-process
1/5/10/20-call isolation without deriving a pod limit; VA-603 owns real-room resource,
provider, 50/100-room load, and soak evidence. Pod
count and autoscaling must follow available session slots plus CPU, memory, and latency —
not room count alone and not an assumed permanent four-session limit.

## Current executable boundary

The branch can now provide the pieces around a local call:

1. start the isolated local LiveKit stack;
2. authenticate a human and mint a short-lived room token;
3. explicitly dispatch the generic worker with signed opaque metadata;
4. connect the browser, publish the microphone continuously, and attach agent audio;
5. create a bounded, isolated call actor with structured IDs and cancellation scopes;
6. detect one bounded utterance, run STT, echo the final text through incremental TTS, and
   publish exact 50 ms PCM frames back into the room;
7. record a privacy-safe echo timing trace without a fabricated agent run.

The branch does not yet execute Boss, call tools, write voice sessions through an authorized
API, perform adaptive two-phase interruption, switch to Sidra, resume after reconnect, or
scale to one hundred real rooms. Those are later milestones, not hidden behavior in the
current echo prototype.

## Verification

Integration verification covered the combined backend voice slice and frontend voice
client rather than trusting each branch's isolated result. The merged branch produced:

```text
backend voice/integration selection: 380 passed, 2 skipped
frontend voice client selection:      12 passed
```

The two skipped backend cases are explicit provider tests that require live credentials;
their committed provider artifacts and lane logs record the credentialed runs. The local
LiveKit and worker lanes also retain their real pinned-server and pinned-SDK evidence in
their task-specific logs.

## Worktree integration and cleanup

All ten Paperclip worktrees were checked before removal. Each was clean and its branch tip
was an ancestor of `voice-agent`. The worktree directories were then removed through Git;
the `PER-*` branches were preserved, so every source branch and commit remains recoverable.
The untracked local `flexus_backend/experiments/voice_streaming/` directory was not removed,
staged, or committed.

## Next iteration

VA-203 is complete. A signed accepted LiveKit job acquires a compare-token Redis lease, claims a
monotonic PostgreSQL actor epoch, loads the authoritative participant/agent/leg/conversation,
keeps the same leg through a fixed reconnect window, and closes idempotently. Lease renewal plus
durable epoch validation prevents an expired old worker from writing after replacement. Each job
owns a 1–2 connection PostgreSQL pool and one Redis client, and all admission, provider, SDK,
lease-loss, reconnect, and shutdown paths release them.

```text
VA-203 focused backend selection: 194 passed
VA-203 PostgreSQL lifecycle e2e:      3 passed
VA-203 focused Ruff selection:        passed
VA-203 commit gate:                   9/9 passed
```

VA-204 now commits a finalized nonblank transcript exactly once as the ordinary human message,
before TTS, and returns the durable message reference without enqueueing ARQ. Concurrent database
replay produced one message and one audit.

VA-205 now sends only a newly inserted visible voice message into the existing executor from the
warm actor. It uses the same registry, checkpointer, predefined tools, RequestContext, policy,
history, persistence, confirmation, and finish path as ARQ. Task-local PostgreSQL binding and a
fresh-`RUNNING` claim gate serialize voice and ARQ without holding a connection during model or
tool work. Replays and queued inserts never start a second direct run; the normal ARQ pool remains
available for promoted follow-ups. Slow cancellation recovery stays actor-owned and call resources
close only after it drains.

VA-206 replaces the durable call's transcript echo with Boss's actual terminal response. The
executor returns final messages only after normal idle completion with no retrigger, promoted
continuation, interrupt, or expert handoff. A fail-closed segmenter selects only the last safe
post-human assistant response and removes tool payloads, reasoning, code, URLs, and markup before
bounded phrase splitting. Phrases use sequential TTS streams, one sink admission, and continuous
frame numbering. Barge-in now cancels the owned run during `THINKING` as well as provider playback
during `SPEAKING`.

VA-207 adds the first durable Boss call control to the ordinary chat workspace. The worker sends
only bounded versioned actor state over a private LiveKit data topic; newly committed human voice
messages and normal executor assistant messages continue through the existing chat event path.
Transient reconnect preserves media, terminal disconnect refreshes the same session, and ambiguous
refresh/end outcomes retain the opaque recovery identity. Tokens are never stored. The next task is
VA-208 end-to-end proof of the five-turn durable vertical slice.

VA-208 closes M2 with executable layered evidence. A real PostgreSQL acceptance test creates one
authorized session on an existing conversation, commits five distinct direct-run-eligible voice
turns, independently exercises five production run-gate claims, rejects replay as a message and
run candidate, refreshes and reconnects the same participant and leg, ends the session, reads the
five rows through ordinary GraphQL chat, and appends and reads a normal text message afterward.
VA-205 separately proves the eligible-result-to-executor wiring. Frontend acceptance remounts the
page and rejoins that same session without another create, then proves voice-provenance content
renders in the ordinary chat timeline.

```text
VA-208 PostgreSQL vertical slice: 1 passed
VA-208 focused frontend:         29 passed
VA-208 full frontend:            1066 passed
```

The next dependency-ordered task is VA-301 browser AEC, noise-suppression, and automatic-gain
compatibility evidence. Group and conference communication remains explicitly deferred.

VA-301 implementation is ready for physical evidence. The browser now reports sanitized effective
settings without treating them as acoustic proof, and the prototype has a local-only AEC-off versus
AEC-on speaker probe with conservative pass, fail, and inconclusive outcomes. The first pilot is
desktop Chrome only; its built-in path has three physical passes. The headphone control is an
explicitly deferred follow-up and is not claimed as passed. Safari and Firefox are deferred to a
later compatibility expansion. Mobile stays outside the pilot until real iOS and Android device
evidence exists.

VA-302 makes each speech-output boundary finite and cancellable. Text and TTS admission, PCM
framing, and LiveKit capture now use explicit item and age limits, preserve FIFO order, reject stale
turn or speech owners, and flush obsolete output. OpenRouter raw and total PCM retention are capped,
and a blocked LiveKit capture fences its sink after a bounded deadline. Terminal turns publish
content-free queue pressure independently of the versioned latency artifact.

VA-303 replaces committed-only barge-in with two phases. At 100 ms of probable speech the actor
holds its old turn and generation, pauses the Flexus playout gate, and clears bounded LiveKit
source audio. At 250 ms the candidate receives one bounded STT classification. Nonblank speech
commits terminal cancellation and becomes the sole input to a fresh turn; short bursts, empty STT,
provider failure, and timeout resume the exact old owner without a phantom message. Stale pause or
resume requests cannot revive an invalidated generation. LiveKit provides transport only; no Cloud
adaptive-interruption service is present.

VA-304 makes the existing speech rows authoritative. `RunCompletion` now returns the exact
assistant message sequence from the executor's own persisted rows, and only a safe plan carrying
that identity can open a durable speech record. The actor renews its lease and verifies the active
participant, epoch, leg, conversation, and assistant row before TTS. One terminal write records
generated and LiveKit-accepted PCM duration plus the last fully completed segment boundary.
Identical retries are no-ops, conflicts fail closed, and no frame loop performs database work.

VA-305 makes the interruption policy injectable and adds an offline deterministic corpus covering
genuine English/Russian interruptions, click, cough, keyboard, echo leakage, empty/error/timeout
STT, and exact acknowledgement backchannels. The 150/250/350/500 ms sweep retains 250 ms: it keeps
all six genuine commits and zero false commits, while 350 ms drops recall to 50%. Code-level pause
and resume sink acknowledgements are reported separately from the 600 ms end-of-speech window and
STT classification time. The committed artifact declares that it did not exercise physical audio,
LiveKit RTC, TURN, or external providers.

VA-401 connects real executor tool-call/result updates to the existing `TOOL_ACTIVE` presentation
state through a boolean-only, turn-fenced callback. Opaque tool-call correlation never leaves the
executor; a server-owned policy admits only known Boss foreground operations, and callback failure
cannot change the durable run. All tool-derived strings stay on the ordinary chat activity path. The browser renders a
fixed localized label, parallel calls coalesce correctly, unfinished activity clears on exit, and
rapid activity/completion packets remain ordered. The first version emits no spoken filler and
keeps normal final assistant speech as the sole audible result.

VA-402 connects the existing durable signed confirmation interrupt to both speech and the Boss
call control. The executor notifies the voice actor only after the interrupt is persisted and never
for a stale cancelled/idle finish. The actor speaks fixed copy and accepts only exact
approve/reject phrases; ambiguity and silence do nothing, while speech onset cancels only prompt
playout. Spoken choices resume the same checkpoint with the database-stored token. The generic
voice card reuses the ordinary authorized resolver, and a fenced status probe observes its ARQ
continuation without a duplicate local run. Different pending resumes cannot overwrite each
other, reconnect restores a real pending wait, and probe failure closes the local call.
