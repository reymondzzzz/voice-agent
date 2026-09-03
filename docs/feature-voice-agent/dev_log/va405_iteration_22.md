# VA-405 iteration 22: proving the tool matrix, M4 exit gate

## Goal

Prove that the foreground, confirmation, background, and undeclared tool classes each report truthful
outcomes across policy, bulkhead, and voice runtime together, and record the M4 exit evidence.

## What was tried

The proof is written as one matrix rather than more unit tests. VA-401 through VA-404 each already
have their own passing suites, so restating them adds nothing. What had never been asserted is the
seam: that a single operation class behaves consistently across classification, cancellability,
retryability, durable background identity, and voice-foreground activity at the same time.

Each row asserts the full truth for its class instead of one property. Retry is proved by call count,
not by a flag: a foreground read-only operation runs its body twice under `vmax_attempts=3` and
succeeds on the second attempt, while confirmation, committing, background, and undeclared operations
each run their body exactly once before the exception propagates.

## What broke

Nothing. Every row behaved as specified and no production change was needed.

## What was decided and why

The undeclared row asserts every axis in one test rather than spreading them. Fail-closed is a single
property with several visible consequences, and splitting it would let one axis regress while the
suite stayed green. It covers an unknown tool name, an unknown operation on a known tool, and a
non-string name, and additionally asserts that an undeclared operation classified BACKGROUND still
mints no durable operation ID.

The provider-limit rows assert that the refused call's body never executed, and run a
`VoiceToolOperationRuntime` alongside the rejection to prove voice state is not corrupted by it: a
bulkhead rejection must not leave an operation looking active or cancellable.

The parallel-blocker test deliberately uses a background operation as the non-cancellable blocker.
`test_voice_tool_operations.py` already proves the committing-write blocker, so reusing it would have
duplicated coverage instead of extending it.

`is_voice_foreground_tool` keys only on execution mode, so a committing operation such as
`flexus_kanban(create)` counts as voice-foreground UI activity while remaining non-cancellable and
non-retryable. That is correct — surfacing activity in the call UI and being safe to cancel or retry
are different questions — and the tests now state it explicitly rather than leaving it implicit for a
future reader to rediscover.

## Verification

27 tests, 50 assertions, no mocks and no patching: every assertion is on observable behaviour or a
real call count. The neighbouring tiers (`execution`, `tools/policy`, `services/voice`) pass at 1027
passed, 2 expected SDK-gated skips.

## M4 exit review

- No voice-only business tool bypasses Flexus policy: every tool in `VOICE_BOSS_TOOL_NAMES` and every
  operation it declares resolves to a declared policy, and a known tool called with no operation key
  resolves undeclared unless it declares a default.
- Long and retryable work leaves the conversation responsive: a background-declared operation mints
  its durable ID while the turn's executor task is still running, and the background path never
  awaits it.
- The initial Boss tool allowlist is classified foreground, confirmation, or background by
  `tool_operation_policy.py`, landed in VA-403.

## Next

M5 opens. VA-502 and VA-506 are the dependency-valid entry points; VA-503 then VA-504, VA-505, and
VA-507 follow. The voice session leg schema from VA-201 already carries agent, conversation, voice
profile, end reason, and source leg, so the handoff coordinator needs no migration.
