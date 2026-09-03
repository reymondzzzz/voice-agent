# Realtime voice agent architecture

The system is built around one rule: **the audio plane and the semantic plane are separate.**

Audio never reaches LangGraph. Business logic never reaches the speech model. The two meet only in
`RealtimeAgentBridge`, which is the single place orchestration lives.

```mermaid
flowchart TB
    subgraph audio["Audio plane — frames, timing, barge-in"]
        Client["Client (browser / phone)"]
        LK["LiveKit room"]
        LKT["LiveKitTransport<br/><i>voice_agent/livekit/adapter.py</i>"]
        RSS["RealtimeSpeechSession<br/><i>protocol — one adapter per model</i>"]
        Model[["Future native<br/>speech-to-speech model"]]
        Sink["AudioSink"]
    end

    subgraph semantic["Semantic plane — meaning, state, work"]
        Bridge["RealtimeAgentBridge<br/><i>the only orchestrator</i>"]
        Runtime["ConversationRuntime<br/><i>who holds the floor</i>"]
        Graph["LangGraph<br/><i>ChatState, intent, instructions</i>"]
        Deleg["DelegationAPI<br/><i>the model's whole tool surface</i>"]
        Sup["TaskSupervisor + TaskRegistry"]
        TaskA(["background task"])
        TaskB(["background task"])
        Reason["ReasoningModel<br/><i>e.g. GPT-5.6</i>"]
        Mem["Memory"]
    end

    subgraph effects["Effect boundary — application authority"]
        Policy["ActionPolicy<br/><i>deterministic, app-owned</i>"]
        Service["ActionService<br/><i>prepare / confirm / commit</i>"]
        Handler["CommandHandler<br/><i>typed domain command</i>"]
        Store[("ActionStore<br/>durable")]
        DB[("database / external API")]
    end

    Mailbox{{"SessionMailbox<br/><i>async results back to the session</i>"}}
    Delivery["DeliveryPolicy<br/><i>speak, queue, interrupt or store</i>"]

    Client <--> LK <--> LKT <--> RSS <--> Model
    RSS --> Sink --> LK
    RSS -- "typed realtime events" --> Bridge
    Bridge -- "context, tool results,<br/>response requests, interrupts" --> RSS
    Bridge <--> Runtime
    Bridge --> Graph
    Bridge --> Deleg
    Graph --> Deleg
    Graph --> Reason
    Graph --> Mem
    Deleg --> Sup
    Sup --> TaskA
    Sup --> TaskB
    Deleg --> Service
    Service --> Policy
    Service --> Handler --> DB
    Service <--> Store
    TaskA --> Mailbox
    TaskB --> Mailbox
    Service --> Mailbox
    Mailbox --> Delivery --> Bridge

    classDef plane fill:#12161f,stroke:#2b3242,color:#e7e9ee
    class audio,semantic,effects plane
```

## Who owns what

| Component | Owns | Must never |
| --- | --- | --- |
| `RealtimeSpeechSession` | the duplex connection, spoken output, conversational timing | execute business logic or tools |
| `LiveKitTransport` | frames, room and participant lifecycle | contain orchestration |
| `RealtimeAgentBridge` | normalising events, routing tools, applying delivery decisions | perform effects itself |
| `ConversationRuntime` | who is speaking, current turn, epoch, idempotency sets | hold semantic history |
| LangGraph | semantic state, intent, task orchestration, instructions | see an audio frame |
| `TaskSupervisor` | every background asyncio task | outlive the session silently |
| `ActionService` | prepare/confirm/commit, idempotency, audit | trust model arguments at commit |
| `ReasoningModel` | planning, synthesis, verification | be the speech model |

## Lifecycles

### Normal conversation

1. `UserSpeechStarted` → runtime marks the floor as the user's, a turn id is minted.
2. `UserTranscriptPartial` revisions arrive and are ignored for decisions; only the final counts.
3. `UserTranscriptFinal` → the bridge hands the utterance to LangGraph, which classifies intent and
   prepares an instruction.
4. `UserSpeechStopped` → any queued results are flushed.
5. The speech model answers on its own. Nothing in the agent plane generates the sentence.

### Quick tool call

1. Model emits `RealtimeToolCallRequested{delegate_task, mode=quick}`.
2. The bridge dispatches to `DelegationAPI`, which runs the task inline.
3. The result is returned as the completion of *that* tool call, and the model speaks it.

### Background task

1. Model emits `delegate_task{mode=background}`.
2. `DelegationAPI` starts the task and returns `{"status": "started", "task_id": …}` **immediately**.
   That original tool call is now finished, permanently.
3. The model keeps talking.
4. On completion the supervisor publishes `BackgroundTaskCompleted` to the mailbox — a *new*
   asynchronous event, never a second answer to the original call.
5. `DeliveryPolicy` decides; the result reaches the model as `ContextScope.BACKGROUND`, never as
   fabricated assistant speech.

### Interruption

1. `RealtimeInterrupted` (or `UserSpeechStarted` while the assistant speaks) clears the assistant
   floor flag.
2. Queued results stay queued: an interruption means the user wants the floor, not that they want a
   backlog read out.
3. Only a `Criticality.CORRECTION` event causes the bridge to actively interrupt the model.

### Task superseding

1. A second `delegate_task` with the same goal produces the same fingerprint.
2. `TaskSupervisor.supersede_duplicates` cancels the older record with `SUPERSEDED` and records
   `vsuperseded_by`.
3. If the older task still completes, `TaskRegistry.accepts_result` rejects it.

### Important action (prepare / confirm / commit)

1. Model emits `request_action{action_type, arguments}` — this can never perform an effect.
2. `CommandHandler.prepare` resolves entities against authoritative state, computes impact, builds
   the typed command, and returns a precondition (e.g. a row version).
3. `ActionPolicy` decides — deterministically — whether confirmation is required.
4. The proposal and command are persisted in `ActionStore` with an idempotency key derived from the
   *resolved target*, not from a random uuid.
5. The user hears a summary naming the resolved entity and its impact.
6. `confirm_action(action_id)` approves that one proposal.
7. `commit` re-reads the stored command, checks the precondition, executes inside a transaction and
   records the outcome. A repeat returns the original result.

## Invariants, and where they are enforced

| # | Invariant | Enforced by |
| --- | --- | --- |
| 1 | A model inference cannot directly cause an irreversible mutation | `request_action` stops at a proposal; `ActionPolicy.ALWAYS_CONFIRM_LEVELS` |
| 2 | Retrying a side-effecting command cannot perform it twice | `idempotency_key` over the resolved target + `ActionStore.find_by_idempotency_key` |
| 3 | Confirmation approves one exact prepared proposal | `commit` executes `record.vcommand`, never fresh arguments |
| 4 | Background tasks cannot silently execute high-impact operations | `ActionPolicy.may_auto_commit(..., vfrom_background=True)` |
| 5 | A stale proposal cannot overwrite newer state | precondition captured in `prepare`, compared in `execute` → `CONFLICT` |
| 6 | Cancelling or superseding prevents future execution | `ACTION_LEGAL_TRANSITIONS` terminal states |
| 7 | Simultaneous commits execute once | per-action `asyncio.Lock` in `ActionService` |
| 8 | Duplicate realtime events are ignored | `ConversationRuntime.seen` / `tool_call_seen` |
| 9 | A background result is never a second answer to a tool call | mailbox delivery is a separate event path |
| 10 | A task is not killed merely because the turn changed | staleness judged on `conversation_epoch`, never `turn_id` |
| 11 | No orphan asyncio tasks after shutdown | `TaskSupervisor.aclose` returns any stragglers |

## Extension points for the real speech model

Adding a native duplex model should require **one adapter and a capability declaration**:

1. Implement `RealtimeSpeechSession` (`voice_agent/realtime/session.py`) — start, close, send_audio,
   commit_audio, add_context, send_tool_result, request_response, interrupt, events.
2. Declare `RealtimeModelCapabilities` (`voice_agent/realtime/capabilities.py`). Everything branches
   on these; nothing assumes a provider's behaviour.
3. Map the provider's websocket schema to the events in `voice_agent/realtime/events.py`. That
   mapping is the only place a vendor schema may appear.
4. Write output audio to an `AudioSink` (`voice_agent/realtime/audio.py`) so frames never enter the
   semantic bus.
5. If the model has no native function calling, set `function_calling=False` and supply a
   `TranscriptRouter` to the bridge; delegation is then derived from transcripts instead.
6. Register domain `CommandHandler`s. The tool surface the model sees stays the seven semantic verbs
   in `REALTIME_TOOL_SCHEMAS`.

Nothing else in the codebase should need to change.

## Unresolved decisions that genuinely depend on the model

- **Who owns barge-in.** If the model detects and stops on its own, `interrupt()` becomes an
  acknowledgement; if not, the transport must gate audio and the interruption thresholds in
  `voice_interruption_policy` become authoritative.
- **Whether context injection is synchronous.** `async_context_updates` exists because some
  backends only accept context between responses. If the chosen model does, the delivery policy
  needs a "wait for a gap" state rather than injecting immediately.
- **Transcript fidelity and revisions.** Whether partials are stable or rewritten decides if
  `UserTranscriptPartial.vrevision` needs conflict resolution beyond "only finals count".
- **Whether the model can hold tool results mid-response.** `supports_tool_results` is declared but
  the timing contract — can a result arrive while it speaks? — is provider-specific.
- **Audio format and rate at the boundary.** Currently the room rate from `voice_contracts`;
  a model with a fixed input rate moves resampling into the adapter.
- **How much history the model keeps.** Decides whether `ChatState` must replay context on
  reconnect, or whether the model reconstructs it.
- **Playback acknowledgement.** Without `supports_audio_playback_ack`, "the assistant finished
  speaking" is inferred from events rather than known, which weakens correction timing.

## Testing without a speech model

`FakeRealtimeSpeechSession` implements the protocol exactly. Tests push events in
(`emit_final_transcript`, `emit_tool_call`, `emit_interrupted`) and assert on what came back
(`vsent_context`, `vsent_tool_results`, `vresponse_requests`, `vinterrupts`). The whole agent plane,
including the effect boundary, is covered without a room, a model, or audio.
