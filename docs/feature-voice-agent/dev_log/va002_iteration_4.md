# Iteration 4 — VA-002: closing the QA findings on the STT adapter

Integrated from `PER-5-va-002-build-and-benchmark-the-openrouter-utterance-stt-adapter`.

Goal: the three blocking findings from the VA-002 acceptance review, without changing the
measurements the review already verified.

## A normalized error kind is a promise about every path, not just the ones I imagined

The adapter's claim is that session logic never has to catch anything but `SttError`. The
review found two ways through it. A 200 whose JSON is the wrong shape reached
`payload["text"].strip()` and `float(usage["cost"])` unguarded, so a provider that returned
`{"text": 42}` or `{"usage": [...]}` raised `AttributeError`/`ValueError` at the caller; and
a PCM buffer with a trailing half-sample fell through `_validated_audio_seconds` into
`pcm16_to_wav`, whose frame check is a `ValueError`.

Both are now validated rather than caught. `_validated_payload` checks the body shape once
and returns `(text, usage)`, with every wrong type raising `malformed_response`; the
audio-frame check moved up beside the 30 s bound and raises `invalid_audio` before any
request is sent. Catching would have been shorter, but the repository bans catching
`TypeError` and `AttributeError` together for exactly the reason that applies here — the
handler would also have swallowed a real bug in the adapter.

One shape is deliberately *not* an error: a `null` `text` still yields an empty `final`
event. An empty transcript is a result the provider genuinely returns for silence, and the
session's rule for it is already "do not create a user message".

## A budget checked after the request is not a budget

`--max-cost-usd` compared spend against the cap at the top of the loop, so it stopped only
*after* the request that broke it, and `--max-cost-usd 0` still bought one transcription.

The guard now runs before each request and reserves headroom for it:
`request_fits_budget` requires `spent + projected < cap`, where `projected_request_cost_usd`
prices the next clip at the worst per-audio-second rate the provider has actually reported
so far in this run. A non-positive cap now sends nothing. Both functions live in
`voice_stt_bench.py` rather than in the script, so the money logic is unit-tested.

The residual is one request: the first of a run is unpriced, because OpenRouter only reports
cost after the fact. At the worst rate either model charged in these runs
(USD 0.0000538 per audio second) that is USD 0.0012 for the longest clip in the corpus.
Closing it entirely would mean fetching and trusting a price list before spending — more
surface, and an estimate rather than the provider's own number.

## The corpus was accurate; the sentence about it was not

The docs claimed every noisy clip landed within 0.01 dB of its target SNR. Recomputing from
the manifest: 32 of 34 did, `noisy_snr5_es_05` is 0.015 dB out and `noisy_snr0_en_07` is
0.056 dB out. Re-measuring the two cached clips explains why, and it is not rounding:
0.06% and 0.32% of their samples hit the int16 rail once speech and babble were summed, so
the mix clips. The measurement was always in the manifest and the deviations are two orders
of magnitude below the 5 dB steps the buckets compare — the summary was simply rounder than
the data. Fixed in `stt_adapter.md` and in iteration 3.

No benchmark was re-run: no code that produces a measurement changed, and the two recorded
reports still match their manifests.
