# Iteration 3 — VA-004: common latency trace and synthetic-call harness

Integrated from `PER-7-va-004-define-the-common-latency-trace-and-synthetic-call-harness`.

## Goal

Define one versioned JSON result that the STT, TTS, local media, interruption, and load-test
milestones can all emit. Prove the shape with a deterministic offline harness and a
reproducible benchmark artifact without calling providers or copying the source worktree's
non-compliant experiment layout.

## What was tried

The source architecture and its uncommitted `voice_streaming` experiment were read as
reference. The experiment demonstrated queueing and persistent-session scheduling, but it
had co-located tests and Markdown, docstrings/comments, p50/p95 without p99, no explicit
error/usage/cost contract, and no utterance-to-playout correlation schema. None of its
files were copied.

The replacement splits responsibilities:

* `services/voice/latency_trace.py` is the reusable contract and validator;
* `experiments/voice_synthetic_call.py` is the isolated offline producer;
* tests mirror those source paths under `flexus_backend/tests/`;
* the JSON evidence and design notes live under `docs/feature-voice-agent/`.

## What broke

The first validation model treated TTS completion as preceding playout start. That is
wrong for the intended incremental PCM path: playback begins after the first bytes while
the provider is still generating the tail. Validation now checks causal pairs
(`tts.started` -> `tts.first_byte` -> `tts.completed` and independently
`tts.first_byte` -> `playout.started` -> `playout.completed`) without serializing the two
streams.

The first narrow test run had 18 passes and one expected failure because the committed
baseline artifact had not been generated yet. Generating it with the public CLI made the
reproducibility test pass. The artifact is produced by product code rather than a separate
one-off generator, so the checked-in evidence cannot drift from the harness silently.

The Paperclip worktree had no local `.venv`. Verification used the existing repository
virtualenv from the main checkout with `PYTHONPATH` pointed at this branch; no environment
or dependency was mutated.

## Decisions and reasons

The schema uses event offsets rather than wall-clock timestamps. Offsets are sufficient
for latency math and do not disclose when a customer spoke.

`run_id` was added to the shared voice correlation fields and bumped the voice contract
from 1.0.0 to 1.1.0. `turn_id` identifies the voice turn, but it does not identify the
existing Flexus executor context; omitting `run_id` would leave the exact acceptance
boundary disconnected.

Phrase, TTS, and playout events share `segment_index` and `provider_generation_id`. A
single top-level TTS duration would lose ordering and stale-generation evidence as soon as
responses contain more than one phrase.

Errors use stable codes without messages. Trace events contain no extensible payload map.
Both choices prevent a later adapter from putting transcript text, raw provider bodies, or
audio into an observability artifact by convenience.

Percentiles use linear interpolation over sorted samples and always report p50, p95, and
p99 together with count/min/max. An error trace omits unavailable downstream metrics and
therefore reduces that metric's visible sample count instead of substituting zero.

## Verification

The narrow suite covers multi-phrase streaming overlap, cancellation without run
cancellation, invalid IDs/order/generations, privacy-safe schema fields, deterministic
output, bounds, nonzero synthetic errors, usage/cost/concurrency summaries, the CLI, and
artifact reproduction.

```text
python -m pytest \
  tests/pipeline/test_latency_trace.py \
  flexus_backend/tests/experiments/test_voice_synthetic_call.py -q
19 passed
```

Repository-gate results belong in the issue handoff after the implementation diff is final.
