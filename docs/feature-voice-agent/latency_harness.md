# Voice latency trace and synthetic-call harness

Schema version: **1.0.0** (`latency_trace.VOICE_LATENCY_SCHEMA_VERSION`)
Voice contract version: **1.2.0** (`voice_contracts.VOICE_CONTRACT_VERSION`)
Task: VA-004 — define the common latency trace and synthetic-call harness
Architecture source: commit `814c59a4cd12401100bf6e2e6908acf7bf83aad4`

Executable sources:

* `voice_agent/pipeline/latency_trace.py` owns the trace records, validation,
  metric and summary derivation, JSON serialization, and Draft 2020-12 result schema.
* `flexus_backend/experiments/voice_synthetic_call.py` produces deterministic offline
  call traces and aggregate evidence from the same contract.
* `docs/feature-voice-agent/benchmarks/synthetic_latency_v1.json` is the reproducible
  baseline artifact.

No runtime service imports the harness. It calls no provider, does not start LiveKit, and
does not activate voice behavior. Later STT, TTS, local media, interruption, and load-test
lanes consume this schema instead of inventing lane-specific JSON.

## Result contract

`voice_latency_result_schema()` returns the machine-readable JSON Schema. The root object
has exactly six fields:

| Field | Meaning |
| --- | --- |
| `vlat_schema_version` | serialization and metric-shape version |
| `vlat_contract_version` | PCM, cancellation, provider, and ID contract version |
| `vlat_run` | benchmark identity, source, and whether real provider calls ran |
| `vlat_config` | scenario, concurrency, warm/cold state, TURN use, provider/model/voice, and synthetic/real timing classification |
| `vlat_summary` | percentiles, errors, cancellations, usage, cost, and capacity snapshot |
| `vlat_traces` | one correlated event trace per utterance/turn/run/speech |

The result schema and voice contract version independently. A field or event addition is
a schema minor change; changing a field meaning, unit, or percentile algorithm is a schema
major change. Changing a PCM, cancellation, endpoint, or shared correlation contract bumps
`VOICE_CONTRACT_VERSION`. Every artifact carries both versions.

## Correlation

Every trace identity has the same machine-readable keys. IDs that do not exist at the last
reached stage are `null`, never empty strings or placeholders. Requirements advance with
the causal timeline:

| Reached point | Non-null identity fields |
| --- | --- |
| every result, including capacity rejection | `vsession_id`, `vleg_seq`, `conversation_id`, `agent_id`, `workspace_id` |
| any room event | `voice_utterance_id`, `turn_id`, `livekit_room_sid` |
| `run.queued` | `run_id` |
| `run.first_text_delta` or `cancellation.requested` | `speech_id` |

`run_id` was added to `VOICE_CORRELATION_ID_FIELDS` in voice contract 1.1.0. It connects
the finalized voice utterance to the existing Flexus executor context rather than treating
the audio pipeline as a second run system.

Every phrase/TTS/playout event also carries `segment_index` and
`provider_generation_id`. A trace rejects gaps in segment indexes, a changed provider
generation within one segment, duplicate event kinds for one segment, negative/non-finite
times, and out-of-order events.

## Event timeline

Times are non-negative milliseconds from a benchmark-local monotonic origin. They are not
wall-clock timestamps and cannot reveal when a customer spoke.

| Stage | Events |
| --- | --- |
| utterance | `utterance.started`, `utterance.speech_end` |
| STT | `stt.started`, `stt.final` |
| Flexus run | `run.queued`, `run.started`, `run.first_text_delta`, `run.completed` |
| phrase | `phrase.ready` |
| TTS | `tts.started`, `tts.first_byte`, `tts.completed` |
| playout | `playout.started`, `playout.completed` |
| cancellation | `cancellation.requested`, `cancellation.publish_stopped`, `cancellation.provider_closed`, `cancellation.stale_queue_empty` |

TTS completion and playout overlap by design. `playout.started` may occur before
`tts.completed` because PCM is consumed incrementally. Validation checks the causal pairs
instead of imposing a serialized stage list.

A successful trace requires the complete utterance/STT/run path and at least one complete
phrase/TTS/playout segment. An errored trace is a causal prefix: every observed event
requires its preceding event, but downstream completion events and phrase metrics are not
fabricated. An STT timeout can therefore end at `stt.started`, a run failure at the last
run event, a phrase failure after the first text delta, and TTS or playout failures inside
one segment. Cancellation failure can end at `cancellation.requested`. A concurrency
admission failure has no utterance events and requires admitted sessions to be lower than
requested sessions. `run.completed` requires and cannot precede `run.first_text_delta`, so
a complete executor run cannot be recorded before its first correlated text output.

The common per-turn metrics are:

```text
utterance_duration_ms
speech_end_to_stt_final_ms
stt_final_to_run_start_ms
stt_final_to_first_text_delta_ms
run_duration_ms
speech_end_to_first_playout_ms
speech_end_to_playout_complete_ms
cancellation_to_publish_stopped_ms
cancellation_to_provider_closed_ms
cancellation_to_stale_queue_empty_ms
```

Each phrase also reports ready-to-first-byte, first-byte-to-playout, total TTS, and total
playout timing. Aggregate metrics contain sample count, minimum, p50, p95, p99, and maximum.
An errored partial trace contributes only to metrics whose endpoints exist, so sample counts
make missing downstream measurements visible.

Per-trace and aggregate metric maps accept only the names declared above. Known metrics may
be absent when their endpoints were not observed; unknown or misspelled names are schema
errors.

## Errors, usage, cost, and privacy

Errors contain only a stage, stable code, and retryable flag. They intentionally have no
message or raw provider body. Usage separates STT audio seconds, TTS input characters,
generated audio seconds, played audio seconds, and provider request count. Cost is USD and
is explicit even when zero.

The schema has no field for transcript text, names, raw audio, request bodies, secrets, or
device identity. Audio stays represented by duration and byte-independent timing only.
Provider-backed lanes must remain explicitly marked and cost-bounded; the default harness
is offline and free.

## Concurrency snapshot

Every trace and aggregate records requested/admitted/active sessions, active turns, worker
replicas, worker session capacity, STT/TTS in-flight counts and limits, and TURN use. The
harness refuses more than 100 sessions, an unbounded turn count, non-positive provider
limits, or a scenario whose worker capacity cannot admit every requested call.

The aggregate concurrency object holds the peak value observed across its traces for each
numeric field and whether any trace used TURN. The public result validator validates every
nested field against the Draft 2020-12 schema, reconstructs each trace, recomputes its
derivable metrics, and recomputes the complete summary. Submitted counts, percentiles,
usage, cost, errors, cancellations, or concurrency values cannot disagree with the traces.

This snapshot does not claim that synthetic sleeps determine production capacity. VA-106
adds measured in-process actor/control-path concurrency without claiming rooms, providers,
or pod capacity. VA-603 replaces those boundaries with real rooms, provider limits,
resource measurements, and worker capacity while keeping the fields stable.

## Reproduce the baseline

From the repository root with the project virtualenv on `PYTHONPATH`:

```bash
python -m flexus_backend.experiments.voice_synthetic_call \
  --output docs/feature-voice-agent/benchmarks/synthetic_latency_v1.json \
  --scenario baseline \
  --sessions 10 \
  --turns-per-session 1 \
  --worker-replicas 1 \
  --worker-session-capacity 10 \
  --stt-limit 10 \
  --tts-limit 10 \
  --seed 7 \
  --jitter 0.15 \
  --cancel-every 4 \
  --error-every 0
```

The artifact-reproducibility test runs the same inputs and requires byte-equivalent JSON
after parsing.

## Synthetic baseline evidence

The baseline has ten admitted concurrent calls, ten successful traces, two deterministic
`barge_in` cancellations, zero errors, twenty synthetic provider requests, 7.714157 seconds
of synthetic STT usage, 9 seconds of generated synthetic speech, and **USD 0** cost.

| Metric | p50 | p95 | p99 |
| --- | ---: | ---: | ---: |
| speech end to final STT | 668.519 ms | 713.552 ms | 714.947 ms |
| final STT to first Flexus text | 359.226 ms | 394.572 ms | 398.068 ms |
| speech end to first playout | 1,346.338 ms | 1,385.229 ms | 1,386.709 ms |
| cancellation to publish stopped | 40 ms | 40 ms | 40 ms |
| cancellation to stale queue empty | 70 ms | 70 ms | 70 ms |
| cancellation to provider closed | 120 ms | 120 ms | 120 ms |

These values verify arithmetic, correlation, cancellation accounting, percentile shape,
and deterministic concurrency output. They are not claims about OpenRouter, the existing
Flexus executor, WebRTC, LiveKit, or host capacity. Real provider and local-media artifacts
must set `vprovider_calls_executed` and `vprovider_timings_are_synthetic` truthfully.

## VA-106 offline actor baseline

`flexus_backend/experiments/voice_echo_concurrency.py` runs the warm actor and echo pipeline
control path with deterministic in-process STT, TTS, and sink seams. The committed
`benchmarks/echo_concurrency_v1.json` covers 1, 5, 10, and 20 simultaneous calls. At every
level the STT and TTS barriers reached the requested concurrency, exactly one call committed
barge-in, and every deliberate foreign identity, foreign PCM marker, and stale
post-cancellation frame was rejected. The 20-call run accepted 39 legitimate frames,
rejected 20 foreign-audio attempts, 20 foreign-identity attempts, and one stale frame, with
zero harness errors. Its p95 whole-session host execution time was 45.383046 ms; this is a
local regression diagnostic, not end-user latency.

The artifact labels provider timings synthetic, provider calls false, LiveKit RTC false,
TURN false, and both raw and safe sessions-per-pod null. Its cancellation timing uses the
in-process trace clock and is not physical speaker playout. Reproduce it with:

```bash
python -m flexus_backend.experiments.voice_echo_concurrency \
  --output docs/feature-voice-agent/benchmarks/echo_concurrency_v1.json
```

## Rollout and rollback

Rollout: none. The schema module and experiment are not wired into a runtime entry point,
image, database, API, or feature flag. The only shared contract change is the 1.1.0
addition of `run_id`. `jsonschema==4.26.0` is a direct base dependency because result
validation is a production-importable service contract; it was already present transitively
in the CI lock and is now declared explicitly.

Rollback: revert the VA-004 implementation commits and the direct `jsonschema` declaration.
No migration, stored data, runtime configuration, provider resource, or customer audio
needs cleanup.
