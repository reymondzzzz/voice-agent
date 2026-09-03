# Iteration 3 — VA-002: the OpenRouter utterance STT adapter

Integrated from `PER-5-va-002-build-and-benchmark-the-openrouter-utterance-stt-adapter`.

Goal: a stable STT provider interface plus real measurements on real speech, so that M0
can pick a provisional model on evidence instead of on a price list.

## What the provider actually returns

Probing the endpoint before writing the adapter changed three design choices.

`usage` is model-shaped, not endpoint-shaped. `whisper-large-v3-turbo` bills audio and
returns `{"seconds": 3.761, "cost": 1.25e-05}`; `gpt-4o-mini-transcribe` bills tokens and
returns `{"input_tokens": 37, "output_tokens": 13, "total_tokens": 50, "cost": 0.000111}`.
`SttUsage` therefore carries all five fields as optionals and the benchmark reports cost
per audio minute so two differently-priced models can be compared at all.

There is no request id in the response body. `X-Generation-Id` is in the headers, and it is
the only correlation handle the provider offers — so `stte_provider_generation_id` reads a
header rather than a JSON field.

There is no confidence score anywhere. The plan's "empty or low-confidence STT does not
create a user message" rule has half a lever on this provider: emptiness is observable,
confidence is not. That is recorded in the adapter doc rather than papered over with an
invented threshold.

## Retry was deliberately not implemented

The failure matrix allows "one bounded retry only when it fits latency budget". The adapter
does not know the budget — end-of-utterance detection has already spent part of the turn by
the time it is called, and only the session actor knows how much. A retry here would double
the deadline invisibly to the state machine. The adapter raises a named error and the
caller decides.

## Building a corpus without committing audio

"Real short/long/noisy English and required-language utterances" needs real speech and
reproducibility, and the repository is the wrong place for audio blobs. The corpus is a
manifest of Tatoeba sentence ids, audio ids, contributors, licences, and sha256 hashes;
`build-corpus` downloads and decodes them into a cache outside the repository, and `run`
verifies each clip's PCM against the recorded hash before spending a request. The manifest
also pins the ffmpeg version, because the hash is only stable for a given decoder.

Long utterances are consecutive same-language sentences joined by 0.35 s gaps. That is
spliced rather than single-take, which is worth knowing when reading the long-bucket
numbers; it buys an exact reference transcript for a 20 s utterance, which single-take
audio would not have given without alignment work.

Noise is six-talker babble from a fourth language, mixed at a computed gain.
`measured_snr_db` re-derives the achieved ratio from the residual so the label on the
bucket is a measurement rather than an intention; 32 of 34 clips landed within 0.01 dB of
their target, the worst being `noisy_snr0_en_07` at 0.056 dB, where 0.32% of the summed
samples hit the int16 rail.

## What the measurements changed

The headline was not the model ranking. It was that **auto-detection silently transcribes
into the wrong language**: `whisper-large-v3-turbo` rendered a Russian utterance in Polish
orthography, deterministically, on both repeats. It does not fail — it returns fluent,
confident text that would have become a durable human message in the user's conversation.

Two of 80 Russian requests came back in the wrong script under auto-detection, zero with an
explicit `language`, and Russian word error rate fell by 59% (whisper) and 73%
(gpt-4o-mini) while English was untouched. VA-105 and VA-203 should send the language
whenever the workspace or user has selected a locale. That is the finding this task exists
to produce, and it was invisible from the model catalogue.

The second finding is that the two candidates do not order consistently.
`gpt-4o-mini-transcribe` is the only one that meets the 1000 ms budget at p95 on
conversational utterances (909 ms versus 2927 ms); `whisper-large-v3-turbo` is 9.6× cheaper
and better on English and Spanish, but its worst short-utterance request took 5.4 seconds.
Selecting between them is the M0 exit review's call, so `VOICE_DEFAULT_STT_MODEL` was left
where VA-001 put it and the trade-off is written down instead.

## What was measured and what was only claimed

Measured: request latency at one request in flight, provider-reported cost, word error rate
against exact references, wrong-script rate, and behaviour on digital silence (zero
characters invented, both models).

Not measured, and not claimed: concurrency (VA-004/VA-106), real room tone, keyboard and
speaker echo leakage (VA-305), end-of-utterance detection latency (VA-105), and any tail
behaviour that a single run at 72 requests per group could distinguish from provider
routing variance.
