# Iteration 1 — VA-001: pin the prototype contracts and dependency versions

Milestone M0. Architecture source: commit `814c59a4cd12401100bf6e2e6908acf7bf83aad4`.

## Goal

Write the values every later voice lane depends on — LiveKit server/SDK versions,
OpenRouter endpoints, PCM format, sample rates, cancellation, identifiers, environment
variables — as one versioned contract, and prove the repository contains no managed
LiveKit endpoint. No runtime behavior.

## What was decided and why

**The contract lives in code, not only in prose.**
`voice_agent/pipeline/voice_contracts.py` holds the values; the document
`docs/feature-voice-agent/prototype_contracts.md` explains them. A Markdown-only contract
drifts silently. The module imports nothing from LiveKit, so it stays importable in CI
where the SDKs are not installed.

**Dependencies go in a `setup.py` extra that the CI lock does not compile.**
`requirements-ci.lock` is produced by `uv pip compile ... --extra test --extra services
--extra service_web --extra psystem --extra doc_convert --extra coco`. The new `voice`
extra is absent from that list, so CI and the production images never install the realtime
media stack. A test asserts the absence, so someone adding `--extra voice` has to do it
knowingly.

**The no-managed-LiveKit rule is a scan, not a promise.**
`require_self_hosted_livekit_url` rejects the host at admission, and
`flexus_backend/tests/_project/test_no_livekit_cloud_endpoints.py` scans the git index for
the host across all tracked non-`docs/` files. The scan plants a synthetic offender in a
temporary repository on every run so it cannot become vacuous as the allowlist changes.

**Sample rates came from the SDK, not from a guess.**
`livekit-agents==1.7.1` was downloaded and read: `voice/room_io/types.py` defaults both
`AudioInputOptions` and `AudioOutputOptions` to 24000 Hz mono, `room_io/_output.py`
publishes `sample_rate // 20` samples per frame (50 ms), and `rtc.AudioSource` defaults to
a 1000 ms queue. (Iteration 2 supersedes that last value: RoomIO overrides the default and
builds the source with 200 ms.) The contract pins 24000 Hz mono `pcm_s16le` in the room, 16000 Hz for the
STT upload, and 50 ms RTC frames, all matching the framework rather than fighting it.

## What broke the plan's assumptions

**OpenRouter ignores the STT `prompt` field.** The plan proposed optional prompt
vocabulary for Flexus, Boss, Sidra, agent and project names. The provider documents the
field as "accepted but ignored". `OPENROUTER_STT_PROMPT_VOCABULARY_SUPPORTED = False`
records this. VA-002 has to measure named-entity accuracy without vocabulary biasing, and
that lever only returns with local STT in M7.

**OpenRouter does not document its PCM sample rate, bit depth, or channel count.** The
TTS guide describes `pcm` as "uncompressed raw audio" and stops there. The contract states
what the actor requires and makes verification VA-003's job, with the resample living in
the provider adapter rather than the session actor.

**The `livekit-client` npm pin is recorded but not installed.** Adding it to
`package.json` would churn `pnpm-lock.yaml` for a package nothing imports yet. VA-103
installs `livekit-client@2.22.1` and co-stages the lockfile.

## Deliberately not done

Compose wiring for `livekit-server` (VA-101), token minting (VA-102), agent voice profiles
and the Boss/Sidra voice IDs (VA-501), the benchmark corpus and result schema (VA-002,
VA-003, VA-004), and any Prisma schema (VA-201). Provisional model defaults are recorded
as configuration; picking the production model is the M0 exit review's decision, not this
task's.

The reference material in the source worktree's `flexus_backend/experiments/voice_streaming/`
was read as background only. Its co-located tests and Markdown do not satisfy AGENTS.md
placement rules and none of it was copied.

## Verification

```text
python -m pytest tests/pipeline/test_voice_contracts.py \
  flexus_backend/tests/_project/test_no_livekit_cloud_endpoints.py -q
```
