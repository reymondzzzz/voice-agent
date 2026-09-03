# VA-305 iteration 19: interruption threshold corpus

## Goal

Select and lock the initial interruption thresholds with reproducible true/false outcomes and
pause/resume percentile evidence while preserving the explicit supported-client boundary.

## What was tried

The fixed VAD values were moved into a validated configuration injected into the real pipeline. A
deterministic policy corpus covers six genuine English/Russian interruptions and eleven negative
click, cough, keyboard, echo-leak, empty/error/timeout STT, and backchannel cases. The benchmark
sweeps 150, 250, 350, and 500 ms minimum speech and recomputes every summary from its raw rows.

## What broke

Thresholds alone cannot distinguish a recognized acknowledgement from a genuine correction. The
existing pipeline committed every nonblank transcript, so “uh-huh” and “угу” became destructive
interruptions. An exact, deliberately small multilingual backchannel set now resumes the same
speech; meaningful phrases such as “stop” or “да, остановись” still commit.

It was also easy to overstate latency. The pipeline pauses after probable speech, waits for 600 ms
of trailing silence, and may then wait for STT. The benchmark therefore separates onset-to-pause,
post-speech classification wait, and classification-to-resume sink acknowledgement. It never calls
the latter physical speaker latency.

## What was decided and why

The initial desktop Chrome pilot keeps energy 500, probable pause 100 ms, minimum commit evidence
250 ms, trailing silence 600 ms, and candidate STT deadline 3 seconds. Both 150 and 250 ms committed
all six genuine cases with zero false commits. The conservative tie-break selects 250 ms. At 350 ms
genuine recall falls to 50%, and at 500 ms it falls to 16.7%.

For the selected value, probable-pause p50/p95/p99 is 100/100/100 ms and resume sink-ack
p50/p95/p99 is 0/0/0 ms after classification. Ten of eleven negative cases pause temporarily by
design, then resume. Full post-speech recovery includes 600 ms trailing silence and, for classified
candidates, STT latency; its p95 is 2385 ms because the corpus includes the bounded timeout case.

The checked-in report contains no transcript, raw audio, or device label. It declares offline
deterministic execution, no provider calls, no physical audio, and no LiveKit RTC. The separate
VA-301 physical evidence still limits support to desktop Chrome with built-in speaker/microphone.
Chrome headphones, Safari, Firefox, iOS, and Android remain deferred rather than silently promoted.

## Verification

Tests prove exact backchannel classification, runtime configuration, deterministic report
reproduction, summary tamper detection, privacy-safe artifact shape, selected threshold invariants,
and the real pipeline's false-interruption resume path.

```text
VA-305 benchmark and pipeline selection: 48 passed
Selected true commit rate:              100%
Selected false commit rate:             0%
Pause p50/p95/p99:                      100/100/100 ms
Resume sink-ack p50/p95/p99:            0/0/0 ms after classification
```

The mandatory repository gate is recorded by the checkpoint commit.

## Stop

The requested VA-3xx scope ends here. Tool cancellation policy, handoff, load, and broader client
compatibility remain untouched.
