# VA-206 iteration 12 — safe Boss response speech

## Goal

Replace the durable call's transcript echo with Boss's real response while keeping assistant
messages, tools, permissions, and run completion owned by the existing executor.

## What was tried

`run_executor.execute_run` now returns a typed completion only after a normal terminal finish. The
completion carries the final LangChain messages, authoritative conversation/agent/workspace IDs,
and monotonic executor timing. Retriggered, interrupted, preempted, handoff-enqueued, and unclaimed
runs return no completion and therefore cannot produce voice output.

The voice runtime sends the final messages through a pure fail-closed segmenter. It selects only
the last AI response after the latest human message, rejects tool metadata and non-text blocks,
removes structured and tagged reasoning, fenced/inline code, URLs, HTML, and Markdown-only syntax,
then splits the remaining text within the configured 160-character bound.

## What broke

The first multi-phrase design reset the PCM sink for every phrase. That made each provider stream
look locally ordered but weakened the stale-output boundary after cancellation. The actor also
remained `TRANSCRIBING` while the executor ran, so microphone input was ignored and a caller could
not interrupt a slow Boss tool or model turn.

Multi-phrase failures exposed two trace assumptions. A provider-open failure after an earlier
completed phrase had no generation ID for a new incomplete phrase, and generated-audio usage did
not include bytes received by the active failing stream even though some frames had played.
The final audit also found that a provider close deadline could escape without a terminal trace
and let outer cleanup attempt the same close twice.

## Decisions and why

The actor never speaks Redis assistant deltas. They are deliberately suppressed by the existing
publisher and are unsafe before the tool loop and completion guards settle. VA-206 uses terminal
message state for correctness; a future incremental path must provide an equally strong post-tool
protocol before it can reduce first-phrase latency.

All phrases share one `speech_id`, one sink admission, and one continuous frame sequence. TTS
providers open, stream, and close sequentially. Cancellation invalidates the whole turn, closes the
one active provider, and prevents any later phrase from opening. The actor enters `THINKING` before
durable execution, so the existing VAD threshold can cancel the owned executor and preserve the
new utterance.

Each provider stream now owns bounded cleanup in its segment scope. A close deadline becomes one
stable retryable TTS error without replacing a primary stream error or cancellation, and active
ownership is cleared deterministically so cleanup is attempted once.

Executor authority is checked again at the speech-plan boundary. Assistant persistence remains in
the executor; the voice layer performs no assistant write and no database reread.

## Verification

```text
voice, worker, and execution regression selection: 754 passed, 2 skipped
focused Ruff and format:                        passed
mandatory commit gate:                          9 passed
```

Coverage includes terminal-result suppression, safe response extraction, reasoning/tool/URL/code
removal, bounded phrase splitting, real-response-versus-echo selection, continuous multi-stream
frame ordering, later-segment provider failure, empty safe output, executor-time barge-in,
later-segment barge-in, trace identity/timing, cancellation cleanup, and unchanged executor
persistence semantics. The final source audits reported no remaining P0/P1 findings.

## Next

VA-207 adds the Boss call UI states, durable transcript reconciliation, and reconnect behavior.
