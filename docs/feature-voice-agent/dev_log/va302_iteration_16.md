# VA-302 iteration 16: bounded streaming queues

## Goal

Make every text, TTS-request, PCM, and RTC boundary finite, ordered, cancellable, and observable
without retaining conversation content.

## What was tried

The SDK-free pipeline received one reusable owner-fenced FIFO primitive with injected time,
strict sequence numbers, item and age ceilings, cancellation flushes, and numeric snapshots. The
speech path applies it separately to text, TTS requests, and framed PCM. The LiveKit sink keeps its
SDK-specific source accounting and maps it into the same pressure shape. OpenRouter retains only a
bounded pending PCM window and enforces a total-response ceiling.

## What broke

The first design still allowed an arbitrary raw provider chunk to enter memory before the adapter
split it. It also allowed a timed-out LiveKit capture to finish after cancellation and become
indistinguishable from replacement speech. Both were fail-open memory or ordering hazards.
Independent review then found that pressure rejection left already-buffered RTC audio live and
that capture timeouts were mislabeled as queue expiry. The wider voice suite also exposed a stale
generic-worker test that still inspected actor metadata before signed job metadata was extracted.

## What was decided and why

Text is limited to four segments for 30 seconds, TTS admission to one request for one second, PCM
to four 50 ms frames for 200 ms, and RTC to four frames for 200 ms. A raw provider chunk is limited
to one second and total provider PCM to the existing 60-second stream deadline. These values keep
the first implementation aligned with the pinned RoomIO queue while allowing one bounded transport
burst.

Every queue binds to both turn and speech ownership. Cancellation invalidates ownership before
awaiting a possibly blocked source, flushes retained items, and rejects late completions. A LiveKit
capture exceeding 200 ms clears the source and permanently marks that sink unhealthy, so the call
resource must be recreated before later speech.

Queue pressure remains a separate content-free operational record. The latency artifact is already
a strict versioned causal schema, and queue health does not require changing its meaning. Reports
contain opaque correlation identifiers plus numeric capacity, age, depth, high-water, accepted,
dequeued, stale, expired, overflow, flushed, and unhealthy values only.

## Verification

Focused queue, provider, pipeline, and LiveKit sink tests cover ordered playout, all limits, expiry,
overflow, stale ownership, cancellation propagation, blocked capture, permanent fencing, and
privacy-safe metrics.

```text
VA-302 focused backend selection: 114 passed
VA-302 full voice selection:     492 passed, 2 skipped
VA-302 focused Ruff and format:  passed
VA-302 guardrails:               clean, 41 rules
```

The mandatory commit gate is recorded by the checkpoint commit.

## Next

VA-303 can add tentative pause and false-interruption resume after VA-301 records real supported
client acoustic evidence. Its resumable tail must reuse these ownership and pressure contracts.
