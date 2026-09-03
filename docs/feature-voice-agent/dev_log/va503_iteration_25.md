# VA-503 iteration 25: durable legs and the handoff coordinator

## Goal

Commit a live agent switch as one durable transaction: end the current leg, start the target leg,
create the target conversation the first time and resume it afterwards, and refuse a stale actor.

## What was tried

One symmetric operation handles both directions. Boss to Sidra and Sidra back to Boss differ only in
which authorization is passed, so a separate return path would have been a second mechanism to keep
correct. `commit_voice_leg_handoff` takes VA-502's `VoiceHandoffAuthorization` and never re-decides
what VA-502 already resolved, including whether the target conversation is new.

The whole switch runs under `FOR UPDATE` on the joined session and active-leg row. Serialising on the
session row is what makes a duplicate safe, rather than an application-level check that two racing
callers could both pass.

## What broke

The first version left `flexus_voice_session.located_fgroup_id` pointing at the source group.
`voice_session_runtime` loads the actor with
`JOIN flexus_agent_instance a ON a.located_fgroup_id = s.located_fgroup_id AND a.agent_id =
l.vleg_agent_id`, so after a handoff to an agent in a different group that join matches nothing and
the actor can no longer load its own session — claim, heartbeat, and participant handling all break
after a transfer that appeared to succeed. The session's group now moves with the active leg in the
same transaction, and the audit records both the old and new value.

## What was decided and why

The idempotency short-circuit runs before the source-group consistency check. A duplicate attempt
carries the original authorization, whose source group is the leg that has already been switched
away from; checking consistency first would have rejected the duplicate as "moved on" instead of
recognising it as a repeat. When the active leg already belongs to the target agent, the call returns
the existing leg with `vidempotent=True` and writes nothing.

The consistency check still exists for the case it is actually for: authorizing against one leg and
committing against a different leg that raced in between.

The scoped context item is a `cd_instruction` row in the target conversation, the same mechanism the
messenger reply guard already uses. Its provenance carries the source agent, source conversation,
source group, the bounded summary, and the expert name, and nothing else. The test asserts the exact
key set rather than the presence of a few fields, so a later change cannot quietly widen it. VA-502's
authorization carries a single summary string, so the plan's "caller's request" and "safe handoff
summary" are one field here; there is no separate raw request to leak.

## Verification

8 tests, no mocks: first handoff creates one conversation and one leg with `source_leg_seq` set; a
second handoff to the same target resumes rather than creating another; returning to the original
agent resumes the original conversation rather than making a third; a failed leg insert leaves the
old leg active with no new leg; a stale actor epoch refuses and writes nothing; a duplicate commit is
idempotent; the context item excludes the source system prompt and history; an ended session refuses.

`flexus_backend/tests/services` and `flexus_backend/tests/langgraph_runtime` stay green.

## Not wired

Nothing in production calls `commit_voice_leg_handoff` yet, so a handoff is not end to end. VA-502's
tool authorizes and returns a bounded result; the commit belongs to the call actor, because the plan
sequences it after the outgoing sentence has drained and immediately before the LiveKit
`update_agent` switch. That switch and the actor call site are VA-504 and VA-505. The livekit SDK is
an optional extra excluded from CI, so the media switch cannot be unit tested here in any case.

## Next

VA-504 owns the global "Boss, come back" routing and the actor call site that drives this coordinator;
VA-505 owns the call UI states at the committed boundary.
