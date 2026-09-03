# VA-203 iteration 9 — durable actor lease and reconnect

## Goal

Turn an accepted LiveKit dispatch into one authoritative Flexus voice actor. The worker must
load the durable session and active leg, exclude duplicate actors, survive a short client
disconnect without changing conversation identity, and close without duplicate lifecycle work.

## What was tried

The implementation was split into three SDK-free seams and one LiveKit adapter:

- `voice_actor_lease.py` implements one Redis owner token per session with atomic acquire,
  compare-and-renew, and compare-and-release;
- `voice_session_runtime.py` implements locked PostgreSQL bootstrap and liveness transitions;
- `voice_call_actor.py` takes the durable active-leg sequence and avoids cancelling or awaiting
  the task currently closing it;
- `service_voice_agent.py` validates signed metadata, binds the lease and durable snapshot to
  LiveKit participant events, owns the reconnect timer, and cleans up per-job resources.

## What broke

The first integration used an async LiveKit `setup_fnc` and assigned `JobProcess.userdata`.
Inspection of the exact pinned `livekit-agents==1.7.1` wheel proved setup is called
synchronously and `userdata` is a read-only dictionary property. Setup now stores only a lazy
resource owner in that dictionary; asynchronous connection happens inside the job entrypoint.

The first resource opener also used asyncpg defaults. That would create ten idle PostgreSQL
connections per voice job, or roughly one thousand at one hundred simultaneous rooms. Flexus now
exposes the existing PgDog-safe pool construction as `create_pg_pool`, and voice jobs use one
idle and two maximum connections. Redis uses a dedicated client on logical database 13.

Duplicate LiveKit disconnect events initially attempted the illegal local transition
`RECONNECTING -> RECONNECTING`, and an exact-deadline reconnect could return a durable `ENDED`
session while the local actor resumed listening. Both paths now fail closed and are tested.

## Decisions and why

PostgreSQL is the only durable lifecycle truth. Redis holds only
`voice:actor:<vsession_id>` with a random token, 15-second TTL, and 5-second renewal. Lua compares
the token before extending or deleting, so a stale actor cannot affect its replacement. Redis
absence or failure rejects actor ownership; there is no in-process fallback.

Final concurrency review found that the Redis fence alone could not reject a paused actor after
its TTL if a replacement had already started. Claim now increments the durable
`vsession_actor_epoch`; every later worker transaction requires the claimed epoch, and the worker
renews Redis before lifecycle writes. This fences the old process on both sides of replacement.
The same review made provider/SDK initialization cleanup unconditional and made leg-end audit
conditional on an actual leg update.

The reconnect window is a fixed 20 seconds. The first matching disconnect stores its deadline;
repeat events cannot extend it. A matching participant returning before expiry restores `ACTIVE`
without creating an actor, leg, conversation, or transcript. Expiry closes the session, dispatch,
and active leg in one transaction. User end racing actor close is serialized by the session row
lock and produces one terminal audit set.

LiveKit accepted dispatch remains admission evidence, not liveness. The actor acquires the Redis
fence before PostgreSQL bootstrap and media connect. It trusts only the signed session and exact
room from metadata, then resolves owner, tenant, participant, agent, leg, and conversation from
PostgreSQL. Lease loss stops the old actor without a later durable write.

## Verification

Focused unit coverage includes lease expiry, stale-token and stale-epoch fencing, Redis failure, authoritative
bootstrap rejection, idempotent connect/disconnect/close, fixed reconnect deadlines, signed room
validation, pinned-SDK process setup, bounded resource pools, participant filtering, and local
fail-closed behavior. Database-backed tests exercise the accepted bootstrap, same-leg resume,
expiry close, and user-end/actor-close race against a fresh PostgreSQL schema.

The exact command totals and full repository gate result are recorded after final integration in
`integration_iteration_1.md`.

## Next

VA-204 persists each finalized human voice transcript exactly once under
`(vsession_id, voice_utterance_id)` and creates one ordinary conversation message with voice
provenance. It does not replay audio or introduce a second conversation store.
