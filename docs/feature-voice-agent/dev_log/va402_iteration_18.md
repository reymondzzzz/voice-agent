# VA-402 iteration 18: signed confirmation prompt and resume

## Goal

Let a live Boss call present and resolve the existing Flexus tool-confirmation interrupt without
creating a voice-only authorization path, leaking tool data, or treating silence and interruption
as approval.

## What was tried

The existing executor interrupt, signed confirmation token, GraphQL resolver, ARQ continuation,
ordinary detailed chat card, and direct warm voice executor were retained. A privacy-minimal
post-commit notice connects the executor to a call-scoped confirmation coordinator. The voice
pipeline routes utterances through a closed confirmation-intent classifier only while that notice
is pending. The compact call surface reuses the existing authorized mutation.

## What broke

Initial integration exposed four races. A stale run could notify the actor even when an idle or
cancelled conversation refused to persist its interrupt. A spoken response could overwrite a
different interrupt's already-durable pending resume. A web decision could race a spoken decision
or complete while prompt playout was still unwinding. Finally, a failed actor-fenced status probe
could leave the call waiting forever. Frontend review also found that a successful button could
briefly become actionable again before the durable subscription removed the card.

## What was decided and why

The durable interrupt finish now reports whether it actually persisted; only a successful finish
may emit the voice notice. Notices contain only interrupt ID and expiry and already-expired
interrupts are ignored. Voice direct runs disable standing automatic approval.

The actor speaks fixed server-owned text and accepts only exact multilingual approve/reject
phrases. Corrections, mixed phrases, backchannels, silence, and speech onset never decide. A valid
choice locks and reauthorizes the accepted session, actor epoch, active leg, conversation, agent,
and owner. It reads the signed token from the stored exact interrupt, refuses to replace another
pending resume, writes the native continuation plus content-free audit, and resumes the same
executor/checkpoint directly.

The existing web resolver remains on its ARQ path. A 500 ms call-scoped probe reads only the
actor-fenced durable phase and releases its database connection before waiting. `PENDING` and
`RESUMING` preserve the wait; terminal state clears it. A web/voice race becomes a safe handled
replay rather than a second execution. Probe or authority failure clears the notice and closes the
local call without a stale durable write. Reconnect restores waiting only while a notice remains
pending.

The call card is deliberately generic. Detailed tool fields remain in the ordinary chat card. A
successful decision remains locally disabled until the subscription removes it, and the
interactive section is not an assertive live region.

## Verification

Tests cover post-commit ordering, stale finish suppression, expiry, sink isolation,
automatic-approval disabling, exact intent classification, actor fencing, same/different interrupt
idempotency, audit privacy, spoken and web races, monitor failure, reconnect, prompt barge-in,
generic UI privacy, signed resolver reuse, stale response fencing, localization, and accessibility.

```text
VA-402 executor selection:   306 passed
VA-402 full voice selection: 586 passed, 2 skipped
VA-402 full frontend:        1094 passed
```

Frontend quality, build, guardrails, and the mandatory commit gate are recorded by the checkpoint
commit.

## Next

VA-403 remains blocked on the VA-301 physical client matrix through VA-303. The next unblocked
dependency-ordered work can proceed to VA-501 while that physical evidence is collected.
