# VA-002 — OpenRouter utterance STT adapter

Task: VA-002 — build and benchmark the OpenRouter utterance STT adapter
Contract version measured: **1.0.0** (`voice_contracts.VOICE_CONTRACT_VERSION`)
Code: `voice_agent/pipeline/voice_stt.py`,
`voice_agent/pipeline/voice_stt_bench.py`,
`voice_agent/pipeline/voice_stt_corpus.py`,
`scripts/voice_stt_benchmark.py`
Artifacts: `benchmarks/va002_stt_corpus.json`, `benchmarks/va002_openrouter_stt.json`,
`benchmarks/va002_openrouter_stt_language_hint.json`

Nothing here is wired into a runtime path. No GraphQL handler, worker, migration, image,
or lockfile changes. The benchmark script is the only code in the repository that spends
OpenRouter money, it runs only when a developer invokes it, and it stops on a
`--max-cost-usd` bound.

## The adapter

```python
provider = voice_stt.OpenRouterSttProvider(api_key)
config = voice_stt.SttConfig(
    sttc_model="openai/whisper-large-v3-turbo",
    sttc_language=None,
    sttc_sample_rate_hz=voice_contracts.VOICE_STT_SAMPLE_RATE_HZ,
    sttc_deadline_s=voice_contracts.VOICE_STT_REQUEST_DEADLINE_S,
)
async for event in provider.stream(utterance_pcm, config):
    ...
```

`stream()` takes one completed, VAD-bounded utterance as contract PCM
(`pcm_s16le`, mono, 16 kHz) and yields exactly one `TranscriptEvent` with
`stte_kind == "final"`. `STT_EVENT_KINDS` admits `"partial"` so a later streaming engine
can add partial transcripts without changing the session actor, but this adapter never
emits one: the documented endpoint accepts a complete base64 audio object and answers
once. Claiming partial transcription here would be a lie about the provider.

Each event carries the transcript, the audio duration the adapter measured from the PCM
it was handed, the request latency, the provider's own `usage` block, and the OpenRouter
`X-Generation-Id` response header as `stte_provider_generation_id` — the only request
identifier this endpoint returns.

Decisions worth knowing before building on it:

* **It does not retry.** The plan's failure matrix allows one bounded retry "when it fits
  the latency budget". The adapter cannot know that; only the session actor knows how much
  of the turn deadline is left after end-of-utterance detection. Retry policy belongs to
  the caller, and putting it here would hide a doubled deadline from the state machine.
* **Every failure is an `SttError` with a named `sterr_kind`** drawn from
  `STT_ERROR_KINDS`. Session logic never sees an `httpx`, `ValueError`, or `AttributeError`
  escape, so neither provider transport detail nor a surprising response shape can leak
  into voice state handling. `_error_kind_for_status` maps 401/403 to `auth`, 429 to
  `rate_limit`, other 4xx to `invalid_request`, and 5xx to `provider_error`; timeouts and
  transport failures get their own kinds.
* **A 200 with the wrong shape is `malformed_response`, not a crash.** The response is
  validated before it becomes an event: a non-JSON body, a missing `text` key, a `text`
  that is not a string, and a `usage` that is not an object each raise
  `malformed_response`. So does every `usage` figure that is not a number a meter could
  have produced — a JSON boolean, a negative, a `NaN`/`Infinity`, a fractional token count,
  or a magnitude outside `[0, USAGE_VALUE_MAX]` (`1e9`, far above any real transcription
  and low enough that a 400-digit JSON integer is refused rather than overflowing the
  conversion to `float`). Zero is valid. This is a money path as well as a metrics path: a negative `cost`
  would otherwise *reduce* the benchmark's recorded spend and loosen its budget. A `null`
  `text` stays an ordinary empty final event, because that is a transcript the provider
  genuinely returned.
* **An empty transcript is a successful `final` event with empty text, not an error.** The
  plan requires that empty or low-confidence results not create a user message; that is a
  session decision about an ordinary result, not a failure.
* **There is no confidence score.** Neither candidate model returns one, and the endpoint
  has no field for it. Any "low-confidence" rule in VA-105/VA-204 has to be built on text
  emptiness and length until a provider exposes confidence, or until local STT lands in M7.
* **Audio is validated before the request is sent**, so a capture bug costs an
  `invalid_audio` error rather than a provider charge: over the 30 s utterance bound,
  empty, or a byte count that is not a whole number of 16-bit mono frames.
* **The API key is never logged.** Log lines carry model, generation id, audio seconds,
  request ms, transcript *length*, and cost — never the transcript body, never the audio,
  never the request payload.

`OPENROUTER_STT_PROMPT_VOCABULARY_SUPPORTED` is `False` (VA-001 established that
OpenRouter accepts and ignores `prompt`), so no vocabulary biasing for agent, project, or
domain names is available on this provider. That lever returns with local STT in M7.

## The benchmark

```bash
export OPENROUTER_API_KEY=...
.venv/bin/python scripts/voice_stt_benchmark.py build-corpus \
  --manifest docs/feature-voice-agent/benchmarks/va002_stt_corpus.json
.venv/bin/python scripts/voice_stt_benchmark.py run \
  --manifest docs/feature-voice-agent/benchmarks/va002_stt_corpus.json \
  --report docs/feature-voice-agent/benchmarks/va002_openrouter_stt.json \
  --repeats 2 --max-cost-usd 0.15 --language-hint auto
```

Every request reserves its worst-case cost from `--max-cost-usd` *before* it is sent, at
`max(model cost ceiling, worst usd per audio second observed so far) x clip audio seconds`,
and the reservation is only released by an invoice. A request whose cost the provider does
not report — a timeout, any other `SttError`, or a successful event with no `usage.cost` —
keeps its full reservation held against the budget, because a request that was sent may
have been billed. Cumulative exposure is therefore reported spend plus reservations still
held, and it, not reported spend, is what the next reservation is checked against.

**The ceilings are assumptions, not the provider's own bound.** They live in
`voice_stt_bench.STT_MODEL_COST_CEILING_USD_PER_AUDIO_SECOND`: whisper `1e-05` and
gpt-4o-mini-transcribe `2e-04`, 3.0x and 3.7x the worst rate each model actually charged
across the 656 requests below. Only one of those maps onto how the model is priced —
[whisper-large-v3-turbo is billed per audio second](https://openrouter.ai/openai/whisper-large-v3-turbo/pricing)
(USD 0.000003/s at the time of writing), so a usd-per-audio-second ceiling is the
provider's own unit. [gpt-4o-mini-transcribe is token-priced](https://openrouter.ai/openai/gpt-4o-mini-transcribe/pricing),
so its ceiling is a claim about how many tokens a second of speech produces, measured on
*this* corpus. Denser speech, a different language mix, or a reprice can break it, and both
numbers have to be re-derived when either model's price changes.

What the guard does and does not promise:

* **Unconditional.** A model with no ceiling fails closed before the first request unless
  `--max-cost-per-audio-second-usd` supplies a bound; a negative, `NaN`, or infinite budget
  is rejected outright; `--max-cost-usd 0` sends nothing. No request is *authorized* unless
  its reservation fits inside the remaining budget, so the number of requests a run may
  send is capped from request one rather than from the first invoice.
* **Conditional on the ceiling assumption.** The reservation is an estimate of what a
  request will cost, so what a run actually spends is bounded only insofar as that estimate
  holds. Two outcomes, and they differ:
  * *The provider reports a cost above the reservation.* Detected on that response: the run
    **fails** naming the overrun instead of returning a successful cap-respecting run, so
    real spend passes the cap by at most that one request, and the observed rate supersedes
    the ceiling for every reservation that would follow.
  * *The provider reports no cost at all* — a timeout, any other `SttError`, or a
    successful event with no `usage.cost`. The request stays accounted at its reservation,
    which is the configured exposure estimate and nothing stronger. If the true charge was
    higher, nothing in the response says so, the observed-rate escalation never triggers,
    and several such requests can each exceed their estimate, so the overrun is neither
    bounded to one request nor detected at all. Only reconciliation against the provider's
    own billing closes that gap: every successful sample records its
    `sttb_provider_generation_id`, so an unpriced success can be looked up afterwards, while
    a failed request carries no generation id and cannot be.

  This is the price of a client-side guard over a provider that invoices after the fact, and
  it is why the ceilings are set several times above measured rates rather than at them.

### Corpus

82 clips, 324 s of audio, all of it real human speech. Speech comes from Tatoeba sentence
recordings (CC BY 2.0 FR); every clip records its sentence id, audio id, contributor, and
the sha256 of the mp3 it was decoded from, so the corpus is a recipe rather than a
snapshot. No audio is committed to the repository and no customer audio is involved.

| Bucket | Clips | Construction |
| --- | ---: | --- |
| `short` | 36 | one sentence, 1.6–2.7 s, 12 each in English, Russian, Spanish |
| `long` | 9 | consecutive same-language sentences joined by 0.35 s gaps to 17.9–21.6 s, 3 per language |
| `noisy_snr10/5/0` | 34 | short clips mixed with six-talker babble at +10/+5/0 dB SNR |
| `silence` | 3 | 3 s of digital silence, to catch invented speech |

Babble is built by summing six real recordings from a fourth language, tiled to length and
mixed at a computed gain; `measured_snr_db` re-derives the achieved ratio from the residual,
so the label on the bucket is a measurement rather than an intention. 32 of the 34 clips
landed within 0.01 dB of their target and the two outliers are `noisy_snr5_es_05` at
0.015 dB and `noisy_snr0_en_07` at 0.056 dB. Both are clip-to-rail effects: 0.06% and 0.32%
of their samples hit the int16 peak once speech and babble were summed, which is what a
loud clip at 0 dB SNR does. That is two orders of magnitude below the 5 dB steps the
buckets compare. Digital silence is the strictest
possible hallucination probe and also the least realistic one — real room tone is VA-305's
corpus, not this one.

### Measurement

One request in flight at a time, because the number that matters to a caller is what one
voice actor waits for; concurrency is VA-106's and VA-004's measurement, not this one.
Percentiles are nearest-rank over the observed sample. Cost is the provider's own reported
`usage.cost`, not a price-list estimate. Each clip is sent twice per model; the second pass
was not systematically faster than the first (gpt-4o-mini median 619 → 589 ms, whisper
764 → 805 ms), so the repeats measure real provider variance rather than a cache.

Latency here is the HTTP request: upload, inference, response. It is the dominant term of
the plan's "user speech end → final STT" budget but not all of it — VAD end-of-utterance
detection and PCM assembly sit in front of it and belong to VA-105.

## Results

656 requests across the two runs, **zero provider errors, zero timeouts**.

### Latency by model and bucket (auto-detect run, ms)

| Model | Bucket | n | p50 | p95 | p99 |
| --- | --- | ---: | ---: | ---: | ---: |
| `gpt-4o-mini-transcribe` | short | 72 | 573 | 909 | 971 |
| `gpt-4o-mini-transcribe` | long | 18 | 1080 | 2270 | 2270 |
| `whisper-large-v3-turbo` | short | 72 | 772 | 2927 | 5361 |
| `whisper-large-v3-turbo` | long | 18 | 1343 | 2998 | 2998 |

The plan's provisional budget for end-of-utterance → final STT is 1000 ms, 500 ms as a
stretch. On conversational-length utterances `openai/gpt-4o-mini-transcribe` fits it at
p99 with the STT request alone; `openai/whisper-large-v3-turbo` has a p50 inside the budget
and a tail far outside it — its worst short-utterance request took 5.4 s, which is dead air
a caller would notice. Long utterances cost about a second more for both, at a real-time
factor of 0.06–0.08.

### Cost and accuracy

| Model | USD / audio minute | WER en | WER ru | WER es |
| --- | ---: | ---: | ---: | ---: |
| `whisper-large-v3-turbo` | 0.00020 | 0.038 | 0.153 | 0.012 |
| `gpt-4o-mini-transcribe` | 0.00192 | 0.049 | 0.037 | 0.113 |

whisper-large-v3-turbo is 9.6× cheaper. Neither model wins every language: whisper is
better on English and clearly better on Spanish, gpt-4o-mini is four times better on
Russian. Word error rate is computed after case folding and punctuation stripping but
nothing else, so it is a pessimistic bound — whisper's Russian output normalises `ё` to `е`
and adds quotation marks, and both cost a full word each.

### Noise

| Bucket | WER whisper | WER gpt-4o-mini |
| --- | ---: | ---: |
| `noisy_snr10` | 0.000 | 0.025 |
| `noisy_snr5` | 0.089 | 0.106 |
| `noisy_snr0` | 0.150 | 0.100 |

Both models stay usable down to 0 dB against six-talker babble. That is a weaker stress
than it sounds: babble is speech-shaped and steady, and the underlying Tatoeba recordings
are clean and close-miked. Impulsive noise, keyboard, and speaker echo leakage are
VA-305's corpus and are not claimed here.

### Silence

Three seconds of digital silence, 12 requests across both models and both runs: **zero
characters returned**. Neither model invented speech. This is the floor case, not proof
against real room tone.

### Auto-detection flips language, and an explicit hint fixes it

Under auto-detection, `whisper-large-v3-turbo` transcribed one Russian utterance into
Polish orthography on both repeats:

```text
reference   Мне много денег не надо.
auto        Mnie mnogo dzień nikt nie nadał.
hinted      Мне много денег не надо.
```

Two of 80 Russian requests came back in the wrong script under auto-detection; with
`sttc_language` set from the clip's language, zero did. Sending the language also lowered
Russian word error rate substantially, and left English untouched:

| Model | Language | WER auto | WER hinted |
| --- | --- | ---: | ---: |
| `whisper-large-v3-turbo` | ru | 0.153 | 0.063 |
| `gpt-4o-mini-transcribe` | ru | 0.037 | 0.010 |
| `whisper-large-v3-turbo` | en | 0.038 | 0.038 |
| `gpt-4o-mini-transcribe` | es | 0.113 | 0.146 |

Spanish on gpt-4o-mini moved the wrong way by roughly one word across 40 requests, which
is inside the noise of a single run at this sample size; the Russian effect is not.

**Consequence for VA-105 and VA-203:** send `language` whenever the workspace or user has a
selected locale, and treat auto-detection as the fallback for users who have not chosen
one. A short utterance carries little evidence for language identification, and the failure
is not a graceful degradation — it produces a fluent, confident transcript in the wrong
language that would become a durable human message.

Latency did not improve with the hint, and the tails moved in both directions between runs.
At 72–78 requests per group, single-run p95/p99 differences of a second are provider
routing variance and should not be read as a model property.

## Provisional default decision

The M0 exit review selected `openai/gpt-4o-mini-transcribe` because the 1000 ms
end-of-utterance-to-final-STT conversational p95 budget is binding and its measured short
p95 was 909 ms. `openai/whisper-large-v3-turbo` was rejected as the provisional default:
its short p95 was 2927 ms and its worst request took 5.4 s, despite being 9.6x cheaper and
better on English and Spanish word error rate.

`VOICE_DEFAULT_STT_MODEL` now names `openai/gpt-4o-mini-transcribe`. This is a
configuration change and does not bump the contract version. An explicit
`SttConfig.sttc_language` is sent whenever the workspace or user has selected a locale;
provider auto-detection remains the fallback only when no locale was chosen.

## Rollout and rollback

Nothing to roll out: no runtime path imports `voice_stt.py`, and the OpenRouter key is
only read by the benchmark script from the environment. Rollback is reverting the commits.
Re-running the benchmark costs about USD 0.023 per pass and is bounded by `--max-cost-usd`,
which reserves each request's estimated worst-case cost before sending it, so a run stops
rather than authorize more than the cap — see **The benchmark** above for the reservation
rule, the ceilings it estimates from, and what that bound does and does not cover once the
provider bills.

Every report generated from here on records its own `language_hint`, `budget_max_cost_usd`,
`budget_reservation_method`, and the per-model ceilings it reserved against, so a reader can
tell what bound a measurement ran under without reconstructing the command. The two reports
below predate that: their `stt_metadata` carries those fields with a
`metadata_backfill_note` saying they were recorded afterwards from the command above, and
that both runs used the original post-request spend check. No sample in either report was
touched.
