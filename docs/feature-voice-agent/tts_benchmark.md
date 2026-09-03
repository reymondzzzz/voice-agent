# Incremental PCM TTS benchmark

Task: VA-003 — build and benchmark incremental PCM TTS with two voices

Contract: `voice_contracts.VOICE_CONTRACT_VERSION == "1.0.0"`

Machine-readable evidence: `benchmarks/tts_openrouter_20260831.json`

## Result

OpenRouter's `hexgrad/kokoro-82m` endpoint returned incremental raw PCM successfully for
both provisional agent voices. All ten complete requests yielded a first usable PCM chunk
before stream completion, each response arrived in 55–116 bounded chunks, and neither path waited
for or decoded a complete MP3. Both explicit cancellation probes stopped local consumption
after the first chunk and closed the provider stream in less than 9.2 ms.

The endpoint and response handling follow OpenRouter's official
[text-to-speech guide](https://openrouter.ai/docs/guides/overview/multimodal/tts):
`POST /api/v1/audio/speech`, `response_format="pcm"`, raw `audio/pcm` bytes, and
`X-Generation-Id` for usage lookup. The candidate IDs come from the live
[speech-model catalogue](https://openrouter.ai/api/v1/models?output_modalities=speech).

## Candidates and measurements

The run used five complete sequential requests per candidate plus one cancellation request
per candidate. Every request carried the same 116-character synthetic sentence. No customer
text or audio was used, and no generated audio was written to disk.

| Candidate | Model voice | First byte p50 / p95 / p99 | Complete p50 / p95 / p99 | PCM duration | Chunk count p50 / p95 / p99 |
| --- | --- | ---: | ---: | ---: | ---: |
| Boss | `am_adam` | 1052.7 / 3350.2 / 3802.8 ms | 1659.2 / 4228.3 / 4738.0 ms | 7.525 s | 73.0 / 107.6 / 114.3 |
| Sidra | `af_heart` | 1054.1 / 1257.1 / 1285.3 ms | 1966.2 / 2045.3 / 2049.0 ms | 7.400 s | 60.0 / 100.0 / 107.2 |

There were zero synthesis or transport errors. Boss's `am_adam` candidate had the lower
first-byte median in this small run, while Sidra's `af_heart` candidate had the lower
first-byte tail. Both remain provisional rather than production selections. Voice-profile
persistence and deterministic fallback belong to VA-501, not this provider spike.

## Cancellation

| Candidate | First byte | Bytes consumed | Cancel to upstream closed | Completed |
| --- | ---: | ---: | ---: | --- |
| Boss / `am_adam` | 865.1 ms | 4,088 | 9.179 ms | no |
| Sidra / `af_heart` | 1815.4 ms | 1,384 | 5.406 ms | no |

Both are far inside the 1-second provider-close contract. The stream records
`cancel_reason="barge_in"`, closes the HTTP response, and yields no further PCM. Cancellation
does not imply any tool or agent-run cancellation.

## PCM interpretation

Every live response declared `audio/pcm;rate=24000;channels=1`, and the adapter validated
those fields before exposing the stream. OpenRouter documents the endpoint as compatible
with the [OpenAI Audio Speech
API](https://developers.openai.com/api/docs/guides/text-to-speech#supported-output-formats),
whose PCM contract is signed 16-bit little-endian; Kokoro's [reference
CLI](https://github.com/hexgrad/kokoro/blob/main/kokoro/__main__.py) independently emits
24 kHz mono two-byte PCM. Contradictory rate, channel, width, encoding, sign, or byte-order
metadata is rejected before duration can be reported.

With that validated layout, `am_adam` produced 361,200 bytes or 7.525 seconds and
`af_heart` produced 355,200 bytes or 7.400 seconds on every full trial. Every chunk was
sample-aligned and at or below the explicit 9,600-byte limit, including live runs that
reached the limit exactly. The limit is one 200 ms RoomIO queue budget; smaller chunks still
reach the consumer immediately.

## Usage and cost

The run made 12 requests and submitted 1,392 input characters. `GET /api/v1/generation`
returned provider and actual cost metadata for all ten completed streams: DeepInfra served
them for a reported total of **$0.0007192**. The two deliberately cancelled generations did
not materialize usage records during the bounded five-second lookup window. Applying the
catalogue price of $0.62 per million characters to all 12 requests gives a conservative
maximum estimate of **$0.00086304**.

The JSON preserves generation IDs, timings, counts, provider names, errors, usage, and cost.
It contains neither request text nor PCM bytes.

## Reproduction

Offline behavior and schema:

```text
.venv/bin/python -m pytest \
  tests/pipeline/test_openrouter_tts.py \
  tests/pipeline/test_voice_tts_benchmark.py -q
```

Explicitly metered two-voice smoke, cancelled after the first chunk:

```text
FLEXUS_PROVIDER_TESTS=1 .venv/bin/python -m pytest \
  tests/pipeline/test_openrouter_tts_provider.py \
  -m provider -q
```

Five-trial benchmark:

```text
PYTHONPATH=. .venv/bin/python scripts/benchmark_voice_tts.py \
  --trials 5 \
  --json docs/feature-voice-agent/benchmarks/tts_openrouter_20260831.json
```

Each provider command requires `OPENROUTER_API_KEY`. The runner caps trials at ten per voice,
caps each request at the 160-character segment contract, runs sequentially, estimates its
maximum spend, and never persists audio.

## Rollout and rollback

Rollout: none. The adapter is not imported by a server, worker, image entrypoint, GraphQL
handler, or frontend. The LiveKit dependencies remain isolated in the optional `voice`
extra, and provider calls require an explicit script or `FLEXUS_PROVIDER_TESTS=1`.

Rollback: revert the VA-003 adapter, tests, benchmark script, and evidence. There is no
migration, persistent state, configuration activation, raw-audio cleanup, or running service
to drain.
