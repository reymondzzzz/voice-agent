# VA-304 iteration 18: durable playout cutoff

## Goal

Persist an honest generated-versus-played outcome for every executor-backed assistant speech
without duplicating assistant content or allowing a stale actor to mutate it.

## What was tried

The executor exposes the exact sequence of its last persisted assistant row in a successful
completion. The safe-speech plan carries that sequence into the voice pipeline. Before opening TTS,
the call runtime creates one deterministic speech record through a lease- and epoch-fenced database
operation. The pipeline retains only aggregate per-segment duration and completion state, then emits
one terminal outcome.

## What broke

The first design considered selecting the latest assistant row or matching spoken text. Both are
ambiguous when messages repeat, Markdown is removed, tools add rows, or another writer advances the
conversation. Audio duration also cannot identify a partial-word character boundary because the
provider returns no word or phoneme alignment.

Adding outcome accounting pushed the pipeline over its production file and function limits. Its
protocols and state records were extracted into a dedicated type module, and plan resolution plus
speech-begin accounting were separated into bounded functions.

## What was decided and why

Assistant identity comes only from rows persisted by the executor during that run. SQL recency and
content matching are forbidden. A speech begins as `PLAYING` only after the session room,
participant, actor epoch, active leg, conversation, and exact non-archived assistant row pass under
lock. Its ID remains `<vsession_id>.<vleg_seq>.<vturn_seq>.speech`.

The terminal states are `COMPLETED`, `INTERRUPTED`, and `FAILED`. Generated duration is provider PCM
received; played duration means PCM accepted by the self-hosted LiveKit audio source, not a remote
speaker acknowledgement. The text boundary advances only across fully completed spoken segments.
An interrupted partial segment therefore retains the prior safe boundary instead of estimating a
word from milliseconds.

Start and terminal retries with identical values are idempotent and write no second audit row.
Immutable or terminal conflicts fail closed. There are exactly two database mutations per speech,
never one per frame. Fixed confirmation copy is intentionally excluded because it has no matching
executor-owned assistant message row.

## Verification

Unit coverage proves exact assistant-sequence propagation, session and actor fencing, immutable and
terminal idempotency, completed/interrupted/failed outcomes, conservative multi-segment cutoff,
begin-before-audio ordering, callback failure, false-interruption continuity, and worker wiring.

```text
VA-304 focused runtime and persistence selection: 126 passed
VA-304 full voice selection:                    575 passed, 2 skipped
VA-304 guardrails:                              clean, 41 rules
```

The mandatory repository gate is recorded by the checkpoint commit.

## Next

VA-305 sweeps interruption thresholds against the echo, noise, cough, keyboard, backchannel, and
genuine-interruption corpus and records the selected supported-client values.
