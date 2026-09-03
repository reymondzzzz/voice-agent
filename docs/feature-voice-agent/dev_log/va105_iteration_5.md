# Iteration 5 — VA-105 local microphone-to-echo media loop

Date: 2026-08-31. Branch: `voice-agent`.

## Goal

Connect the already isolated pieces into one local self-hosted call: browser microphone,
decoded PCM, local utterance detection, OpenRouter final STT, recognized text as the echo
response, incremental OpenRouter PCM TTS, and a published agent track in the same LiveKit
room. Record end-of-utterance, STT, TTS, and publication timing without claiming a Boss run.

This is the last single-call integration task before VA-106 interruption and concurrency
work. It deliberately excludes LangGraph, tools, durable messages, session schema, handoff,
and production deployment.

## What was tried

The first design kept the LiveKit SDK at the existing single import boundary and put all
turn behavior in an SDK-free module. The pinned `livekit-agents==1.7.1` and RTC APIs were
checked for raw input and output behavior before wiring:

- `rtc.AudioStream` can subscribe to one decoded remote track with an explicit finite
  capacity and can resample it to 16 kHz mono for the utterance STT contract;
- `rtc.AudioSource` accepts the measured 24 kHz mono TTS layout and applies awaited
  backpressure through `capture_frame`;
- the RoomIO path's 200 ms source queue remains the bound, while this prototype publishes
  exact 50 ms frames;
- publishing the output track and waiting for a subscriber belongs inside actor
  initialization, before the actor advertises `LISTENING`.

The first pipeline implementation proved the energy-VAD and echo flow in isolation, then
the trace schema and LiveKit boundary were integrated as separate lanes. A final browser
route connects the existing room client to `voice_prototype_session_start` so a local human
can exercise the path without pasting a token into source code.

## What broke

### The common latency trace assumed every successful voice turn had a Flexus run

The VA-004 trace required `run.queued`, `run.started`, `run.first_text_delta`,
`run.completed`, and `run_id`. VA-105 is specifically an echo task and does not call the
executor. Filling those fields would make a clean schema by recording events that never
happened.

The trace now has an explicit `echo` pipeline mode. Echo traces reject all run events and a
run ID, allow conversation, agent, and workspace IDs to remain null until durable bootstrap
exists, and require the real one-phrase STT -> TTS -> publication path. Existing agent traces
and committed mode-less benchmark artifacts remain backward compatible.

An early TTS-open failure exposed a second trace gap: there is no provider generation ID
before the response headers arrive. Echo mode now permits a causal TTS error after
`stt.final` with no invented phrase or generation event. Once phrase events exist, the
ordinary incomplete-segment requirements still apply.

### Arbitrary provider chunks are not LiveKit frames

OpenRouter may yield chunks smaller or larger than one room frame. Passing them directly to
RTC would make frame duration depend on network chunking. The pipeline now combines and
splits chunks into exact 2,400-byte, 50 ms, 24 kHz mono frames. A final aligned remainder is
silence-padded to the same frame size while usage records only its real audio duration.

### The worker could become ready before its output path existed

The actor skeleton originally transitioned to `LISTENING` after validating environment,
then connected to the room afterward. A microphone track could therefore arrive before the
agent track was published or subscribed. VA-105 moved connect, publish, and subscription
wait into the actor's existing bounded initializer. Input arriving during `CONNECTING` is
not admitted as an utterance.

### The worker environment gate did not require the provider credential

`OPENROUTER_API_KEY` was already a required voice contract variable, but the worker gate
checked only required names beginning with `FLEXUS_VOICE_`. That was sufficient for the
actor skeleton and would fail later with a key lookup on its first real job. The gate now
checks every required voice variable and fails at worker startup.

### The first acceptance page confused generated and wire response shapes

The named GraphQL mutation returns a nested field on the wire, but the generated RTK
endpoint already transforms that response to the field payload. The first page version
tried to unwrap it a second time. The direct TypeScript build caught the mismatch before a
browser run; the page now consumes the generated endpoint's actual `{vlk_url,
vpart_token}` result. Full accessibility lint also caught that the programmatic live audio
element lacked a caption track, so the element now satisfies the repository's sealed media
accessibility contract.

## Decisions and why

### LiveKit remains one thin SDK boundary

`service_voice_agent.py` is still the only LiveKit SDK importer. It accepts one remote
microphone publication, opens an eight-frame 16 kHz mono `AudioStream`, and feeds bytes to
the pure pipeline. It publishes one 24 kHz mono track with microphone source metadata and
awaits the returned subscription. Call-local streams, STT client, pipeline, and audio source
close idempotently on shutdown.

Keeping VAD, provider orchestration, trace construction, and PCM reframing outside the SDK
file lets the normal test tier exercise the real control flow even though the media extra is
deliberately absent from CI.

### VA-105 uses a simple local energy VAD

The first VAD ignores leading silence, retains 150 ms of pre-roll, requires 250 ms of speech,
ends after 600 ms of silence, and caps the utterance at 30 seconds. These are prototype
values that prove the media contract; they are not claims about noisy-room quality. Browser
AEC, noise suppression, and gain control remain enabled. VA-303 owns two-phase barge-in,
false-interruption resume, and a real threshold corpus.

### One turn is active at a time

While STT or TTS is active, new input frames are not queued into another unbounded turn.
This makes VA-105 honest turn-taking rather than premature interruption logic. VA-106 adds
turn cancellation, stale `turn_id`/`speech_id` rejection, and the initial 1/5/10/20-call
baseline.

### Echo traces contain measurements, not content

Each finalized utterance logs opaque correlation IDs, event timestamps, stable provider
error codes, usage, and generated/published duration. It never logs transcript text, PCM,
request bodies, credentials, or raw provider errors. `playout.completed` means LiveKit
accepted the final frame into its source; client speaker completion is not observable yet.

### The measured Boss prototype voice remains temporary

VA-105 uses `am_adam`, the Boss candidate measured by VA-003, through the explicit
`VOICE_PROTOTYPE_ECHO_TTS_VOICE` contract. This is not a persisted agent voice profile.
Provider-independent Boss and Sidra profiles remain VA-501.

## Verification

The implementation is structured so the ordinary tier can prove the entire control flow
without media SDKs or provider billing:

- pipeline tests cover silence, pre-roll, minimum speech, 30-second bounds, empty STT,
  provider failures, exact echo text, exact 50 ms frames, cleanup, state recovery, and
  privacy-safe echo traces;
- trace tests cover echo success, partial failures, no fake run/authority identity, schema
  round-trip, and compatibility with the committed agent-mode benchmark artifact;
- fake-SDK boundary tests cover registration order, audio-only subscription, finite 16 kHz
  input, one microphone, 24 kHz output, subscription wait, backpressure, and idempotent
  cleanup;
- frontend tests cover mutation, room join/leave, autoplay recovery, safe errors, and
  unmount cleanup.

The assembled offline suite passed 402 backend voice tests with two provider-live tests
skipped, 18 frontend voice tests, the full GraphQL refresh plus TypeScript project build,
full frontend lint, the accessibility and design-token ratchets, and i18n
extraction/validation with no missing keys. The repository-wide Python
guardrail scanner reports only the pre-existing untracked
`flexus_backend/experiments/voice_streaming/` prototype; no VA-105 file adds a finding.

The actual self-hosted LiveKit plus OpenRouter smoke requires local Docker, the ignored
LiveKit credentials, and an OpenRouter credential. Its measured one-call result must be
recorded before claiming the M1 gate; automated tests do not manufacture that evidence.

## Rollout and rollback

The route is manually reachable and the backend mutation remains refused unless
`FLEXUS_VOICE_ENABLED=1`. No production image includes the worker or LiveKit SDK, no
deployment starts it, and no database or Redis record is added. Rollback is stopping the
worker and local media stack or reverting this iteration.

## Next iteration

VA-106 adds cancellation of active STT/TTS/publication, stale-frame rejection, bounded
queue-pressure evidence, and isolated 1/5/10/20-call measurements. Durable Boss execution
must still wait until the local loop and those concurrency results establish a plausible
latency and capacity envelope.
