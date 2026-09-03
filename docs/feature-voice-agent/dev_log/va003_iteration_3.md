# Iteration 3 — incremental OpenRouter PCM TTS

Integrated from `PER-6-va-003-build-and-benchmark-incremental-pcm-tts-with-two-voices`.

## Goal

Implement VA-003 against the approved version 1.0.0 voice contract and measure first-byte
latency, incremental chunk delivery, completion, cancellation, audio duration, errors,
usage, and cost for distinct Boss and Sidra candidates.

## What was tried

The adapter uses the existing shared OpenRouter attribution headers and `httpx` streaming
response API. It validates `audio/pcm` and `X-Generation-Id` before exposing the stream,
reassembles provider chunks split inside a 16-bit sample, yields every usable chunk, and
tracks only byte counts and generation metadata. Offline tests use a streaming transport
that proves the second response chunk has not been read when the first reaches the caller.

The benchmark runs `am_adam` for Boss and `af_heart` for Sidra on
`hexgrad/kokoro-82m`. Five full trials and one post-first-chunk cancellation per candidate
stay below one tenth of one cent at the catalogue price.

## What broke

The first benchmark runner polled `/generation` immediately after every response. Cancelled
generations stayed absent, so bounded retries accumulated between requests and the first full
run ended before writing an artifact. Usage lookup was moved after all measured audio work;
it now retries the remaining generation IDs in bounded one-second rounds. This also prevents
usage lookup latency from contaminating the measured TTS completion time.

The provider returned no generation records for the two deliberately cancelled streams
within five seconds. The artifact records those two missing rows explicitly and computes a
conservative maximum cost from all submitted characters instead of treating missing usage as
free.

## Decisions and reasons

The adapter exposes raw, sample-aligned PCM chunks rather than RTC-sized frames. HTTP chunk
boundaries are provider transport details; VA-302 owns bounded RTC frame queues and must not
force the provider adapter to wait for a 50 ms playout frame before reporting first byte.

The adapter requires a generation ID and rejects non-PCM content before any audio reaches a
consumer. These are the only safe handles for content-free tracing, usage, and cost; accepting
an uncorrelated byte stream would make the acceptance evidence unauditable.

The benchmark stores no prompt text or audio. A fixed synthetic sentence lives in the script,
while the JSON contains only its character count, PCM byte counts, durations, timings, errors,
and generation metadata. The two voices are provisional candidates, not durable agent-profile
truth.
