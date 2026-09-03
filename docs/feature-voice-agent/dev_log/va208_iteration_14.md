# VA-208 iteration 14: durable Boss vertical-slice proof

## Goal

Prove that the M2 components operate as one durable Boss conversation rather than as independent
session, transcript, executor, speech, and UI features. Five voice turns must create five ordinary
human messages and no more than five direct runs, survive reconnect, and remain visible and usable
through normal text chat.

## What was tried

A database-backed acceptance test uses the real GraphQL session lifecycle, PostgreSQL voice actor
state, transcript persistence, and production conversation run gate. It creates a voice session for
an existing conversation, claims and connects the actor, persists five unique finalized utterances,
claims and completes the run gate after each turn, and replays the first utterance.

The test then refreshes the same session identity, disconnects and resumes the same durable leg,
ends the call, lists messages through the ordinary conversation GraphQL field, and appends and
reads a normal text message after the call. Two frontend tests cover the browser boundaries that a database test
cannot: a full component remount recovers the persisted session without creating another one, and
a voice-provenance message renders in the ordinary chat timeline.

## What broke

The existing `flexus_dbtest` database contained an earlier failed migration, so Prisma correctly
refused to apply newer migrations. The proof was rerun against a fresh dedicated local test
database instead of resolving or deleting history in the shared test database. The fresh schema
migrated and the vertical test passed.

The first frontend quality command was launched from the repository root, causing the package
manager to attempt dependency work outside the writable workspace. Running it from the frontend
package with the pinned Node version and explicit Flexus Python resolved the environment issue.

## Decisions and why

The database proof records five direct-run-eligible results and independently exercises five
successful claims through the production run gate; it does not mislabel those manual claims as
executor invocations. Existing VA-205 tests prove that only a newly inserted eligible result
invokes `execute_run`, and concurrent claim tests prove competing entry paths cannot both own the
run gate. Adding a separate historical run ledger only for VA-208 would create a second source of
truth and expand production scope without a runtime need.

The durable transcript is read and rendered through existing conversation contracts. There is no
voice-only message list, page cache, or parallel transcript. Browser storage retains only opaque
recovery identity and never a LiveKit token.

## Verification

```text
PostgreSQL vertical-slice acceptance: 1 passed
Focused frontend acceptance:         29 passed
Full frontend suite:                  1066 passed
Backend changed-file Ruff:            passed
Frontend changed-file ESLint:         passed
Frontend typecheck and GraphQL:        passed
```

## Next

VA-301 establishes the supported-client AEC, noise suppression, and automatic gain compatibility
matrix before the two-phase interruption work depends on browser echo behavior. Group and
conference communication remains future work.
