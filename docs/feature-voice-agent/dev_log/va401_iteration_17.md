# VA-401 iteration 17: safe foreground-tool activity

## Goal

Show truthful foreground-tool activity during a live Boss turn without exposing tool data or
creating a second unordered speech path.

## What was tried

The existing LangGraph updates stream already yields completed AI tool-call messages and matching
tool result/error messages. Its normal run-activity projection was evaluated as the hook point. The
voice room already accepted `TOOL_ACTIVE`, although no runtime transition or localized UI label
used it.

## What broke

The ordinary run-activity payload is intentionally rich: it can contain model-provided titles,
display inputs, raw tool outputs, error summaries, tool names, call IDs, and node names. Forwarding
that payload, even after generic redaction, would violate the private voice-state contract. Rapid
`TOOL_ACTIVE` followed by `THINKING` could also be coalesced before the browser observed activity.
Final review found two additional fail-open edges: every non-protocol tool was initially treated as
foreground, and an observer exception could abort or mask the durable run it was only presenting.

## What was decided and why

The executor keeps opaque activity IDs private and emits only aggregate boolean transitions. Sets
make duplicate starts idempotent and keep parallel activity true until the last matching result or
error. Protocol tools never enter the aggregate, and executor exit clears unfinished activity. A
server-owned policy admits only known Boss inline reads and bounded presentation/wait tools.
Unknown, background, confirmation-gated, and mutating operations fail closed. Observer exceptions
log only their type and conversation ID and never change the executor outcome.

The direct voice runtime binds the callback to the exact owned turn. It changes only
`THINKING` to `TOOL_ACTIVE` and `TOOL_ACTIVE` back to `THINKING`; stale, cancelled, closed, or
unrelated-state callbacks do nothing. The bounded state publisher preserves the rapid activity
pair without changing the version-1 packet fields. The browser label is a fixed translation, not a
tool-derived string.

The first version intentionally uses zero spoken acknowledgements. That respects the at-most-one
contract and avoids delaying the executor or racing final response TTS. A future acknowledgement
requires a separate serialized, cancellable speech lane and safe server-owned label metadata.

## Verification

Focused executor, runtime, state-protocol, and frontend tests cover single, duplicate, parallel,
protocol-only, result/error, unfinished-exit, stale-turn, ordered packet, privacy allowlist,
localization, and accessible rendering behavior. Two independent final reviews confirmed that the
foreground policy fails closed and observer failures cannot change a durable run.

```text
VA-401 focused backend selection: 54 passed
VA-401 executor selection:        299 passed
VA-401 full voice selection:      537 passed, 2 skipped
VA-401 focused frontend:          15 passed
VA-401 full frontend:             1086 passed
VA-401 frontend quality/build:    passed
VA-401 focused Ruff and format:   passed
```

The mandatory commit gate is recorded by the checkpoint commit.

## Next

VA-402 can build confirmation prompt/resume on the existing signed interrupt rather than treating
an unresolved confirmation as completed foreground work. VA-403 still depends on VA-303 for
operation-versus-speech cancellation semantics.
