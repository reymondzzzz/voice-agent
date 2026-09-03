# VA-303 iteration 17: two-phase interruption

## Goal

Pause agent speech quickly when the user may be speaking, commit genuine interruptions safely,
and recover from coughs, clicks, or empty recognition without creating a phantom turn.

## What was tried

The actor keeps the existing turn, speech, generation, and bounded queues during an interruption
candidate. A Flexus-owned playout gate stops additional PCM before it reaches LiveKit. The LiveKit
sink independently clears already queued source audio and fences writes until the exact owner is
resumed. Candidates that reach the commit threshold are transcribed once and reuse that final
event as the replacement turn input.

## What broke

LiveKit's audio source has queue clearing but no native pause-and-resume ownership contract, so a
transport-only pause could not prevent producer frames from refilling it. The replacement-turn
tests also exposed a trace-ordering error when an agent run completed without any safe speech: the
error was recorded after `run.completed`, contrary to the latency schema.

## What was decided and why

Probable speech pauses at 100 ms. A candidate must contain at least 250 ms of contiguous speech
before classification. Candidate STT has a three-second deadline. A nonblank transcript commits
the interruption; a shorter candidate, blank transcript, STT failure, or timeout resumes. These
are initial conservative values for VA-305 to tune, not universal acoustic claims.

Pause ownership lives above and inside the sink. The pipeline gate applies backpressure to queued
PCM; the sink clears transport buffering, rejects writes while paused, and accepts resume only for
the same turn, speech, and generation. Terminal cancellation still invalidates the owner first, so
no stale resume can make obsolete audio valid again.

An interruption transcript is never inserted speculatively. Only the committed replacement turn
passes it to the existing durable commit path, exactly once. Confirmation-prompt speech can pause
and resume, but interruption never approves or rejects a confirmation. Tool-operation cancellation
policy remains outside this task.

## Verification

Focused pipeline and LiveKit sink tests cover probable pause, short separated noise, empty STT
resume, committed interruption while thinking and speaking, bounded queue flush, later-segment
cancellation, provider-cancel failure, stale ownership, and close races.

```text
VA-303 focused pipeline and worker selection: 94 passed
```

The mandatory repository gate is recorded by the checkpoint commit.

## Next

VA-304 persists generated-versus-played duration and the best-known text cutoff for completed and
interrupted speech.
