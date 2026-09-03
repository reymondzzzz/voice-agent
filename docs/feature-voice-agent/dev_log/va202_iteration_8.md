# Iteration 8 — VA-202 authorized voice lifecycle

Date: 2026-09-01. Branch: `voice-agent`.

## Goal

Expose authenticated create, refresh-token, and end mutations for durable voice sessions. The
surface must authorize the human, group, agent, and conversation; enforce active-call quotas;
mint only short-lived room credentials; audit lifecycle writes; and remain idempotent across
client retries and ambiguous self-hosted LiveKit responses.

## What was tried

The existing VA-201 migration was extended instead of adding a second migration directory. A
session now stores the caller create-request ID, stable opaque participant identity, LiveKit
dispatch ID/state, and the last dispatch-attempt timestamp. Owner plus request ID is unique.

`voice_session_ops.py` reserves a new conversation, session, and first leg in one PostgreSQL
transaction after locking the workspace, reauthorizing the human, validating the enabled agent,
locking the human owner row, and enforcing workspace and user active-session caps. The user lock
serializes both global user quota admission and request-key replay across workspaces. The same
transaction writes audit rows for every created durable record. Dispatch happens only after commit. Refresh and end lock the durable
session, reauthorize its persisted group and exact human owner, and never trust a caller-supplied
room or participant identity.

The self-hosted LiveKit control plane now lists dispatches for the exact opaque room before create.
One signed generic-worker match is reused; zero matches permits create; malformed, mismatched, or
multiple dispatches fail closed. Worker admission still requires fresh signed metadata, while
control-plane reconciliation may validate an older signed record without treating its original
admission expiry as a deletion signal.

## What broke

The first service pass made a stale `PENDING` attempt permanently unclaimable, because the retry
condition rejected every pending row regardless of age. It also allowed an already accepted active
session to regress to `CONNECTING` during a repeated acknowledgement. The final transition rules
allow one stale claim after the bounded delay, preserve accepted active state, refuse terminal
resurrection, and mark end as both lifecycle `ENDED` and dispatch `CANCELLED`.

The existing echo-prototype tests assumed every LiveKit request was `CreateDispatch`. Adding
reconciliation introduced a preceding `ListDispatch`, so the fixture was changed to model both
endpoints and assertions now select the create request explicitly.

The first Prisma validation attempt had no `DATABASE_URL`; rerunning with an explicit local value
validated and regenerated the client. The isolated PostgreSQL rehearsal then applied the complete
schema and exercised both schema constraints and real GraphQL lifecycle mutations.

Final review found that workspace locking alone did not serialize the global per-user quota or a
same-request race across two workspaces. It also found that omitted conversation input acted as a
replay wildcard, retryable LiveKit 4xx responses were being terminalized, and the old diagnostic
mutation shared the durable feature flag. Owner-row locking, exact persisted conversation intent,
retryable-status reconciliation, and a separate legacy-start opt-in close those gaps. Request IDs
are hashed before persistence so free-form caller text never enters the durable audit trail.

## Decisions and why

Database idempotency alone is insufficient. LiveKit may accept a dispatch and lose the HTTP
response, so a blind retry can create two workers for one room. Durable `UNCERTAIN` state plus
room-scoped `ListDispatch` reconciliation closes that cross-system gap without storing tokens or
provider payloads.

Tokens are minted only after dispatch state is durably `ACCEPTED` and are never stored. Refresh
uses the persisted participant identity so it rotates credentials rather than creating another
participant. Ending stays available when new voice admission is disabled, because a rollout flag
must never prevent cleanup of an existing call.

The echo-only prototype mutation remains temporarily available for the M1 diagnostic page only
when `FLEXUS_VOICE_LEGACY_START_ENABLED=1` is also set. It is separate from the new durable Boss
control path, is disabled by default, and still creates no Flexus conversation.

An accepted room dispatch proves a single matching signed LiveKit record, not that its actor ever
became live. VA-203 owns authoritative actor bootstrap and bounded lease liveness.

## Result

The focused unit and static schema tier passes 78 tests. The database-backed tier passes 10 real
migration and GraphQL lifecycle tests: request uniqueness and state checks, concurrent create
replay, cross-workspace user-quota serialization, one-dispatch guarantee, concealed other-owner
lookups, exact conversation intent, atomic conversation/session/leg persistence, audit
cardinality, stable token refresh, idempotent end, and refresh refusal after end. The disposable
test database was removed after the rehearsal. Three independent read-only Terra audits reported
no remaining actionable findings after the fixes.

## Next

VA-203 loads the authoritative session by `vsession_id`, acquires and renews a bounded Redis lease,
transitions connection/reconnect state without duplicating the actor, and closes promptly when the
session becomes terminal.
