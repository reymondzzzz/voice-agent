# Iteration 4 — QA corrections to the common latency contract

Integrated from `PER-7-va-004-define-the-common-latency-trace-and-synthetic-call-harness`.

## Goal

Make the version 1 result safe for later provider and concurrency milestones after QA
showed that early failures required fabricated completion events, nested summaries escaped
the public validator, and metric-name typos escaped the JSON Schema.

## What was tried

The three QA reproductions were run against the committed implementation. An STT timeout
failed before serialization because all run events and one phrase were unconditional. An
empty `vsummary_cost` passed `validate_voice_latency_result` even though the Draft 2020-12
schema rejected it. A misspelled per-trace metric passed the schema because the metric map
accepted arbitrary properties.

The validator now treats an error trace as a causal prefix. Present events require their
predecessors, while only successful traces require the complete base path and a complete
phrase. Stage checks distinguish utterance, STT, run, phrase, TTS, playout, cancellation,
and pre-utterance concurrency failures.

The result reader now runs the full Draft 2020-12 schema, reconstructs and validates every
trace, compares submitted metrics with metrics derived from events, and compares the entire
summary with one recomputed from the traces. The common summarizer moved from the experiment
into `services/voice/latency_trace.py` so producers and readers cannot drift.

## What broke

The original summary test had encoded empty nested objects as a valid result. Replacing it
with a summary derived from a real trace exposed the intended boundary and made malformed
nested objects negative cases instead of fixtures.

The schema package was available only through unrelated transitive dependencies. Because
the public validator now imports it directly, `jsonschema==4.26.0` became an explicit base
dependency and the CI lock was regenerated. Resolution kept the existing pinned version;
only its provenance changed to include `flexus-backend`.

## Decisions and reasons

Empty event arrays are valid only for a rejected concurrency admission. This preserves a
record for capacity failure without inventing an utterance that never began.

Metric properties are closed but optional. A known metric disappears when either endpoint
is absent; accepting arbitrary names would split dashboards and benchmark comparisons on a
spelling mistake.

Aggregate concurrency uses the peak numeric value across traces and reports whether any
trace used TURN. A first-trace snapshot would become order-dependent as soon as real load
tests record changing active and in-flight counts.

The schema remains version 1.0.0 because the first implementation has not landed and these
changes correct its advertised semantics before any downstream artifact consumes it.

## Verification

The focused regression suite covers partial failures at every advertised stage, full nested
schema validation, derived trace metrics, summary coherence, misspelled trace and summary
metrics, deterministic harness output, CLI serialization, and artifact reproduction. The
final command results are recorded in the issue handoff after the benchmark artifact and
repository gates are rerun.
