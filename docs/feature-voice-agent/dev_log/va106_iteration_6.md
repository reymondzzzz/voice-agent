# Iteration 6 — VA-106 cancellation and concurrency baseline

Date: 2026-08-31. Branch: `voice-agent`.

## Goal

Make stopping one echo speech invalidate queued output by turn and speech identity, close
provider work without fabricated completion, and establish the first 1/5/10/20-call
isolation baseline before durable voice-session schema work.

## What was tried

The echo pipeline kept one warm actor per call and added a speech-aware PCM sink contract:
`begin_speech`, sequenced `write`, and `cancel_speech`. Every frame carries the actor's
`VoiceTurnIds` plus a monotonic frame sequence. The LiveKit seam accepts only the active
identity and next sequence while serializing `capture_frame` and `clear_queue`.

The initial interruption policy deliberately stayed small. While the actor is SPEAKING,
five contiguous 50 ms speech frames commit barge-in. Silence resets the contiguous counter;
separated short bursts are discarded without creating a second turn. The accumulated new
utterance survives cancellation and can become the next turn. PCM arriving during
TRANSCRIBING is ignored because STT-phase interruption belongs to VA-303.

The concurrency harness runs the production actor and echo pipeline with deterministic,
content-free STT/TTS/sink seams. It creates 1, 5, 10, then 20 distinct warm calls and uses
barriers to prove the requested STT and TTS overlap. Every sink receives one deliberately
foreign turn identity and PCM marker; one call per level also receives a stale frame after
cancellation.

## What broke

The first cancellation order let the caller await provider cancellation before the actor
task owned its terminal state. A yielding provider could finish iteration and emit normal
`tts.completed` or `playout.completed` after cancellation started. Provider cancel and the
final trace now belong to the canceled turn task, and trace validation rejects normal speech
completion at or after `cancellation.requested`.

The first sink lock held `capture_frame` safely but let cancellation wait forever behind a
stalled capture. Cancellation now invalidates the active identity before waiting and bounds
the serialized queue clear by the 200 ms stop-publication deadline. If an old capture
returns after that deadline, the writer rejects it and clears its stale queue before a newer
speech can begin. Deadline failure is recorded as `cancellation:publish_stop_failed` rather
than pretending the queue was empty.

Session shutdown originally canceled only the actor task. It now uses the same sink and
provider cancellation path with reason `session_end`, producing one causal terminal trace.
Provider cancel failures and deadlines produce `cancellation:provider_cancel_failed`, omit
normal completion, and return the actor to LISTENING.

The first isolation counter only proved that each session accepted its own marker. The
harness now injects foreign identities and foreign audio into every session, including a
nonparticipant identity in the one-call case, and requires every attempt to be rejected.

## Decisions and why

VA-106 implements committed barge-in, not tentative pause and resume. The 250 ms contiguous
threshold matches the minimum-utterance contract and prevents two separated noise bursts
from canceling speech. VA-303 retains the richer two-phase policy, false-positive resume,
and the decision about input while STT is in flight.

Cancellation is a terminal speech outcome. A canceled trace may end with the cancellation
observations actually achieved or a stable cancellation-stage error. It may not fabricate
TTS or playout completion after the request.

The 1/5/10/20 result is an offline control-path and isolation baseline. It does not exercise
LiveKit RTC, TURN, the browser, OpenRouter, CPU saturation, memory pressure, or network
jitter, and therefore cannot establish sessions per pod. VA-603 owns real-room capacity and
soak evidence.

## Result

`benchmarks/echo_concurrency_v1.json` reports zero harness errors at every level. The 20-call
level reached 20 concurrent STT and TTS operations, accepted 39 legitimate PCM frames, and
rejected 20 foreign-audio attempts, 20 foreign-identity attempts, and one stale
post-cancellation frame. No frame was accepted after cancellation. Its p95 whole-session
host execution time was 45.383046 ms, recorded only as a local regression signal.

Focused cancellation, trace, LiveKit seam, and concurrency verification passed 80 tests;
the complete voice selection passed 428 with two credentialed provider tests skipped.
Real pinned-LiveKit and provider latency were not rerun because this worktree has no running
Docker engine, `.env.voice`, or OpenRouter credential. The artifact labels provider calls,
LiveKit RTC, TURN, and capacity derivation explicitly false or null.

## Next

VA-201 adds the durable voice session, leg, speech, and playout schema. VA-303 later replaces
the committed threshold with adaptive pause, cancel, and false-positive resume.
