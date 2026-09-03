# Iteration 5 — QA corrections to causal identity

Integrated from `PER-7-va-004-define-the-common-latency-trace-and-synthetic-call-harness`.

## Goal

Keep partial failure traces truthful after QA showed that the causal event prefix still
accepted an impossible run order and required identities from stages that never started.

## What was tried

The two review reproductions were run against the round-one correction. Moving
`run.completed` between `run.started` and `run.first_text_delta` passed typed validation.
An STT timeout with no executor run or assistant speech failed on an empty `run_id`, and a
pre-admission capacity rejection failed on an empty `voice_utterance_id`.

The event prerequisite now makes `run.first_text_delta` the direct predecessor of
`run.completed`. Optional pipeline identities remain present as stable JSON keys but use
`null` until their stage exists. Typed validation and Draft 2020-12 conditionals require
room/utterance/turn IDs for any event, `run_id` from `run.queued`, and `speech_id` from the
first text delta or cancellation request.

## What broke

The service module briefly exceeded the repository's 1,000-line production limit while
the conditional schema was added. Consolidating the conditional schema builder restored
the file below the hard ceiling without weakening the contract.

## Decisions and reasons

Empty strings remain invalid. They are indistinguishable from missing instrumentation and
encourage synthetic placeholder IDs. Explicit `null` preserves a stable result shape while
stating that a downstream identity did not exist.

The always-required fields identify the requested session context, leg, conversation,
agent, and workspace. Capacity rejection occurs before room admission, so it does not
require room, utterance, turn, run, or speech identities. Once events exist, requirements
advance with the causal stage and prevent a later measurement from losing its join key.

The schema remains version 1.0.0 because the initial contract has not landed and this is a
correction to its advertised relevant-subset semantics, not a compatibility change to a
released consumer.

## Verification

Regression coverage includes typed and serialized impossible run order, schema-level
stage identity requirements, STT failure without run/speech IDs, and pre-admission
rejection without room/utterance/turn/run/speech IDs. Final command results are recorded
in the issue handoff after the deterministic artifact and repository gates are rerun.
