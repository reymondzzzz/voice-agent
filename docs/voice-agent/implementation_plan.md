# Flexus Boss voice agent implementation plan

Status: proposed implementation plan
Last updated: 2026-08-30
Execution backlog: [task backlog and milestone gates](./task_backlog.md)

## Intended outcome

Flexus users can open a low-latency voice session with Boss, speak naturally, interrupt the response, delegate durable work, and transfer the live conversation to another Flexus agent such as Sidra without reconnecting the call.

Boss remains the default entry point and organizational coordinator. Voice is an additional interface to the existing Flexus agent runtime; it must not become a second agent system with different permissions, tools, memory, or conversation truth.

The initial speech pipeline uses OpenRouter for speech-to-text and text-to-speech. LiveKit server, TURN, media routing, agent workers, session state, Flexus execution, and all durable data remain self-hosted. STT and TTS move to locally hosted inference later through stable provider interfaces.

## Settled product and architecture decisions

1. LiveKit Cloud and managed LiveKit inference are out of scope. `livekit-server` and LiveKit agent workers run on Flexus-controlled infrastructure.
2. Version 1 supports one human and exactly one active speaking agent in a room.
3. “Boss, ask Sidra…” means asynchronous delegation. Boss stays active while Sidra works through the durable task system.
4. “Boss, call Sidra…” means a live handoff. The same room and media session remain connected, but subsequent turns go directly to a Sidra-owned Flexus conversation.
5. “Boss, come back” is a global voice-session command and restores the original Boss conversation.
6. Each agent has a stable voice profile. Boss and Sidra must sound recognizably different.
7. An admitted voice call owns a persistent realtime session actor. Turns do not compete for a shared ARQ slot before producing speech.
8. The existing Flexus run executor remains the only agent execution implementation. The voice actor invokes it directly and consumes the same streaming events and durable messages.
9. ARQ remains responsible for delegated tasks, schedules, recoverable background work, and non-realtime conversations.
10. OpenRouter is a temporary speech provider. Provider-specific details must not leak into the voice-session, handoff, or Flexus conversation contracts.
11. A LiveKit room is logical media state, not a process or pod. One admitted call owns one persistent voice job actor, and generic voice-worker replicas host multiple call actors up to an explicit measured limit.
12. Agent switching does not allocate another room or actor. Boss and Sidra reuse the same media session while the active Flexus conversation, tool policy, and voice profile change at a committed leg boundary.
13. Realtime speech is a chain of bounded, cancellable streams. Version 1 streams media, LangGraph events, and PCM output, while OpenRouter STT remains utterance-level until a later provider supports partial transcripts.
14. Client-side WebRTC acoustic echo cancellation, noise suppression, and automatic gain control are enabled by default. LiveKit Cloud enhanced noise cancellation and adaptive interruption inference are not dependencies of the local deployment.
15. Barge-in first pauses output tentatively, then either commits the interruption or resumes after a false interruption. Stopping speech never silently cancels or rolls back a tool operation.

> **XXX Future:** Group or conference communication, where Boss and multiple agents listen and speak in the same room, is explicitly excluded from this plan. It requires floor control, transcript routing, mention rules, simultaneous-response prevention, and a separate product design. Revisit it only after one-active-agent handoffs are stable.

## Existing Flexus foundation

This plan builds on existing behavior rather than replacing it:

- A conversation already points to one agent through `flexus_conversation.conv_agent_id`; see [the Prisma model](../../prisma/schema/public.prisma) and [conversation CRUD](../../flexus_backend/flexus_v1/v1_conversation_crud.py).
- `run_executor.execute_run` already claims a conversation, resolves the agent snapshot and tools, loads durable history, streams LangGraph events, persists completed nodes, handles interrupts, and finishes the conversation state; see [run_executor.py](../../flexus_backend/langgraph_runtime/execution/run_executor.py).
- LLM text deltas can already be published as live conversation events through Redis; see [chat_hallucitron.py](../../flexus_backend/langgraph_runtime/llm/chat_hallucitron.py).
- ARQ currently wraps the same executor by conversation ID; see [service_langgraph_arq_worker.py](../../flexus_backend/services/service_langgraph_arq_worker.py).
- The scheduler can create target-agent conversations and start Todo tasks asynchronously; see [service_scheduler.py](../../flexus_backend/services/service_scheduler.py).
- The synthetic voice benchmark already compares a shared per-turn queue with persistent admitted sessions; see [the experiment](../../flexus_backend/experiments/voice_streaming/README.md) and [results](../../flexus_backend/experiments/voice_streaming/RESULTS.md).

The first synthetic measurements support session admission rather than per-turn ARQ admission:

| Scenario | Shared ARQ speech-end to first-audio p95 | Persistent session p95 |
| --- | ---: | ---: |
| warm traffic within capacity | 1,101.8 ms | 1,077.4 ms |
| burst above ARQ capacity | 2,367.0 ms | 1,104.6 ms |
| slower tool/reasoning turn above capacity | 4,625.9 ms | 2,050.8 ms |

These are scheduling experiments with synthetic provider stages, not claims about real OpenRouter or WebRTC performance. They justify the runtime shape and define what the next prototypes must measure.

## Scope

### Included

1. Browser or mobile client joins a self-hosted LiveKit room with a short-lived token.
2. Voice session starts with Boss and a normal human-owned Boss conversation.
3. Local VAD and turn handling determine the end of the user’s utterance.
4. OpenRouter STT returns a finalized transcript for the completed utterance.
5. The transcript is durably appended to the active Flexus conversation.
6. A persistent voice actor invokes the existing Flexus run executor directly.
7. Flexus text deltas are segmented and sent to streaming OpenRouter TTS.
8. PCM audio is published into the same LiveKit room.
9. User barge-in stops audio and cancels the obsolete generation safely.
10. Boss can request a durable asynchronous delegation to another agent.
11. Boss can request a live handoff to another agent.
12. Handoff creates or resumes a human-owned conversation tied to the target agent.
13. Agent-specific voice selection changes at the handoff boundary.
14. The user can return to Boss without leaving the room.
15. Metrics, audit records, permissions, idempotency, quotas, and overload behavior are defined and tested.
16. Supported clients apply WebRTC AEC, noise suppression, and automatic gain control before uplink.
17. Text, TTS, PCM, and RTC stages use bounded queues and reject stale turn/speech output.
18. Genuine and false interruptions follow separate commit/resume paths.
19. Concurrent-room admission and voice-worker autoscaling use explicit measured session slots.

### Explicit non-goals

- multiple simultaneously active AI agents;
- group or conference calls;
- PSTN/SIP telephony in the first release;
- call recording by default;
- voice cloning or user-uploaded voice samples;
- replacing the Flexus agent runtime with LiveKit’s LLM logic;
- executing delegated agent work inside the realtime voice actor;
- one Kubernetes pod per Flexus agent;
- local STT/TTS in the first provider benchmark;
- automatic sharing of a full Boss conversation with a target agent.

## Product behavior

| User phrase | Meaning | Active agent afterward | Durable effect |
| --- | --- | --- | --- |
| “Boss, summarize today’s work.” | normal Boss turn | Boss | messages in Boss conversation |
| “Boss, ask Sidra to prepare the report.” | asynchronous delegation | Boss | target-assigned Kanban task and target execution conversation |
| “Boss, call Sidra.” | live handoff | Sidra | new voice leg and Sidra conversation |
| “Boss, call Sidra about that report.” | live handoff with scoped context | Sidra | voice leg plus a short handoff context item |
| “Boss, come back.” | global return command | Boss | close Sidra leg; resume original Boss conversation |
| “Stop.” or user speech during playback | barge-in | unchanged | stop obsolete speech generation/audio; any tool follows its explicit cancellation policy |

The interface must always show the active agent’s name, avatar, voice state, and conversation status. A live handoff is not complete until the target agent is authorized, its conversation is ready, and the voice coordinator has switched the active leg.

## Runtime topology

```text
Browser / mobile client
  microphone + speaker + call UI
          |
          | WebRTC audio/data with short-lived room token
          v
Self-hosted livekit-server / TURN
          |
          | one room job assigned by explicit dispatch
          v
flexus-voice-agent deployment
  persistent actor for each admitted call
  - LiveKit AgentSession / room I/O
  - VAD and turn state
  - OpenRouter STT adapter
  - active Flexus conversation leg
  - direct Flexus run executor invocation
  - text segmentation
  - OpenRouter TTS adapter
  - barge-in and handoff coordinator
          |
          +---- PostgreSQL: conversations, messages, voice sessions, audit
          +---- Redis: checkpointer, live events, ephemeral leases
          +---- OpenRouter: temporary STT and TTS only
          |
          +---- ARQ / scheduler: delegated and asynchronous agent work
```

### Critical realtime path

```text
speech end
  -> end-of-utterance decision
  -> finalized STT transcript
  -> durable user message
  -> direct run_executor.execute_run
  -> first safe text segment
  -> first TTS PCM bytes
  -> LiveKit playout
```

There is no shared ARQ admission step between an accepted voice turn and first audio. Database run claiming still prevents two executors from running the same conversation concurrently.

### Durable asynchronous path

```text
Boss tool call
  -> target-assigned Kanban task in Todo
  -> PostgreSQL notification
  -> scheduler creates target execution conversation
  -> ARQ execution
  -> task state / review / result visible to Boss
```

The voice actor acknowledges successful task creation but never waits synchronously for the delegated agent to finish.

### Concurrent room and process model

One private call normally contains one human participant and one voice-agent participant. At 100 simultaneous calls, the expected logical shape is:

| Resource | Quantity at 100 concurrent calls | Notes |
| --- | ---: | --- |
| LiveKit rooms | 100 | each room is isolated and opaque |
| LiveKit participants | approximately 200 | one human and one agent participant per room |
| persistent call actors | 100 | one job actor or subprocess for each admitted call |
| voice-worker pods | `ceil(100 / safe_sessions_per_pod)` plus spare capacity | determined by benchmark, not by agent count |
| active speaking agents | 100 | one per room even if a session has several conversation legs |
| ARQ jobs | variable | background/delegated work only |

The load balancer accepts the WebSocket and RTC connection. In a multi-node self-hosted deployment, LiveKit and its Redis coordination place and locate each room on a media node. A room stays on its assigned media node for its lifetime; the generic agent dispatch layer independently assigns the room job to an available `flexus-voice-agent` worker.

Boss, Sidra, and every other Flexus agent use the same worker image. The actor loads the authorized agent snapshot, conversation, tool policy, and voice profile from the durable `vsession_id`; it does not require an agent-specific process or pod. Boss-to-Sidra handoff changes the active leg inside the existing actor and therefore does not double media or worker capacity.

### Admission, pod sizing, and autoscaling

Capacity is reserved before the client enters a room. The create-session transaction and a short-lived Redis capacity lease must ensure that two concurrent admissions cannot claim the same final slot. A worker advertises a configured maximum and its current active count; the control plane dispatches only after a slot is reserved.

Define:

```text
raw_sessions_per_pod = highest measured concurrency that passes latency/error gates
safe_sessions_per_pod = floor(raw_sessions_per_pod * target_utilization)
required_pods = ceil(peak_concurrent_calls / safe_sessions_per_pod) + HA_spare
```

Start with `target_utilization` between 0.60 and 0.75 and choose the final value from burst and soak tests. For illustration only, if a replica passes at 12 calls, a 0.70 safety factor yields 8 admitted slots; 100 calls then require 13 active replicas plus warm high-availability/burst capacity. This example is not a production sizing claim.

Autoscaling must use a custom capacity signal in addition to CPU and memory:

```text
available_voice_slots = sum(max_sessions - active_sessions across ready workers)
```

Scale up when available slots fall below the configured burst reserve, pending dispatch appears, or admitted-call latency degrades. Maintain a warm minimum because a pod that starts only after the last slot is consumed cannot prevent dead air for the next caller. CPU alone is insufficient: actors may be waiting on STT, TTS, LLM, or tools while every session slot is already occupied.

When capacity is exhausted, `voice_session_create` returns a retryable busy result before minting a usable room token. It must not admit a call and queue every turn. On scale-down or rollout, a worker stops advertising slots, drains existing actors, and exits only after its calls finish or their bounded reconnect leases expire. Active calls are not migrated between worker pods in version 1.

## Component responsibilities

| Component | Responsibility | Must not do |
| --- | --- | --- |
| Voice client | capture/play audio, apply supported AEC/NS/AGC, join room, render active agent and confirmation UI | hold LiveKit or OpenRouter secrets |
| Flexus voice API | authenticate user, authorize agent/group, create session, mint token, initiate explicit dispatch | process realtime audio |
| `livekit-server` | signaling, SFU media routing, ICE, NAT traversal, TURN, room/participant state | execute Flexus tools or store conversation truth |
| `flexus-voice-agent` | own admitted call, VAD/STT/turn loop, direct run, TTS, interruptions, handoffs | become a second source of durable agent truth |
| Flexus run executor | agent snapshot, tools, permissions, LangGraph streaming, persistence, interrupts | know about audio codecs or WebRTC |
| OpenRouter adapters | translate stable Flexus speech interfaces into current OpenRouter requests | expose provider response shapes to product code |
| Scheduler and ARQ | delegated tasks and background/recoverable work | block the realtime first-audio path for an admitted call |
| PostgreSQL | durable voice session, leg, conversation, message, audit, and task records | store raw audio by default |
| Redis | checkpointer, events, session lease, cross-process cancellation and ephemeral coordination | be the sole source of session or conversation truth |

## Voice session lifecycle

### Session creation

1. Client calls `voice_session_create` with the requested initial agent, normally Boss.
2. Backend authenticates the human, authorizes the agent’s group, verifies the agent is enabled and not archived, and applies workspace voice quotas.
3. Backend creates a human-owned Flexus conversation or uses an explicitly selected existing conversation.
4. Backend creates a `flexus_voice_session` row and first leg in one transaction.
5. Backend creates an opaque LiveKit room name and short-lived token scoped to that room.
6. Backend explicitly dispatches the generic `flexus-voice-agent` worker with only the voice session ID in signed job metadata.
7. Worker fetches authoritative session, human, group, agent, conversation, and voice-profile data from Flexus. It does not trust display names or permissions in LiveKit metadata.
8. Worker accepts the call only after it has reserved session capacity and initialized its PostgreSQL, Redis, provider, registry, and checkpointer dependencies.
9. Client joins and receives an `active_agent` data event.

### Runtime states

```text
CREATING
  -> CONNECTING
  -> LISTENING
  -> TRANSCRIBING
  -> THINKING
  -> TOOL_ACTIVE
  -> SPEAKING
  -> LISTENING

Any active state may enter:
  HANDOFF_PENDING
  WAITING_FOR_CONFIRMATION
  INTERRUPTION_CANDIDATE
  INTERRUPTING
  RESUMING_SPEECH
  DEGRADED
  RECONNECTING
  CLOSING
  CLOSED
```

State transitions are serialized by the per-session actor. One finalized utterance receives one stable `voice_utterance_id`, and that ID is carried through transcript, Flexus message provenance, run metrics, and TTS segments.

### Audio capture and preprocessing

The client microphone remains published while the agent is speaking. Muting capture during playback would prevent natural barge-in. Audio cleanup happens primarily at capture because acoustic echo cancellation needs a synchronized reference of the agent audio playing through the local speaker.

Initial client processing order:

```text
microphone frames
  -> WebRTC acoustic echo cancellation using speaker playout as far-end reference
  -> WebRTC noise suppression
  -> automatic gain control
  -> optional platform high-pass filter
  -> Opus encode
  -> LiveKit uplink
```

Requirements:

1. Enable `echoCancellation`, `noiseSuppression`, and `autoGainControl` through the LiveKit client capture options when supported.
2. Inspect effective browser/device constraints for diagnostics because unsupported constraints may be ignored rather than fail the call.
3. Keep AEC on for speaker use; headphones naturally reduce echo but are not assumed.
4. Do not stack multiple aggressive neural noise cancellers. A second filter can deform speech, remove quiet consonants, and reduce STT accuracy.
5. Server-side processing may add a light, locally hosted high-pass/noise gate only after measured benefit. It must not replace capture-side AEC unless it also receives a correctly synchronized far-end reference signal.
6. Preserve a clean bypass path for debugging and A/B tests. Record processing configuration and quality metrics, never raw audio by default.
7. Test built-in processing separately on every browser/device included in a pilot.

VA-301 defines the initial pilot as desktop Chrome only. Safari and Firefox are deferred to a later
compatibility expansion, and iOS/Android remain outside the pilot until their own physical-device
rows pass; viewport emulation is not audio evidence. The client reports only a sanitized browser
family/major, platform family, secure-context state, AEC/noise-suppression/AGC settings, channel
count, and sample rate. It never reports raw user-agent strings or device identifiers.

Effective settings are necessary but not sufficient for speaker-mode support. The local physical
probe compares AEC-off and AEC-on trials with noise suppression and automatic gain disabled,
requires the requested settings to be observed, and uses a provisional 12 dB attenuation gate only
when the speaker reference is at least 6 dB above background. Missing constraints or weak physical
coupling are Unverified, not a pass. See the browser settings contracts for
[AEC](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackSettings/echoCancellation),
[noise suppression](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackSettings/noiseSuppression),
and [automatic gain](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackSettings/autoGainControl).

The self-hosted baseline relies on WebRTC client processing, which LiveKit supports for any deployment. LiveKit Cloud enhanced cancellation is not used. A future local enhancement may use a self-hosted model or an approved on-device processor behind the same capture contract. See [LiveKit noise and echo cancellation](https://docs.livekit.io/home/cloud/noise-cancellation/) and [self-hosting capabilities](https://docs.livekit.io/transport/self-hosting/).

### Normal turn

1. VAD observes speech and suppresses agent playback according to the interruption policy.
2. The actor buffers audio only for the active utterance.
3. End-of-utterance commits a WAV/PCM utterance to STT.
4. STT returns final text. Empty or low-confidence results do not create a user message.
5. The actor checks `(vsession_id, voice_utterance_id)` idempotency before appending.
6. The final transcript is stored as a normal human-authored message with voice provenance.
7. The actor invokes the same `run_executor.execute_run` used by ARQ, using its warm registry/checkpointer/pools.
8. Text deltas remain visible through existing Flexus live events and are also fed to a speech segmenter.
9. The segmenter emits a clause when punctuation is reached or configurable size/time bounds are exceeded.
10. TTS returns PCM bytes; the actor publishes frames to LiveKit and records first-byte/playout timing.
11. Completed assistant messages are persisted by the existing executor. The voice layer does not independently construct a second assistant message.

### Stream model and bounded buffers

“Streaming” is several independent streams joined by bounded queues:

```text
WebRTC Opus frames
  -> decoded PCM frames
  -> VAD / utterance buffer
  -> finalized STT transcript
  -> Flexus LangGraph message/update/custom events
  -> safe text phrase segments
  -> TTS response PCM bytes
  -> fixed-duration LiveKit audio frames
  -> client playout
```

Version 1 behavior by boundary:

| Boundary | Version 1 behavior | Later improvement |
| --- | --- | --- |
| client -> LiveKit | continuous WebRTC media track | codec/device tuning |
| LiveKit -> voice actor | continuous decoded audio frames | optional local enhancement |
| voice actor -> OpenRouter STT | one bounded completed utterance after VAD | partial streaming local STT |
| Flexus executor -> actor | terminal persisted message state after a normal unqueued finish | safe incremental post-tool events after their protocol is proven |
| text -> OpenRouter TTS | stable clause/sentence requests | provider-native bidirectional stream if useful |
| TTS -> LiveKit | incrementally consumed PCM, reframed for RTC | adaptive jitter tuning |

Every item carries `vsession_id`, `vleg_seq`, `voice_utterance_id`, `turn_id`, `speech_id`, and a monotonically increasing segment/frame sequence where applicable. Consumers reject stale items after interruption, handoff, reconnect, or retry.

The speech segmenter does not submit every LLM token to TTS. It emits only safe user-facing assistant text after the tool loop has selected a speakable response, using punctuation plus minimum/maximum size bounds. It removes Markdown-only syntax, tool arguments, URLs, code blocks, and hidden reasoning. VA-206 serializes provider streams and keeps one continuous frame sequence.

VA-302 makes the speech path bounds executable. Each turn admits four text segments for at most
30 seconds, one TTS request for one second, four PCM frames for 200 ms, and four RTC frames for
200 ms. Provider responses also reject one raw chunk above one second and total PCM above the
60-second stream deadline. Queues preserve FIFO sequence, bind items to the current turn and
speech, reject stale owners, expire old items, and flush immediately when cancellation invalidates
the speech. A 200 ms LiveKit capture deadline clears and fences an unhealthy sink instead of
letting an old blocked capture leak into replacement speech. Terminal turns emit content-free
depth, age, overflow, expiry, stale, flush, and health pressure metrics. The actor stops requesting
later TTS segments when an earlier boundary fails rather than accumulating minutes of audio.
PostgreSQL connections are borrowed per operation and are never held while waiting for user
speech, provider bytes, or external tools.

The first-audio latency budget is the sum of end-of-utterance detection, final STT, first safe Flexus phrase, TTS first-byte delay, and the small playout buffer. LangGraph generation, later TTS requests, and playback overlap after the first safe phrase.

### Barge-in and false interruption

Barge-in uses a two-phase local policy because the deployment cannot depend on LiveKit Cloud adaptive interruption inference. Initial production mode is VAD-based interruption with configurable duration, transcript, and false-interruption rules. A future self-hosted acoustic classifier may distinguish genuine interruptions from backchannels such as “uh-huh” without changing the state machine. See [LiveKit turn handling](https://docs.livekit.io/agents/logic/turns/) and [turn-handling options](https://docs.livekit.io/reference/agents/turn-handling-options/).

#### Phase 1: tentative pause

1. The microphone and VAD remain active while agent PCM is playing.
2. Capture-side AEC removes the agent’s speaker echo before VAD sees the signal.
3. On probable human speech onset, the actor enters `INTERRUPTION_CANDIDATE` and immediately pauses or strongly ducks agent playout.
4. It stops publishing new RTC audio frames but temporarily preserves a bounded resumable tail and the current speech handle.
5. It continues collecting user audio while evaluating speech duration and, when available, STT evidence.

This tentative pause is intentionally faster than final interruption classification. Prototype thresholds must sweep short values rather than copy a single default: measure approximately 150, 250, 350, and 500 ms minimum speech durations against real devices, echo, keyboard noise, coughs, and backchannels.

#### Phase 2A: committed interruption

If speech continues beyond the configured threshold or produces qualifying transcript content:

1. Enter `INTERRUPTING` and mark the previous `speech_id` obsolete.
2. Flush unplayed PCM and RTC frames and cancel queued or in-flight TTS segments.
3. Signal the existing Flexus stream-stop/cancellation mechanism and let `run_executor` recover at a safe boundary.
4. Continue capturing the new utterance; do not discard its pre-roll while cancellation completes.
5. Record the last confirmed playout timestamp and best synchronized text boundary. Generated text or audio beyond that point must not be treated as heard.
6. Wait until the old conversation run is safely idle or interrupted before appending the new finalized user message.
7. Start a new turn using a fresh `turn_id` and `speech_id` namespace.

#### Phase 2B: false interruption

If the candidate is a click, cough, echo leak, very short noise, or produces no qualifying transcript:

1. After a configurable bounded timeout, emit a false-interruption event.
2. Enter `RESUMING_SPEECH` and resume from the paused playout cursor when the buffered tail is still valid.
3. If exact resumption is not safe, continue from the next complete phrase rather than replaying already heard audio.
4. Return to `SPEAKING` without creating a user message or a new Flexus run.

Backchannels require product tuning. Version 1 may pause briefly for “yes,” “right,” or “uh-huh” and then resume when the phrase is classified as non-interrupting. It is better to make a short recoverable pause than to talk over a genuine correction. Metrics must distinguish tentative pauses, committed interruptions, false interruptions, and resumed speeches.

Interruption of speech is separate from cancellation of work:

- generated response text and pending TTS may be cancelled safely;
- a read-only cancellable foreground tool may receive cancellation;
- an irreversible or already committed tool continues and its true outcome is reported;
- a durable background task continues unless the user explicitly cancels that task;
- speech during a confirmation prompt stops the prompt but never implies approval or rejection.

Target acceptance: tentative playout pause begins within 250 ms p95 of real user speech onset under normal network conditions, confirmed interruptions never replay obsolete audio, and false interruptions recover without creating phantom user messages.

### Disconnect and reconnect

- A short client network loss keeps the voice actor and room lease alive for a bounded reconnect window.
- A reconnect token is minted only after reauthorizing the same human and session.
- The same active voice leg and conversation resume; no transcript is replayed as new input.
- When the window expires, the session closes and the current Flexus conversation remains available as ordinary chat history.
- Worker crash recovery may reconnect media, but it must not re-run an already committed utterance. Idempotency is based on durable utterance provenance, not process memory.
- The browser preserves the current room and microphone during transient SDK reconnect. After a
  terminal disconnect it refreshes and rejoins the same session; recovery never creates a new one.
- Only opaque recovery IDs are kept in session storage. An ambiguous refresh or end failure retains
  them for an idempotent retry, while a confirmed end removes them.

## Live handoff: Boss to Sidra

### Voice-only handoff tool

Expose a capability such as:

```text
voice_call_agent(
  target_agent_id,
  handoff_summary,
  expert_name=""
)
```

The capability is injected only when a run has a valid active voice-session context. A non-voice Boss conversation cannot pretend it transferred a call. The tool validates:

- target exists, is enabled, and is not archived;
- target is in the same workspace and allowed group hierarchy;
- initiating human may create/read the target conversation;
- requested expert exists on the target;
- no other handoff is pending;
- target voice profile has a valid fallback;
- handoff summary is bounded and safe to store.

### Handoff sequence

1. User says, “Boss, call Sidra about the sales report.”
2. Final transcript enters Boss’s conversation.
3. Boss calls `voice_call_agent` with Sidra’s internal ID and a concise scoped summary.
4. Coordinator authorizes Sidra and prepares the target leg while Boss says, “Connecting you to Sidra.”
5. First Sidra handoff in this voice session creates a new human-owned Sidra conversation. A later return to Sidra in the same session resumes that same conversation.
6. The target conversation receives a bounded internal context item containing the caller’s request, the safe handoff summary, source agent/conversation IDs, and provenance. Boss’s complete system prompt and full private history are not copied.
7. Coordinator waits until Boss’s handoff sentence has drained or is explicitly skipped.
8. LiveKit `AgentSession.update_agent` switches to a `FlexusVoiceAgent` adapter bound to Sidra’s agent and conversation. The WebRTC room and user audio track do not reconnect.
9. TTS switches to Sidra’s stable voice profile.
10. Sidra greets the caller and confirms the scoped subject.
11. All subsequent finalized utterances are persisted directly in Sidra’s conversation.

LiveKit’s handoff model and context behavior are documented in [Agents and handoffs](https://docs.livekit.io/agents/logic/agents-handoffs/). Context is fresh by default, which matches the Flexus decision to pass an explicit scoped summary instead of silently copying all history.

### Returning to Boss

“Boss, come back” is recognized by a global session-level intent before sending the utterance to the active specialist. It must work even if Sidra lacks any routing tool.

1. Coordinator marks the Sidra leg complete.
2. Sidra may provide a bounded return summary and outstanding actions.
3. The original Boss leg and conversation resume.
4. Voice switches back to Boss.
5. Boss receives the return summary as an internal context item and says that it is back.

The global command requires a high-confidence target match. Ambiguous phrases such as “tell Boss later” remain ordinary specialist input.

### Handoff failure

If authorization, conversation creation, agent availability, or voice initialization fails, Boss stays active and explains that the transfer did not happen. The system must never say “connected” before the target leg commits successfully.

## Asynchronous delegation

Delegation remains distinct from live handoff. The desired backend contract is:

```text
boss_delegate(
  target_agent_id,
  title,
  details,
  expert_name="",
  project_id="",
  reviewer_factor_id="",
  start_mode="todo",
  idempotency_key
)
```

The operation must atomically validate the target, create a target-assigned task, put an immediate delegation into Todo, record human/Boss/voice provenance, and return the durable task identity. PostgreSQL notification wakes the existing scheduler, which creates the target execution conversation and sends it through ARQ.

Current Flexus contains useful legacy `flexus_hand_over_task` and `flexus_agent_send` implementations, but those tools are retired and not in Boss’s active allowlist. Active `flexus_kanban(create)` assigns the task to the calling agent. A safe active Boss-specific delegation tool is therefore a backend prerequisite for the complete “ask Sidra” voice story; it must not be implemented as voice-only logic.

Voice behavior after a successful commit:

> “I delegated the report to Sidra and I’ll track it.”

Voice behavior after any failure:

> “I couldn’t create that task, so nothing was delegated.”

The user may continue speaking with Boss while Sidra works. A later “call Sidra about that task” live handoff can include the task ID and status in the scoped context.

## Speech provider plan

### Temporary OpenRouter integration

OpenRouter currently exposes dedicated OpenAI-compatible endpoints:

```text
POST https://openrouter.ai/api/v1/audio/transcriptions
POST https://openrouter.ai/api/v1/audio/speech
```

References:

- [OpenRouter STT guide](https://openrouter.ai/docs/guides/overview/multimodal/stt)
- [OpenRouter TTS guide](https://openrouter.ai/docs/guides/overview/multimodal/tts)
- [current transcription models](https://openrouter.ai/api/v1/models?output_modalities=transcription)
- [current speech models](https://openrouter.ai/api/v1/models?output_modalities=speech)

Initial benchmark matrix:

| Role | Default candidate | Challenger | Reason to test |
| --- | --- | --- | --- |
| STT | `openai/whisper-large-v3-turbo` | `openai/gpt-4o-mini-transcribe` | multilingual speed/cost versus contextual accuracy |
| English TTS | `hexgrad/kokoro-82m` | `deepgram/aura-2` | inexpensive many-voice prototype versus voice-agent catalog |
| multilingual TTS | `x-ai/grok-voice-tts-1.0` | `fish-audio/s1` | broader language behavior; must be measured with Flexus prompts |

The selected production candidate is determined by measured p50/p95 latency, intelligibility, language accuracy, interruption behavior, voice consistency, cost, and provider error rate. Model names are configuration, not constants embedded in the session code.

### STT contract

The documented OpenRouter transcription endpoint accepts a completed base64-encoded audio object and returns finalized text. Version 1 therefore uses utterance-level STT after local VAD. It must not claim word-by-word partial transcription when the adapter only has a final response.

Recommended input for the first benchmark:

- mono PCM captured from LiveKit;
- WAV wrapper for endpoint compatibility;
- sample rate normalized once, outside the provider adapter if required;
- explicit language only when the user selected one; otherwise provider auto-detection;
- bounded utterance length and byte size;
- optional prompt vocabulary for Flexus, Boss, Sidra, agent names, projects, and domain terms when the chosen provider supports it.

The stable interface supports partial events even though the first adapter yields only `final`, allowing a later local streaming engine to add partial transcripts without changing the session actor.

### TTS contract

Request `response_format="pcm"` and consume response bytes incrementally. Do not wait for a complete MP3 file before playout.

The speech segmenter should initially emit on:

- sentence-ending punctuation;
- clause punctuation after a minimum character threshold;
- a maximum character threshold;
- a maximum buffering delay after first LLM text.

Exact thresholds belong to the latency benchmark. Starting candidates are 40 minimum characters, 160 maximum characters, and 250 ms maximum buffer after the first speakable token. Tool-call JSON, Markdown syntax, URLs, code blocks, and hidden reasoning must never be spoken verbatim.

Each segment carries `voice_utterance_id`, `speech_id`, `segment_index`, selected model, selected voice, and cancellation state.

### Agent voice profiles

Persist one provider-independent profile identifier at each configurable scope:

```text
agent_voice_profile_id
blueprint_voice_profile_id
ws_voice_profile_id
vleg_voice_profile_id
```

The first three fields are nullable inheritance points. The leg field freezes the selected semantic
profile for that actor leg. A server-owned registry maps the semantic profile to the current
provider, model, provider voice ID, and speed. Provider migration therefore changes the registry,
not persisted agent identity. Builtin blueprints give Boss and Sidra stable defaults. Instance
overrides remain separate from snapshot fields and survive blueprint refresh.
The authenticated agent and workspace patch inputs accept a registered semantic key. An empty
string clears that scope's override back to SQL `NULL`, re-enabling inheritance; GraphQL `null`
means the patch field was omitted.

Fallback order:

1. agent instance override;
2. blueprint default;
3. workspace default;
4. global safe default.

Example prototype profiles:

```text
voice_boss:    openrouter / hexgrad/kokoro-82m / am_adam / 1.0
voice_sidra:   openrouter / hexgrad/kokoro-82m / af_heart / 1.0
voice_default: openrouter / hexgrad/kokoro-82m / am_adam / 1.0
```

Voice selection changes only after the previous agent’s final audio segment drains. The UI also changes name/avatar at the same committed boundary.

### Stable provider interfaces

```python
class STTProvider:
    async def stream(self, audio, config):
        yield TranscriptEvent(kind="final", text="...")


class TTSProvider:
    async def stream(self, text, voice, config):
        yield PCMFrame(...)
```

Required behavior:

- deadline and cancellation propagation;
- connection reuse where the provider supports it;
- provider request/generation ID capture;
- normalized errors rather than provider-specific exceptions in session logic;
- usage and cost metadata;
- no API keys in logs, database rows, LiveKit metadata, or client responses.

### Migration to local speech inference

Local STT/TTS must implement the same interfaces. The migration sequence is:

1. Deploy shared local inference services, not one pod per agent.
2. Run offline replay comparisons against consented benchmark audio.
3. Shadow local STT on sampled calls without affecting the transcript of record.
4. Compare word error rate, named-entity accuracy, latency, and resource cost.
5. Switch selected workspaces to local STT behind a feature flag.
6. Shadow and then switch TTS, comparing first-byte latency and human preference.
7. Remove external audio routing only after local capacity and failure recovery meet the same gates.

## Durable data

VA-201 implements the durable session, leg, and playout foundation in PostgreSQL. The active
actor lease may remain in Redis, but handoff and reconnect identity survives worker loss.

### `flexus_voice_session`

Implemented fields:

```text
owner_fuser_id
owner_shared=false
located_fgroup_id
vsession_id
vsession_room_name
vsession_create_request_id
vsession_requested_conversation_id
vsession_participant_identity
vsession_dispatch_id
vsession_dispatch_state
vsession_dispatch_attempted_ts
vsession_status
vsession_actor_epoch
vsession_active_leg_seq
vsession_client_kind
vsession_created_ts
vsession_connected_ts
vsession_last_seen_ts
vsession_reconnect_expires_ts
vsession_ended_ts
vsession_end_reason
```

The room name and participant identity are opaque and unique. The stored create-request ID is
an equality-preserving hash. The owner plus that key is unique, and user-row serialization makes
sequential and cross-workspace concurrent retries resolve to one durable intent. The nullable
requested-conversation field distinguishes “create a conversation” from “reuse this exact one.”
Dispatch state is `PENDING`, `UNCERTAIN`, `ACCEPTED`, or `CANCELLED`; it is separate from
the call lifecycle. Do not put user email, workspace name, agent name, or conversation title
in any LiveKit identifier.

Room-scoped dispatch reconciliation is at-least-once external dispatch with one accepted
matching record, not proof that a worker actor became live. VA-203 therefore acquires a
token-fenced Redis lease before loading the locked PostgreSQL session and active leg or
connecting media. The 15-second lease renews every 5 seconds and before lifecycle mutations.
Actor claim increments `vsession_actor_epoch`; subsequent worker transactions reject any older
epoch even if a paused process wakes after replacement. Only the stored participant
identity may move the session to `ACTIVE`; a disconnect creates one non-extending 20-second
reconnect deadline and a matching return resumes the same actor, leg, and conversation.

### `flexus_voice_session_leg`

Implemented fields:

```text
vsession_id
vleg_seq
vleg_agent_id
vleg_conversation_id
vleg_started_ts
vleg_ended_ts
vleg_end_reason
source_leg_seq
```

Handoff summaries belong in the target Flexus conversation with bounded content and provenance, not duplicated as free-form leg metadata.

### `flexus_voice_speech`

Use the implemented lightweight durable playout record when an assistant message is spoken:

```text
vsession_id
vleg_seq
speech_id
speech_conversation_id
speech_message_seq
tts_provider
tts_model
tts_voice_id
speech_status            # queued, playing, completed, interrupted, failed
speech_started_ts
speech_last_playout_ts
speech_ended_ts
speech_generated_audio_ms
speech_played_audio_ms
speech_played_text_end   # best-effort synchronized character/word boundary
speech_interruption_reason
```

This record does not duplicate assistant message content. It identifies what was selected for speech and how much was actually played, which is needed for interruption provenance, reconnect, metrics, and honest history. Fine-grained per-frame rows are not durable; frame and queue state remain inside the actor.

All relations that cross a session boundary are group-scoped. Session deletion cascades to
legs and speech rows, while agent, conversation, and assistant-message deletion is blocked
until the voice history is intentionally removed. Session and room IDs are opaque 32-hex
namespaces. Legs start at sequence one, and speech IDs use the actor-owned exact shape
`<vsession_id>.<vleg_seq>.<vturn_seq>.speech`.

`vsession_active_leg_seq` is not a foreign key back to the leg table because that would make
session/first-leg insertion cyclic. VA-202 creates the conversation, session, and first leg in
one transaction; VA-203 locks and validates the current leg during actor bootstrap and every
liveness transition, and fences those writes with the actor epoch. VA-503 will lock the session
and target leg before changing the active
sequence. VA-204 separately owns exactly-once user-transcript identity through
`(vsession_id, voice_utterance_id)`; it is intentionally not overloaded into the playout
table.

### Message provenance

Final user transcripts and handoff context should include:

```json
{
  "source": "voice",
  "vsession_id": "...",
  "vleg_seq": 2,
  "voice_utterance_id": "...",
  "livekit_room_sid": "...",
  "stt_provider": "openrouter",
  "stt_model": "openai/whisper-large-v3-turbo",
  "initiated_by_fuser_id": "..."
}
```

Do not store raw audio, access tokens, API keys, or unbounded provider responses in provenance.

Assistant messages selected for speech should link to `speech_id` through the playout record. An interrupted response must preserve the durable assistant content produced by the executor while separately recording the best known heard cutoff. Unplayed generated audio is never inserted as a second assistant message and is never presented as something the user heard.

## Control-plane API surface

Flexus uses Strawberry GraphQL, so the initial control plane should remain GraphQL while audio stays in LiveKit.

### Mutations

```text
voice_session_create(input)
  -> vsession_id, room_name, livekit_url, participant_token,
     initial_agent, conversation_id, expires_ts

voice_session_refresh_token(vsession_id)
  -> participant_token, expires_ts

voice_session_end(vsession_id)
  -> final status
```

`voice_session_create` is idempotent for a caller-supplied request ID and performs authorization
before any dispatch. It locks the workspace before enforcing active-session caps, persists the
reservation before the external call, and returns a token only after the dispatch is durably
`ACCEPTED`. A timeout marks the dispatch `UNCERTAIN`; a later replay first calls self-hosted
LiveKit `ListDispatch` for the exact opaque room and creates a dispatch only when none exists.
Refresh reauthorizes the human owner and mints a new token for the persisted room and participant
identity without storing the credential. End is terminal and idempotent, closes the active leg,
marks dispatch state cancelled, and writes lifecycle audits in the same transaction.

### Status delivery

VA-207 implements fast session state on the reliable private LiveKit topic
`flexus.voice.state.v1`. Its version-1 envelope contains only `vprotocol_version`, `vsession_id`,
`vleg_seq`, monotonic `vstate_seq`, declared `vstate`, `vagent_id`, and `vconversation_id`.
The worker targets the persisted human identity; the browser validates every identity field,
rejects the human as sender, ignores stale sequences, and caps packets at 4 KiB. Transcript text,
tool arguments/results, audio, tokens, and secrets are forbidden from this channel.

Listening, transcribing, thinking, speaking, and recoverable errors are ephemeral presentation.
Durable conversation messages, tool confirmations, task status, and history continue through
existing Flexus APIs and subscriptions. The existing chat transcript remains the sole message
surface; a newly committed visible voice transcript publishes the ordinary `message_insert`
event, while idempotent replays do not publish again.

### M2 durable vertical-slice evidence

VA-208 closes the first durable Boss milestone with a layered full-stack acceptance test. Against
a freshly migrated PostgreSQL database, one authorized session binds to an existing conversation,
connects its claimed actor, persists five distinct finalized utterances, and obtains five
direct-run-eligible results. The test also exercises five independent successful claims through the
production conversation run gate. Replaying the first utterance is an idempotent no-op for both
message persistence and direct-run eligibility. Token refresh preserves the participant identity,
reconnect preserves the same leg and conversation, session end is terminal, and a normal text
message is appended and read back through ordinary GraphQL afterward.

The same durable rows are read through the ordinary `conversation_messages_list` GraphQL field,
not a voice-specific transcript API. Frontend acceptance separately remounts the call UI with only
the opaque recovery identity in session storage, verifies refresh and rejoin without a second
create, and renders a voice-provenance row in the normal chat timeline. VA-205 executor-service
tests prove that only a newly inserted direct candidate invokes the existing executor, while the
concurrent claim tests prove competing entry paths cannot both own the run gate. The evidence is
layered by design; VA-208 does not mislabel its manual gate claims as executor invocations and does
not introduce a duplicate voice executor or a test-only run ledger.

## Tool execution in voice sessions

LiveKit does not execute tools and the voice layer must not define parallel voice-only copies of business tools. A finalized transcript becomes a normal human message in the active conversation, and the persistent actor invokes the existing Flexus executor directly:

VA-204 implements the first half of that boundary: it commits the message exactly once and
returns its conversation/message reference. VA-205 consumes only a newly inserted visible
message directly; a replay or a message queued behind an active run cannot create a second
executor. The warm call holds the normal registry, checkpointer, predefined tool catalog, and ARQ
continuation connection. Initial voice turns never enter ARQ, while promoted follow-ups and crash
recovery retain the existing durable queue path.

```text
LiveKit audio
  -> VAD and OpenRouter STT
  -> durable message in active conversation
  -> run_executor.execute_run(conversation_id)
  -> existing agent snapshot, tool policy, RequestContext, and tool registry
  -> LangGraph tool/result events
  -> safe final assistant text
  -> TTS and LiveKit playout
```

This is the same executor implementation that ARQ calls, but the admitted live turn does not enter
the shared ARQ queue. Its PostgreSQL pool is task-local, fresh `RUNNING` claims serialize direct
voice against ARQ, and cancelled recovery is drained before call resources close. VA-206 consumes
only a terminal, persisted, unqueued completion and selects the last safe post-human assistant
response. Raw deltas and structured tool activity stay out of speech; VA-401 owns their safe UI and
spoken acknowledgement contract.

### Tool execution classes

| Class | Examples | Voice behavior | Execution path |
| --- | --- | --- | --- |
| no tool | explanation, ordinary discussion | stream safe response phrases | direct realtime run |
| short read-only foreground tool | calendar lookup, task status, small search | show/speak concise activity, wait, then summarize | direct realtime run |
| consequential foreground tool | send email, modify record, delete file | enter existing confirmation interrupt before action | direct realtime run plus confirmation resume |
| slow but bounded foreground tool | query expected to complete in a few seconds | show one activity state; remain interruptible | direct realtime run with deadline/cancellation policy |
| long/retryable work | large report, crawl, bulk import | create durable operation and acknowledge immediately | scheduler/ARQ/background service |
| Boss delegation | ask Sidra to prepare work | commit target task, return task ID, keep Boss active | scheduler and ARQ target run |
| live handoff | call Sidra | commit/switch session leg after authorization | voice coordinator, not a business integration |

The exact foreground/background boundary is tool-specific, not a single universal timeout. Tools that may take minutes, require retries, or must survive the call should return a durable operation/task ID quickly. A foreground tool that unexpectedly exceeds its deadline should fail clearly or convert through an explicit background contract; the voice actor must not secretly enqueue an arbitrary half-executed call.

### Spoken tool activity and results

The speech planner does not read raw tool arguments, JSON results, URLs, code, secrets, or provider errors. When a real tool-call event is observed, the client may show a structured activity such as “Checking calendar.” Optional spoken acknowledgement must come from safe structured metadata and should be used once, not repeated as filler.

VA-401 implements the first safe projection as aggregate state only. The executor tracks opaque
call IDs internally to deduplicate and keep parallel calls active until the last result or error,
but its voice callback emits only `true` or `false`. A server-owned fail-closed policy admits known
Boss inline reads and bounded presentation/wait tools while excluding unknown, background,
confirmation-gated, and mutating operations. Presentation callback failure cannot change the
durable run. The owned call maps the aggregate to `TOOL_ACTIVE` and
back to `THINKING`; the browser shows the fixed localized label “Using a tool.” No tool-derived
string crosses the callback or LiveKit state protocol. Version 1 deliberately emits zero spoken
activity acknowledgements: this satisfies the at-most-one limit and avoids starting an unordered
second TTS lane while the executor is still running. Normal final assistant speech remains the
only spoken tool result.

Text emitted before or alongside an unresolved tool call is not automatically speakable. The actor waits until the tool loop produces an assistant response that the executor regards as the user-facing result. Detailed result cards and links stay in the Flexus UI; voice gives a concise summary and offers to expand.

### Tool concurrency and isolation

One hundred active rooms may produce bursts of one hundred tool calls. Voice-worker scaling cannot solve a third-party quota, so each integration needs its own bulkhead:

- per-provider and per-tool concurrency semaphore;
- per-user and per-workspace rate/spend limits;
- deadline, bounded retry, and circuit-breaker policy;
- idempotency key for side-effecting operations and delegation;
- cancellation capability metadata;
- no PostgreSQL connection held during external network waits;
- blocking libraries moved to an executor process/thread or dedicated service so they cannot stall audio handling;
- metrics for queue wait, execution time, timeout, provider 429/5xx, cancellation, and committed outcome.

The realtime actor remains reserved while an inline tool awaits I/O, but its audio loop and cancellation path must remain responsive. Long work moves out of the actor and frees the conversational turn even though the call actor itself remains admitted.

### Tool interruption semantics

Barge-in always stops or pauses audible speech first. It does not infer that the user intended to cancel the underlying operation.

1. If generation or TTS is still pending, mark it obsolete and cancel it.
2. If a read-only foreground tool declares itself cancellable, propagate the turn cancellation and discard a late result using `turn_id`.
3. If a side effect may already have committed, do not retry or claim cancellation without checking idempotent operation status.
4. If a background task exists, keep it running until an explicit task-cancel command succeeds.
5. If a live handoff is pending, interruption aborts the handoff before the active leg changes; a committed leg switch is reported truthfully.
6. If confirmation is pending, speech is interpreted only through the confirmation state machine described below.

## Tool confirmations and powerful-agent safety

Boss can invoke consequential tools. Voice must preserve the existing confirmation and permission model.

1. When the run enters a tool confirmation interrupt, the voice actor enters `WAITING_FOR_CONFIRMATION`.
2. The ordinary chat surface renders the existing structured confirmation card; the compact call card remains generic and never repeats tool-derived data.
3. Voice version 1 speaks the fixed server-owned summary “This action needs your confirmation. Please say approve or reject.”
4. Exact multilingual approve/reject phrases are interpreted only while one specific durable confirmation is pending. Corrections and mixed phrases are ambiguous in version 1; reject first, then restate the corrected request.
5. A spoken response retrieves the signed token from the stored interrupt and resumes the existing interrupt/checkpoint directly; the tool is not restarted from scratch.
6. A web-card response uses the existing authorized mutation and ARQ continuation. The call actor observes its actor-fenced durable phase and never starts a duplicate resume.
7. Ambiguous input asks again. Silence never approves.
8. Barge-in during the spoken confirmation stops speech but does not approve or reject.
9. Live handoff cannot bypass pending confirmation. The current agent must resolve or cancel it first.

The executor produces its voice notice only after the interrupt is durably committed. The notice
contains only the interrupt ID and expiry; the signed token, tool name, command, arguments, and
model explanation remain in the ordinary durable confirmation payload. Voice runs disable standing
automatic approval. Resolution locks and reauthorizes the accepted session, actor epoch, active
leg, conversation, agent, and owner; refuses to replace another interrupt's pending resume; removes
only the exact interrupt; writes the native pending resume; and audits without sensitive
confirmation fields. Reconnect preserves the waiting state. Actor-fence or phase-probe failure
closes the local call rather than leaving an unresolvable prompt.

## Latency and capacity gates

These are provisional version-1 targets to validate with real local LiveKit and OpenRouter traffic:

| Metric | Initial p95 gate | Long-term local goal |
| --- | ---: | ---: |
| user speech end -> final STT | <= 1,000 ms | <= 500 ms |
| final STT -> first Flexus text delta | <= 1,000 ms | <= 600 ms |
| first speakable text -> first TTS PCM byte | <= 600 ms | <= 300 ms |
| speech end -> audible response | <= 2,000 ms | <= 1,200 ms |
| user speech onset -> tentative old-audio pause | <= 250 ms | <= 150 ms |
| confirmed interruption -> stale PCM queue empty | <= 100 ms | <= 50 ms |
| false interruption -> resumed speech | <= 1,000 ms after classification | <= 500 ms after classification |
| Boss handoff audio end -> Sidra greeting starts | <= 1,500 ms | <= 750 ms |
| admitted-call p95 degradation at target concurrency | < 25% versus warm single-call baseline | < 15% |

Every benchmark must report p50, p95, p99, errors, cancellations, provider/model, audio duration, concurrency, cold/warm state, and whether TURN was used. Averages alone are insufficient.

Capacity is admitted at call start. When no realtime slot is available, session creation returns a clear busy/retry response before the user enters a room. It must not accept a call and then queue every turn behind other sessions.

Capacity reports must state rooms, participants, persistent actors, voice-worker replicas, `raw_sessions_per_pod`, `safe_sessions_per_pod`, warm spare slots, LiveKit media nodes, database pool pressure, and provider concurrency limits. The target-concurrency run includes 100 private rooms even if the first pilot cap is lower. Passing 100 idle rooms is not sufficient; the load must include simultaneous end-of-utterance bursts, TTS playback, interruption, and tool activity.

## Deployment plan

### Local development

Extend [compose.yml](../../compose.yml) with development-only services or a companion compose file:

- `livekit-server` with a pinned image version and development key/secret;
- LiveKit configuration with local WebSocket/RTC ports;
- existing Redis with an isolated database/prefix for the prototype, or a dedicated LiveKit Redis service if collisions/load make isolation necessary;
- `flexus-voice-agent` built from the Flexus backend environment;
- existing PostgreSQL, backend, Redis, Jaeger, and frontend.

The first browser test can use `ws://localhost:7880`. Production uses trusted TLS and `wss://`.

### Production services

| Service | Initial shape | Scaling rule |
| --- | --- | --- |
| LiveKit server | dedicated node/VM or one host-networked pod per node | CPU, bandwidth, room load; drain active rooms on rollout |
| LiveKit Redis | dedicated production Redis or isolated managed cluster | availability and room/signaling load |
| `flexus-voice-agent` | minimum two replicas | available job capacity, active sessions, CPU/memory |
| Flexus backend | existing deployment plus voice control API | ordinary API load |
| Flexus PostgreSQL/Redis | existing durable runtime dependencies | current DB/checkpointer/event load plus voice turns |
| ARQ/scheduler | existing deployment | delegated/background work only |
| Prometheus/OpenTelemetry/Jaeger | self-hosted observability | metric/trace volume |

There is no pod per Boss, Sidra, or other Flexus agent. A generic LiveKit agent server accepts room jobs and starts one job subprocess/actor per admitted call. LiveKit documents this worker-pool model in [self-hosted agent deployments](https://docs.livekit.io/deploy/custom/deployments/).

There is also no pod per LiveKit room. Media rooms are in-memory SFU state distributed across LiveKit nodes, while the corresponding call actors are distributed independently across the voice-worker pool. For a 100-call target, provision media-node high availability separately from the measured number of voice replicas; one layer must not use the other layer’s replica count as its sizing rule.

For Kubernetes, LiveKit requires direct RTC networking and commonly host networking, which limits the deployment to one LiveKit pod per node. See [LiveKit Kubernetes deployment](https://docs.livekit.io/transport/self-hosting/kubernetes/). A small initial production deployment may be operationally simpler on a dedicated VM before multi-node Kubernetes is justified.

Production networking must account for TLS, TURN, and RTC ports. LiveKit’s current deployment guidance and port reference are:

- [self-hosted deployment](https://docs.livekit.io/transport/self-hosting/deployment/)
- [ports and firewall](https://docs.livekit.io/transport/self-hosting/ports-firewall/)

### Worker lifecycle

- Prewarm provider HTTP clients, VAD, registry, and checkpointer dependencies before accepting jobs.
- Prewarm the configured safe number of job subprocesses or initialization slots; do not admit a call before its actor dependencies are ready.
- Mark a pod unavailable before termination and allow active calls to drain.
- Enforce a maximum session duration and idle timeout.
- Keep an explicit per-worker session capacity; do not rely only on CPU saturation.
- Publish `max_sessions`, `active_sessions`, and `available_sessions` as autoscaling metrics.
- Keep STT, TTS, LLM, and integration connection pools bounded and do not multiply their limits blindly by pod count.
- Do not autoscale down a worker with active calls unless the session can migrate safely.
- Use separate staging and production LiveKit deployments and credentials.

## Observability

Entirely self-hosted LiveKit does not provide LiveKit Cloud Insights. Consume SDK data hooks and export metrics/traces to Flexus infrastructure. LiveKit documents local collection in [Data hooks](https://docs.livekit.io/deploy/observability/data/).

### Correlation identifiers

Every log, metric, and trace should carry the relevant subset of:

```text
vsession_id
vleg_seq
voice_utterance_id
turn_id
speech_id
segment_index
conversation_id
agent_id
workspace_id
livekit_room_sid
provider_generation_id
```

Never use names, transcript text, or raw audio as metric labels.

### Required metrics

- active/admitted/rejected voice sessions;
- room connect and reconnect time;
- effective client AEC/noise-suppression/AGC settings and platform, without device-identifying labels;
- input clipping, silence, VAD speech probability, utterance length, and rejected-noise counts;
- end-of-utterance and transcription delay;
- STT request duration, audio duration, error rate, model, and cost;
- Flexus run claim time, first text delta, tool queue wait, tool execution time, interrupt wait, and completion;
- TTS time to first byte, total duration, generated audio duration, cancellation rate, model, and voice;
- text-segment, TTS, PCM, and RTC queue depth plus oldest-item age;
- generation-to-playout lag and played-versus-generated audio duration;
- speech-end to first-audio end-to-end latency;
- barge-in candidate detection, tentative playout-pause, committed-interruption, stale-queue-flush, and false-interruption-resume latency;
- counts for tentative, committed, false, resumed, and backchannel-classified interruptions;
- handoff preparation and audible-gap latency;
- provider 429/5xx/timeout counts;
- worker max/active/available capacity, pending dispatch, and session actor memory/CPU/file descriptors;
- rooms and participants per LiveKit node, RTC bandwidth, packet loss, jitter, and TURN usage;
- integration bulkhead utilization, provider rate-limit wait, and circuit-breaker state;
- delegated task creation and scheduler pickup latency.

LiveKit’s `EOUMetrics`, `STTMetrics`, and `TTSMetrics` already expose relevant component timings such as transcription delay and TTS TTFB; see [metrics reference](https://docs.livekit.io/reference/python/livekit/agents/metrics/).

### Logs and retention

- Log state transitions and IDs, not transcript bodies, by default.
- Store transcripts only through normal Flexus message persistence and retention rules.
- Do not record room audio by default.
- A debug recording mode requires an explicit feature flag, user consent, bounded retention, access control, and audit.

## Security and privacy

1. LiveKit API secret and OpenRouter key remain server-side.
2. Client tokens are short-lived, room-scoped, and minimally permissioned.
3. Room names and participant identities are opaque identifiers.
4. Worker fetches authoritative authorization from Flexus and rechecks it on every handoff.
5. Initiating human identity is preserved in `RequestContext`, message provenance, tool confirmation, delegation, and audit.
6. Cross-workspace agent handoff and delegation fail closed.
7. Audio sent to OpenRouter is the minimum completed utterance required for STT; TTS receives only text selected for speech.
8. Provider routing, data retention, and zero-data-retention requirements must be configured and reviewed before external beta.
9. No raw provider response or secret is persisted in JSONB.
10. Voice session creation, handoff, return, confirmation, delegation, and termination produce searchable audit events.
11. Rate limits cover concurrent sessions per workspace/user, session duration, utterance duration, provider spend, and repeated failed handoffs.

The privacy statement must be explicit: media transport is local, but speech content temporarily leaves Flexus infrastructure for OpenRouter STT/TTS until local inference replaces it.

## Failure behavior

| Failure | Required behavior |
| --- | --- |
| no voice capacity | reject before room admission with retryable busy result |
| LiveKit room connection failure | preserve conversation; allow bounded token refresh/reconnect |
| client AEC/NS/AGC unsupported | continue with explicit degraded-audio telemetry; recommend headphones; never claim filtering is active |
| persistent echo or noisy input | increase interruption evidence threshold or ask user to change audio setup; do not create repeated phantom turns |
| empty or low-confidence STT | ask user to repeat; do not append an invented message |
| STT timeout/5xx | one bounded retry only when it fits latency budget; otherwise ask to repeat |
| Flexus run already active | serialize within actor or present busy state; never start a competing run |
| LLM/foreground tool takes long | show one thinking/activity state; cancel by policy or explicitly convert to durable work; do not play misleading filler repeatedly |
| integration concurrency/rate limit | wait only within foreground deadline; otherwise return a clear retry/background option |
| side-effecting tool interrupted | verify idempotent operation status and report truth; never assume rollback |
| TTS first-byte timeout | cancel segment, show text, use configured fallback voice/provider if policy permits |
| TTS/PCM queue stalls | bound the queue, stop requesting later segments, and fail or degrade to text before memory grows unbounded |
| confirmed user interruption | stop playout, obsolete generation, and preserve committed history plus heard cutoff safely |
| false interruption | resume from a safe playout cursor without creating a user message or replaying heard audio |
| target agent missing/disabled | remain with Boss and say transfer failed |
| handoff conversation creation fails | no active-agent change and no “connected” acknowledgement |
| voice worker crashes | reconnect within lease if possible; deduplicate committed utterances |
| Redis event loss | durable PostgreSQL messages remain truth; recover from conversation state |
| OpenRouter unavailable | degrade to text chat or explicit voice-unavailable state; do not route media to an unapproved provider silently |
| call disconnects during delegation | committed task remains valid and visible; uncommitted tool call does not exist |

## Implementation phases

### Phase 0: real provider microbenchmarks

Deliverables:

- OpenRouter STT adapter spike against the candidate matrix;
- OpenRouter streaming PCM TTS adapter spike with at least two distinct voices;
- cancellation test that closes TTS stream promptly;
- benchmark corpus covering short/long utterances, silence, echo leakage, fan/keyboard/cough noise, backchannels, genuine barge-in, English, Russian if required, agent names, and domain vocabulary;
- JSON results containing latency, errors, usage, and cost.

Gate:

- choose provisional STT/TTS defaults;
- prove PCM can be consumed incrementally;
- document whether Russian and mixed-language behavior is acceptable;
- no production architecture work until real provider latency is known.

### Phase 1: local LiveKit media prototype

Deliverables:

- pinned local `livekit-server` configuration;
- browser join/token flow;
- generic self-hosted `flexus-voice-agent` worker;
- microphone -> VAD -> OpenRouter STT -> echo text -> OpenRouter TTS -> room loop;
- WebRTC AEC/noise-suppression/AGC capture configuration and effective-setting diagnostics;
- two-phase VAD barge-in with tentative pause, committed cancellation, false-interruption resume, and stale-frame rejection;
- bounded text/TTS/PCM/RTC queues with per-turn and per-speech sequence IDs;
- local metrics for EOU, STT, TTS, playout, interruption classification, cancellation, queue pressure, and reconnect;
- one-call and concurrent-call load script capable of driving 100 isolated rooms.

Gate:

- zero LiveKit Cloud endpoints;
- first-audio and barge-in targets are measurable;
- speaker echo does not repeatedly interrupt the agent on supported clients;
- false interruption resumes without creating a durable user message;
- overload rejects at session admission instead of queuing turns indefinitely.

### Phase 2: Boss conversation integration

Deliverables:

- `voice_session_create`, refresh, and end API;
- durable voice session and first-leg records;
- persistent actor calls the existing Flexus run executor directly;
- final transcript stored once as a human message;
- existing text deltas drive speech segmentation;
- completed response remains identical in durable chat and voice transcript;
- foreground tool activity, background-operation acknowledgement, and interrupt states reflected in call UI;
- direct tool execution preserves active agent policy and uses integration bulkheads;
- interrupted speech records a playout cutoff without duplicating assistant content;
- conversation remains usable after call end.

Gate:

- no parallel voice-only implementation of tools or memory;
- one utterance creates one durable user message and at most one run;
- speech interruption does not imply tool cancellation or confirmation;
- warm and admitted-concurrency latency meets the initial gate.

### Phase 3: agent voices and live handoff

Deliverables:

- blueprint/instance voice profiles and fallback resolution;
- Boss and Sidra use recognizably different voices;
- voice-only `voice_call_agent` capability;
- durable session legs;
- scoped handoff and return summaries with provenance;
- same-room `AgentSession.update_agent` transition;
- global “Boss, come back” command;
- handoff UI state and active-agent identity.

Gate:

- no WebRTC reconnect during handoff;
- no full Boss context copied automatically;
- failed handoff leaves Boss active;
- repeated Boss <-> Sidra switching in one call does not duplicate conversations or messages.

### Phase 4: durable Boss delegation

Deliverables:

- active safe `boss_delegate` contract outside the voice layer;
- target-assigned Todo task, provenance, idempotency, and scheduler wake-up;
- spoken acknowledgement only after commit;
- Boss board/status integration;
- “call Sidra about task X” context link.

Gate:

- delegated work executes through scheduler/ARQ, never in the voice actor;
- duplicate/retried voice tool calls create exactly one task;
- target, group, expert, reviewer, and permission tests pass.

### Phase 5: production hardening

Deliverables:

- confirmation speech/UI bridge;
- reconnect and crash recovery;
- room/session admission leases, custom available-slot autoscaling, warm spare capacity, and graceful drain;
- quotas, metering, provider-spend limits, integration bulkheads, and rate limiting;
- Prometheus/OpenTelemetry dashboards and alerts;
- provider fallback policy;
- security/privacy review;
- chaos tests for Redis, PostgreSQL, OpenRouter, integration providers, worker termination, packet loss, stalled queues, and repeated false interruptions;
- feature flag and staged rollout controls.

Gate:

- load, failure, permission, audit, and rollback acceptance criteria pass;
- measured `safe_sessions_per_pod` and a provisioned 100-room capacity model are documented;
- no unresolved high-severity privacy or authorization issue.

### Phase 6: local STT/TTS

Deliverables:

- shared local STT and TTS inference deployments;
- adapter compatibility tests;
- shadow comparisons and capacity model;
- per-workspace migration flag;
- removal plan for external audio routing.

Gate:

- local quality/latency/capacity meets or exceeds the chosen OpenRouter baseline;
- rollback to the prior adapter is immediate and does not alter conversations or sessions.

## Testing plan

### Unit tests

- session state transition legality;
- utterance idempotency;
- speech segmentation and Markdown suppression;
- bounded queue overflow/backpressure and ordered segment playout;
- provider request/response normalization;
- TTS cancellation and stale-frame rejection;
- interruption candidate -> commit/resume state transitions;
- false-interruption timeout and safe playout cursor;
- heard-text cutoff calculation and speech-record status;
- barge-in versus explicit tool-cancel classification;
- voice profile fallback;
- global return intent confidence;
- handoff authorization and context bounds;
- reconnect token authorization;
- delegation idempotency and target assignment.

### Integration tests

- local LiveKit room with synthetic audio participant;
- real OpenRouter test behind an explicit integration marker and budget limit;
- captured speaker-reference echo injected into the microphone path with AEC on/off comparison;
- fan, keyboard, cough, backchannel, and genuine-interruption audio corpus;
- final STT transcript -> one PostgreSQL message;
- direct run executor -> Redis text events -> TTS frames;
- user interruption -> tentative pause -> committed stream stop -> next safe turn;
- false interruption -> resume without a new message or run;
- foreground tool call -> activity -> result summary -> ordered TTS;
- interrupted side-effecting tool -> idempotent status check rather than blind cancellation;
- Boss -> Sidra -> Boss with separate durable conversations;
- pending tool confirmation over voice;
- delegated Todo task -> scheduler -> ARQ target run.

### End-to-end scenarios

1. Talk to Boss for five turns, interrupt twice, then continue in text chat.
2. Ask Boss to delegate a report to Sidra and continue talking to Boss.
3. Call Sidra, discuss the report directly, then ask Boss to return.
4. Disconnect and reconnect during Sidra’s leg.
5. Attempt handoff to disabled, archived, unrelated, and cross-workspace agents.
6. Trigger a consequential tool and approve/reject by voice and UI.
7. Exhaust voice capacity and verify pre-admission rejection.
8. Terminate one voice-worker pod and verify graceful drain for existing calls.
9. Play Boss through laptop speakers, interrupt naturally, and verify AEC prevents Boss’s own voice from creating a turn.
10. Produce coughs, keyboard noise, and short backchannels while Boss speaks; verify tentative pause and correct resume/commit behavior.
11. Interrupt while a read-only tool, a committed side-effect, and a background task are each active; verify their different cancellation semantics.
12. Switch Boss -> Sidra -> Boss while repeatedly speaking near the handoff boundary; verify no stale voice frames cross the committed leg.

### Load and soak tests

- 1, 5, 10, 20, 50, 100, and measured-capacity concurrent calls;
- regular short turns and long tool-wait turns;
- burst of simultaneous speech-end events;
- burst of simultaneous foreground tool calls constrained by per-integration bulkheads;
- simultaneous TTS generation and barge-in across many rooms;
- queue-slowdown injection for STT, Flexus events, TTS bytes, PCM, and RTC publication;
- 30- and 60-minute call soak;
- forced TURN/TLS path versus direct UDP;
- provider rate limiting and partial outage;
- worker rollout while rooms remain active;
- LiveKit node distribution, worker slot distribution, warm-spare exhaustion, and admission rejection at the exact configured limit.

## Rollout and rollback

Introduce a workspace-scoped feature flag such as `release_boss_voice`, independent of `release_boss_agent`.

Rollout order:

1. developer workspaces;
2. internal Flexus workspace;
3. named pilot workspaces with explicit consent to external STT/TTS;
4. small production percentage with concurrency cap;
5. broader rollout after one week of stable latency/error/privacy metrics;
6. local-speech pilots behind separate provider flags.

Rollback disables new voice-session creation and drains active calls. It does not delete voice-created conversations, transcripts, tasks, or audit history. Boss text chat and ARQ delegation remain available.

## Acceptance criteria

1. All media rooms, signaling, TURN, and voice workers use self-hosted LiveKit infrastructure.
2. No LiveKit Cloud or managed LiveKit inference endpoint appears in runtime configuration.
3. An admitted call keeps a persistent actor for its lifetime and does not require shared ARQ admission for every turn.
4. Final speech produces exactly one durable human message with voice provenance.
5. The existing Flexus run executor remains responsible for tools, memory, interrupts, persistence, and final response.
6. Speech-end to first-audio p95 meets the initial gate under warm target concurrency.
7. Barge-in stops obsolete audio within the initial p95 gate and never interprets interruption as approval.
8. Boss and Sidra use stable, perceptibly different voice profiles with deterministic fallback.
9. “Call Sidra” changes logical agent and conversation without reconnecting the LiveKit room.
10. Sidra receives only bounded scoped handoff context, not Boss’s complete private conversation or system instructions.
11. All direct turns after handoff are stored in Sidra’s conversation.
12. “Boss, come back” works as a global command and resumes the original Boss conversation.
13. A failed handoff leaves Boss active and does not claim success.
14. “Ask Sidra” creates one target-assigned durable task and leaves Boss active.
15. Delegated work runs through the scheduler and ARQ and survives voice-call termination.
16. Tool confirmations preserve existing Flexus permission and interrupt semantics.
17. Reconnect and worker retry cannot duplicate a committed transcript, run, handoff leg, or delegation.
18. Raw audio is not stored by default.
19. OpenRouter keys and LiveKit secrets never reach the client or durable message metadata.
20. Cross-workspace and unauthorized handoff/delegation attempts fail closed and are audited.
21. Over-capacity calls are rejected before admission with a clear retryable response.
22. Disabling the rollout flag stops new calls without affecting Boss text conversations or existing durable work.
23. One hundred private rooms map to one hundred isolated call actors distributed across generic worker replicas; no agent-specific or room-specific pod is required.
24. Worker scaling uses explicit measured session slots and warm spare capacity in addition to CPU/memory, and active actors drain during scale-down.
25. Client capture enables supported WebRTC AEC, noise suppression, and AGC and reports the effective degraded state when a feature is unavailable.
26. LiveKit Cloud enhanced noise cancellation and adaptive interruption inference are absent from runtime dependencies.
27. Every text, TTS, PCM, and RTC queue is bounded; stale `turn_id` or `speech_id` output is rejected after interruption, reconnect, or handoff.
28. Version 1 accurately describes OpenRouter STT as utterance-level while preserving an interface that can accept partial local STT later.
29. Genuine barge-in tentatively pauses playout within the latency gate, commits cancellation safely, and records the best-known heard cutoff.
30. A false interruption resumes safely without creating a user message, Flexus run, or duplicated speech.
31. Stopping or interrupting speech never implicitly approves, rejects, cancels, retries, or rolls back a tool operation.
32. Short foreground tools use the existing Flexus run and policy; long/retryable work returns a durable operation/task and leaves the realtime turn responsive.
33. Tool-provider bulkheads prevent a burst in one integration from blocking audio processing or exhausting all voice-worker resources.

## Open questions to resolve through prototypes

1. Which languages are required for the first pilot: English only, English/Russian, or broader multilingual support?
2. Which OpenRouter STT/TTS pair meets real p95 latency and intelligibility requirements from Flexus deployment regions?
3. What is the acceptable maximum call length and idle timeout?
4. What physical iOS and Android device set is required before mobile voice can be promoted into a later pilot?
5. Which provider-retention and routing constraints are mandatory before external beta?
6. How many concurrent admitted calls fit one voice-worker replica after real VAD, provider clients, and Flexus execution are measured?
7. Which client platforms satisfy echo, noise, and interruption gates with built-in WebRTC processing, and which require an approved on-device enhancement?
8. What minimum speech duration, false-interruption timeout, and backchannel policy produce the best tradeoff for English, Russian, and mixed-language calls?
9. What is the maximum safe text-segment, PCM, and RTC queue age before the actor should cancel or degrade to text?
10. Which existing Flexus tools are safe foreground tools, which need explicit cancellation metadata, and which must return durable background operation IDs?
11. What warm-spare slot count absorbs the expected production call burst without unacceptable cost?

These questions change configuration and capacity, not the core one-room, one-active-agent, multi-conversation-leg architecture.
