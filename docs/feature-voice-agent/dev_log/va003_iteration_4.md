# Iteration 4 — bounded chunks and verified PCM layout

Integrated from `PER-6-va-003-build-and-benchmark-incremental-pcm-tts-with-two-voices`.

## Goal

Address QA's two blocking VA-003 findings: prevent an oversized provider transport chunk
from reaching the consumer intact and stop calculating audio duration from an unvalidated
24 kHz assumption.

## What was tried

The stream now splits provider output at 9600 bytes, which is 200 ms of the pinned 24 kHz
mono s16le layout and equals the RoomIO playout queue budget. It does not aggregate smaller
provider chunks, so first-byte measurement remains incremental. Cancellation clears any
locally pending bytes while closing the upstream response.

PCM layout validation now combines three provider facts: OpenRouter documents the endpoint
as OpenAI Audio Speech compatible, OpenAI defines its raw PCM output as 24 kHz signed 16-bit
little-endian, and Kokoro's reference implementation emits 24 kHz mono two-byte PCM. The
adapter rejects contradictory `Content-Type` parameters for rate, channels, width, encoding,
sign, or byte order before returning a stream. The benchmark records this validation source,
the declared response fields, the 9600-byte maximum, and the largest chunk observed per run.

## What broke

An attempted six-character synthetic `response_format="wav"` control request returned HTTP
400 because OpenRouter's current speech endpoint accepts only MP3 or PCM. No raw audio was
produced or retained. Layout verification therefore uses the OpenAI-compatible endpoint
contract and the selected Kokoro model's reference implementation rather than claiming a WAV
header measurement that the provider cannot supply.

## Decisions and reasons

The adapter cap uses the already pinned 200 ms RoomIO queue budget instead of an arbitrary
network buffer size. This bounds the downstream handoff and cancellation surface without
turning the provider adapter into the RTC framing layer owned by VA-302.

Missing layout parameters remain valid because the provider's documented compatibility and
model implementation establish the layout. Declared parameters are additional assertions,
not optional overrides: a response that says 16 kHz, stereo, 24-bit, big-endian, unsigned, or
a different encoding fails closed before duration can be reported.
