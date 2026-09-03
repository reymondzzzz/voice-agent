# VA-207 iteration 13: Boss call UI, state, transcript sync, and reconnect

## Goal

Put the durable Boss voice call inside the existing chat without creating a second transcript or
session authority. The UI must show useful realtime state, recover the same call after transport
loss, and leave the conversation usable as ordinary text after the call ends.

## What was tried

The worker gained a small versioned LiveKit data protocol for actor lifecycle state. The publisher
is private to the persisted human participant, reliable, bounded, deadline-limited, and
crash-reported. The browser validates the session, leg, sequence, agent, conversation, packet size,
and sender before accepting a state.

The durable voice mutations were added to the frontend GraphQL document. A Boss-only call control
was placed between the existing transcript and composer. Its controller stores only opaque recovery
IDs, fences stale asynchronous completions, refreshes the same session after a terminal disconnect,
and ends the durable session before clearing its recovery identity.

Newly inserted visible voice transcripts now publish the ordinary post-commit `message_insert`
event. Assistant messages already use the executor's normal persistence and event path, so the
existing chat subscription remains the only durable transcript source.

## What broke

The first room-state sender check assumed the worker participant would use the human `vpart_`
identity prefix. LiveKit agent participants do not share that application identity contract. The
check was narrowed to require a real remote participant and reject the exact persisted human
identity.

The initial UI lifecycle lived in one oversized component and could clear the recovery identity
after an ambiguous refresh. It was split into a bounded controller plus a small render component,
and ambiguous refresh/end failures now retain the same session for idempotent retry. Operation
generation prevents late create or refresh results from joining after end or unmount.

The prototype page's older room-state shape and generic connection-error copy no longer matched the
expanded client contract. Its initial/test state was extended, and connection/protocol failures no
longer claim that the microphone failed.

## Decisions and why

LiveKit data messages carry presentation state only. They never carry transcript text, tool data,
tokens, or durable truth. This keeps packet loss or publication failure from changing message,
run, tool, or session outcomes.

Transient SDK reconnect preserves the same room, tracks, and microphone. A terminal disconnect
uses `voice_session_refresh_token` and rejoins the same session; it never creates a replacement
session. An ambiguous mutation keeps the opaque identity because forgetting it could duplicate a
live durable call.

Only Boss chats expose this first control. Multi-agent active-identity switching and distinct voice
profiles remain VA-501 through VA-505. Group or conference communication remains future work.

## Verification

```text
backend voice and worker selection: 464 passed, 2 skipped
frontend focused voice/chat selection: 47 passed
frontend full suite: 1064 passed
frontend typecheck: passed
frontend changed-file ESLint: passed
backend changed-file Ruff: passed
i18n extract and validation: passed with the repository's existing unused-key report
```

## Next

VA-208 must exercise the real durable vertical slice: five voice turns, five durable human messages,
at most five runs, page reconnect on the same session, and the same history rendered in ordinary
chat after call end.
