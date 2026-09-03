# VA-204 iteration 10 — exactly-once finalized transcript

## Goal

Persist each accepted final human transcript once as the ordinary user message that the existing
conversation and agent runtime already understand.

## What was tried

The actor now calls an SDK-free transcript committer after final nonblank STT and current-turn
ownership, before TTS. The transaction re-locks the accepted active voice session, participant,
actor epoch, active leg, enabled agent, workspace, group, owner, and conversation. It writes a
normal `user` message with bounded voice provenance and returns its conversation and message
sequence for VA-205.

The conversation message itself is the deduplication record. A partial unique expression index
on `msg_provenance` enforces `(vsession_id, voice_utterance_id)` for `source=voice`; no fourth
voice table was added. The session lock serializes retries before sequence allocation, and replay
returns the first message without another sequence or audit.

## What broke

Final review proposed enqueueing the persisted message through ARQ so Boss would answer
immediately. That would violate the next milestone: VA-205 must call the existing executor
directly from the warm actor and must not put realtime turns on the shared ARQ queue. VA-204
therefore returns the committed reference but starts no run. Conversation-event reconciliation
for the call UI remains VA-207.

## Decisions and why

The message uses role `user`, the initiating human as author, `{"text": ...}` content, normal
questionnaire metadata, and provenance containing source, session, leg, utterance, room SID, STT
provider/model, and initiating human. Audio, tokens, credentials, and provider payloads are not
stored.

An empty or failed STT result, an obsolete turn, a lost lease, a stale actor epoch, a changed leg,
or an inactive session cannot persist. Once the transaction commits, later TTS interruption does
not erase the human message. VA-205 must consume the returned message reference and never insert
another user message.

## Verification

Focused unit coverage proves message shape, replay, invalid input rejection, the post-STT and
pre-TTS commit point, empty-transcript suppression, and worker authority arguments. A disposable
PostgreSQL test concurrently retries one utterance and proves one message, one audit, one sequence,
and database rejection of duplicate provenance.

```text
combined backend voice selection: 443 passed, 2 skipped
PostgreSQL concurrency e2e:          1 passed
guardrails and focused Ruff:         passed
commit gate:                         9 passed
```

## Next

VA-205 bridges the warm actor to `run_executor.execute_run` using this committed message.
