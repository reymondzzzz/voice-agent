# VA-205 iteration 11 — direct warm-actor execution

## Goal

Run a finalized Boss voice turn through the existing Flexus executor without adding shared ARQ
admission latency or creating a second agent, tool, permission, checkpoint, or message runtime.

## What was tried

Each admitted call now opens a bounded PostgreSQL pool, the ordinary AgentRegistry, the configured
Redis checkpointer, the predefined per-agent tool catalog, the voice lease Redis client, and a
separate correctly configured ARQ connection for later continuations. After VA-204 commits the
human message, the actor calls `run_executor.execute_run` directly with those warm resources.

Executor database access now binds the passed pool through a task-local context. Existing ARQ
workers still fall back to their process-global pool, while concurrent voice calls cannot replace
one another's database dependency.

## What broke

The first bridge draft exposed four hidden assumptions from the ARQ-only world. `RUNNING`
conversations were always reclaimable because ARQ's job reservation had been the real concurrency
guard. Replayed and negative-sequence queued transcripts had no explicit direct-dispatch outcome.
The direct call omitted the ARQ connection needed when its finish step promotes a queued follow-up.
Finally, a cancelled executor could outlive the actor's one-second provider cancellation deadline
while shutdown closed its database and checkpoint resources.

The first local PostgreSQL test attempt was blocked by the workspace sandbox's local-network rule,
not by the implementation. The same test passed against the disposable database with the required
local connection permission.

## Decisions and why

`PersistedVoiceTranscript.vdirect_run_required` is true only for the transaction that creates a
visible message. It is false for every replay and for a message queued behind an active run. A
crash after persistence therefore falls back to the existing sweeper instead of risking duplicate
tool execution.

The locked run claim rejects a fresh `RUNNING` conversation and accepts it only after the existing
stale-recovery boundary. This makes a voice-versus-ARQ race choose one owner without removing crash
recovery. The ARQ connection is passed into the executor only so its normal finish semantics can
schedule promoted messages; the initial voice message is never enqueued.

Cancelled tasks that miss the short provider deadline remain in the actor's owned task set. Job
shutdown waits up to 15 seconds for executor recovery, then retains one crash-reported finalizer
that closes the checkpointer, ARQ connection, voice Redis client, and PostgreSQL pool after the
task actually exits. Executor failures keep the durable conversation error semantics and do not
mislabel transcript persistence or end the whole call.

This iteration does not add a voice-specific post-commit `message_insert` publication. The human
message is durable immediately, while VA-207 owns call-UI transcript reconciliation through the
ordinary conversation event/subscription contract. Adding a partial publisher here would split
that responsibility before the client synchronization task exists.

VA-205 intentionally leaves the existing echo TTS placeholder after executor completion. VA-206
must replace it with safe assistant-event segmentation; this iteration does not claim that Boss's
generated answer is spoken yet.

## Verification

```text
focused voice and claim selection: 485 passed, 2 skipped
agent-execution regression suite:   289 passed
PostgreSQL transcript/claim e2e:      2 passed
focused Ruff and format:              passed
commit gate:                          9 passed
```

The PostgreSQL test races two simultaneous claimers and proves one fresh `RUNNING` owner. Focused
coverage also proves direct-versus-replay/queued decisions, exact executor resources, ARQ follow-up
availability, predefined tool discovery, executor-error separation, concurrent task-local pools,
fresh/stale recovery, cancellation-resistant task ownership, bounded drain, deferred cleanup, and
partial-open cancellation cleanup.

## Next

VA-206 converts only safe user-facing assistant events into ordered speech segments and replaces
the temporary transcript echo with Boss's actual response.
