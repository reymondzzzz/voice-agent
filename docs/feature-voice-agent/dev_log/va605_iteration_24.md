# VA-605 iteration 24: approved providers, bounded policy, degraded speech

## Goal

Make provider timeout and rate-limit behaviour bounded and audited, and make it impossible to route
speech to a provider nobody approved.

## What was tried

The allowlist is a separate declaration from the voice profile registry rather than derived from it.
Deriving it would have made the check a tautology: the registry is what chooses the provider and
model for a call, so an allowlist read from the registry would approve whatever it was handed. The
relationship is the same one tool policy has with the tool registry — the thing that enforces must
not be the thing being enforced.

Bounded retry and the circuit breaker reuse VA-404's `ToolBulkhead` rather than a second resilience
abstraction with different vocabulary. The retry decision stays in the provider policy because it
classifies each failure first; the bulkhead supplies the breaker and the slot.

## What broke

A real hole was already there. `_stt_config()` read `FLEXUS_VOICE_STT_MODEL` from the environment and
passed it into `SttConfig` with no validation, so any value in that variable was sent to OpenRouter
unchecked. That is exactly the failure this task exists to prevent, and it predated the task. The
call now goes through the policy gate, and a test drives an unapproved model and asserts the
transcribe callable was never invoked.

## What was decided and why

The gate sits in a wrapper layer, not inside `voice_stt.py` or `openrouter_tts.py`. The benchmark
harnesses `voice_stt_bench.py` and `voice_tts_benchmark.py` legitimately call those adapters with
non-default models to compare providers, which is their whole purpose; gating inside the adapters
would have broken provider comparison to buy nothing, since the benchmarks are not a call path.

The approval check runs once when the wrapper is constructed, not per call. If it refuses, the
wrapped closure is never created, so the network call is unreachable rather than merely unreached.

On exhaustion the original `SttError` / `VoiceTtsError` is re-raised with its audit record attached,
rather than a new exception type. `voice_echo_pipeline.py` already transitions to
`VoiceCallState.DEGRADED` and publishes it on those exact types, and that state is in
`VOICE_STATE_PRIORITY_STATES` so it survives coalescing. Introducing a new type would have required
editing a 975-line file with 25 lines of headroom to reimplement a transition that already works.

Timeout, rate limit, and provider error are three distinct audited outcomes, not one generic failure.
A 429 and a 503 mean different things to an operator and to a retry decision, and collapsing them
would have destroyed the signal this task is supposed to produce. Non-transient failures — auth,
invalid request, malformed response — burn exactly one attempt and are never retried.

The audit record has no field capable of holding a payload, key, or audio. That is structural rather
than a convention someone has to remember, and a test asserts the exact field set.

## Verification

15 tests, no mocks. An unapproved provider is refused with the underlying callable never invoked
(asserted as a call count of zero, for STT and TTS separately). Retry is proved by real attempt
counts. A dedicated test drives enough failures to open the shared breaker and asserts the next
call's audit outcome becomes `circuit_open`, which proves the VA-404 breaker is genuinely in the path
rather than a local counter. `flexus_backend/tests/services/voice` passes at 565 passed, 2 expected
skips, and `test_service_voice_agent.py` at 57 passed covers the wiring change.

## Not done

The frontend is untouched. The degraded state is published on the existing state protocol and is
observable to a client, but no UI work was in scope here; VA-505 owns call-UI states.

Per-workspace provider spend limits are not here either. Balance enforcement already lives in
`chat_hallucitron.py` via `OutOfCoinsError`, and a second spend gate in the voice path would compete
with it.
