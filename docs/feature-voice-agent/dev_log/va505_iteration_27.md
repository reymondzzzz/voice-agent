# VA-505 iteration 27: active-agent and handoff states in the call UI

## Goal

Make the call UI show the right agent at the right moment: switch identity only when the server has
committed the new leg, keep the original agent visible when a handoff fails, and discard anything
arriving from a leg that is no longer active.

## What was tried

All three requirements collapse into one invariant once you look at the wire format. The state packet
already carries `vleg_seq`, and it increments only when `voice_leg_ops` commits a switch. So "switch
only at the committed boundary" and "reject stale output from the old leg" are the same rule seen from
two sides: apply a packet whose leg sequence is higher, ignore one that is lower.

That rule lives in a pure module, `voiceLegSwitch.ts`, and is unit-tested directly rather than only
through rendering. It returns one of four outcomes — switched, updated, stale, rejected — so a
same-sequence packet carrying a different agent or conversation is treated as malformed instead of
silently accepted.

## What broke

The client could never have displayed a handoff. `isVoiceStatePacket` validated incoming packets by
comparing `vleg_seq`, `vagent_id`, and `vconversation_id` against the identity captured at `join()`,
so every packet belonging to a second leg failed validation and was raised as a protocol error. The
feature was unreachable from the browser regardless of what the backend committed. Validation now
checks shape and session identity, and leg semantics are decided by the pure module.

Per-leg `vstate_seq` deduplication had to be narrowed to same-leg updates. A new leg's publisher starts
its counter at zero, so a switched packet would otherwise look like a replay of an already-seen
sequence and be dropped.

## What was decided and why

There is no separate PCM channel to the browser. A voice session is one continuous LiveKit room and
audio track for the whole call, one actor process serves every leg, and backend staleness is already
gated in `voice_livekit_pcm_sink.py` by turn and generation checks. The only per-leg signal the
frontend sees is `vleg_seq` on the state channel, so rejecting stale audio and rejecting a stale state
packet are the same gate at this boundary. That interpretation is recorded here rather than left
implicit, because a future reader could reasonably expect a separate audio-side check and find none.

The handed-off agent's name and avatar are fetched through the existing `agent_instance_get` resolver
that chat already uses, so no schema change was needed, and the query is skipped whenever the active
agent is the call's own agent — it never fires for an ordinary call, or while a handoff is merely
pending.

## Correction to an in-flight report

The implementing agent reported that `voice_call_actor.set_active_leg` has no caller. That is wrong:
VA-504 added one at `service_voice_agent.py:482` in `_apply_voice_return`. The claim was checked and
rejected rather than carried forward.

## The real remaining seam

The concern underneath that wrong claim is correct. `_apply_voice_return` updates the session, the
actor's leg sequence, and the tool-operation runtime, but `_vstate_publisher` belongs to
`_LiveKitVoiceCall` and is never rebound, so after a committed switch the publisher still emits the
previous `vleg_seq`. The durable switch happens and the wire signal does not reflect it. The frontend
is correct against the documented contract and will work the moment a higher sequence is emitted, but
today nothing emits one, so the switch cannot be demonstrated end to end.

This was closed in the same change rather than deferred. The publisher is registered on the actor via
`set_state_publisher`, and its publish callback carries no leg identity — it only writes bytes — so the
runtime can rebind it through the actor without touching the SDK. `VoiceStatePublisher.rebind_leg`
moves the identity, restarts `vstate_seq` at zero as the frontend expects a new leg's publisher to do,
and clears pending states. That last step matters: pending entries are bare state strings, so rebinding
without clearing would publish a state queued for the old leg under the new agent's identity.

What is still unwired is the LiveKit `AgentSession.update_agent` call. The voice profile is read fresh
per turn so the caller already hears the new agent, but the SDK rebind and a real call against live
LiveKit and OpenRouter belong to VA-507 and cannot run in the unit tier.

## Not fixed, pre-existing

`voiceCallController.ts` hardcodes `vlegSeq: 1` when recovering a saved session, so a browser that
reconnects after a committed handoff tracks leg 1 until the next state packet arrives. Closing it
needs the current leg on reconnect, which `voice_session_refresh_token` does not return today. None of
this ticket's requirements exercise that path.

## Verification

81 tests in the voice feature, up from 64, with 11 of the new ones asserting the switch rule directly
on the pure function: a higher sequence switches, an equal one updates only, a lower one is rejected as
stale even after a switch has already landed, an equal sequence with mismatched identity is rejected,
a pending handoff causes no optimistic switch, and a refused handoff that reverts to listening on the
same leg leaves identity untouched. `pnpm lint` passes at zero warnings with the a11y and design-token
guards green; `pnpm file-loc:check` and `pnpm i18n:validate` pass. One string was added, translated in
all three locales.
