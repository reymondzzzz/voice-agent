# Iteration 7 — VA-201 durable voice state

Date: 2026-09-01. Branch: `voice-agent`.

## Goal

Persist enough authoritative state for an authorized control plane to create, reconnect,
end, and later hand off one voice call without treating LiveKit metadata, an in-memory
worker, or ARQ as the source of truth.

## What was tried

The data model was split into three normalized tables. A session owns the human, group,
opaque room, lifecycle timestamps, reconnect expiry, and active leg. A leg binds one ordered
period of the call to exactly one agent and Flexus conversation. A speech row binds the
actor's speech identity to one existing assistant message and records provider/voice choice,
lifecycle, generated duration, played duration, and the best-known played-text boundary.

The migration is additive and contains tables, indexes, checks, and foreign keys only. It
adds composite group-plus-ID keys to agents and conversations so the database, rather than
future service code alone, rejects tenant-crossing legs. Session deletion cascades through
legs and speech; referenced agent, conversation, and message history uses `NO ACTION`.

## What broke

Formatting the multi-file Prisma schema initially rewrote an unrelated ERP schema and much
of the public file. That churn was removed; the final Prisma diff contains only the new
relations, composite keys, and three voice models.

The first draft used zero-based leg defaults even though the existing actor starts at leg
one, and it accepted a speech ID shape different from the actor's namespace. Both were
aligned to the executable contract: legs start at one and speech IDs are exactly
`<vsession_id>.<vleg_seq>.<vturn_seq>.speech`.

The final relational audit found two provenance gaps. Separate same-group agent and
conversation keys allowed a leg to claim Sidra while pointing at a Boss conversation, and
the message key allowed speech to point at a user or tool message. The final migration
binds group, agent, and conversation in one composite foreign key and binds speech to a
message key that is checked to the `assistant` role. Real negative tests cover both.

The real database rehearsal found one faulty negative test. It inserted leg one with source
leg one, so the source-order check correctly failed before the missing-source foreign key
the test intended to exercise. The case now inserts leg two referencing absent leg one and
proves the foreign key directly.

## Decisions and why

No raw audio, access token, transcript copy, message content, or unbounded provider response
is stored. PostgreSQL owns durable identity and honest playout provenance; transient frames
and queues remain inside the call actor.

`vsession_active_leg_seq` is deliberately not a foreign key back to the leg table. Such a
key would make session and first-leg insertion cyclic. VA-202 must insert both atomically,
and VA-203 must lock the session and validate the target leg before updating the active
sequence.

Exactly-once finalized user transcript identity remains VA-204. Its
`(vsession_id, voice_utterance_id)` invariant is not mixed into assistant playout rows.
Likewise, this task stores reconnect state but does not create actor leases or tokens; those
belong to VA-202 and VA-203.

## Result

Prisma validation and client generation passed. Seven static migration tests prove the
single additive migration's shape and safety properties. Four DB-backed tests applied the
complete schema to a fresh isolated PostgreSQL database and passed real insert, status,
opaque-ID, speech-ID, counter, relationship, restriction, and cascade behavior. The
disposable rehearsal database was removed afterward.

## Next

VA-202 adds authenticated, authorized, quota-aware, idempotent create/refresh/end mutations
that write this schema and mint only short-lived, room-scoped LiveKit credentials.
