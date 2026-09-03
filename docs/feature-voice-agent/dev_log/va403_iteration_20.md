# VA-403 iteration 20: tool cancellation and late outcomes

## Goal

Make speech interruption independent from business-operation cancellation, declare the initial Boss
tool behavior, and preserve exact turn correlation when a result arrives after the voice moved on.

## What was tried

The initial Boss allowlist was classified per operation instead of per tool name. Each declaration
states foreground, confirmation, or background execution; read-only or committing side effects; and
whether explicit cancellation is allowed. A content-free executor outcome stream carries only the
originating turn, opaque activity, tool name, phase, and declaration.

The direct executor was moved from turn ownership to call ownership and awaited through a shield.
Turn cancellation can now stop TTS, queues, and stale presentation without injecting cancellation
into a tool run. Session close still owns and cancels the call-scoped task.

## What broke

The previous turn task awaited `execute_run` directly. Committed barge-in cancelled that task, which
could inject cancellation while a tool was already working. This contradicted the existing written
contract that stopping speech does not roll back an operation.

A tool-level boolean was also insufficient for mixed tools. `forms`, `flexus_kanban`,
`flexus_task_todo`, `agent_memory`, `boss_hire`, and `boss_colleague_setup` contain both reads and
writes or confirmation-gated operations. They require operation-level declarations.

## What was decided and why

Read-only foreground operations are explicitly cancellable. Committing operations, confirmation
operations after invocation, background work, unknown operations, and a run with any parallel
non-cancellable activity refuse cancellation. Cancelling the whole run while one parallel operation
has crossed a side-effect boundary would be unsafe.

An explicit request carries a bounded request ID. Repeating the same request for the same target
returns the same result; reusing the ID for a different target raises. The call-local replay ledger
is capped at 128 entries. Speech stop has a distinct `speech_only` result and never touches the
executor task.

Start and terminal outcomes are idempotent per opaque activity ID. They contain no arguments,
results, provider body, transcript, error text, or secret. A late result preserves its old `turn_id`;
the owned-turn voice activity callback already ignores it for presentation.

## Verification

Tests cover every tool in the Boss blueprint, mixed operation classification, fail-closed unknowns,
safe outcome shape, duplicate suppression, late-turn correlation, speech-only interruption,
explicit cancellation, parallel non-cancellable refusal, request replay, request-ID misuse, and the
real voice runtime's shielded executor ownership.

The focused VA-403 and voice regression selection passed with 618 tests and two expected SDK-gated
skips. The mandatory repository gate is recorded by the checkpoint commit.

## Next

VA-404 owns integration bulkheads, provider deadlines, circuit breakers, metrics, and durable
background-operation IDs. This task does not invent those contracts early.
