# Iteration 5 — VA-002: making the cost cap a bound rather than a heuristic

Integrated from `PER-5-va-002-build-and-benchmark-the-openrouter-utterance-stt-adapter`.

Goal: the second QA pass. The exception-normalization and corpus-accuracy fixes from
iteration 4 were accepted; three things were not.

## "Worst observed" is not a worst case

Iteration 4 reserved the next request at the worst per-audio-second rate the provider had
already reported. That is an estimate wearing a bound's clothes, and it has no value at all
for the first request of a run: the reservation was zero, so any positive cap authorized
one unpriced call. QA reproduced a completed run at USD 0.01 against a USD 0.000001 cap —
10,000x the configured budget, reported as success. `math.inf` was accepted as a cap too.

The bound now comes from a declared ceiling instead of from history.
`STT_MODEL_COST_CEILING_USD_PER_AUDIO_SECOND` carries one entry per supported model —
whisper `1e-05`, gpt-4o-mini-transcribe `2e-04`, being 3.0x and 3.7x the worst rate each
model charged across the 656 recorded requests — and the reservation is
`max(ceiling, worst observed) x clip audio seconds`. Taking the max in both directions
matters: the ceiling bounds request one, and a provider that reprices above its ceiling is
picked up from its first invoice onward rather than silently under-reserved for the rest of
the run.

A model with no entry now fails closed, before the client is constructed, unless
`--max-cost-per-audio-second-usd` supplies a bound. Non-finite and negative budgets are
rejected at construction. `--max-cost-usd 0` sends nothing.

(Iteration 6 corrected two overstatements here: the reservation was released whenever a
request's cost came back unknown, and a provider charging above its ceiling could still
overrun the cap. See `iteration_6.md`.)

Choosing a declared ceiling over fetching OpenRouter's price list was the real decision. A
fetched price would be more precise and would also be a network dependency, a parsing
surface, and a second thing to be wrong at the exact moment the guard matters. A constant
measured from this repository's own 656 requests, with the run's own invoices allowed to
override it upward, needs no network and cannot silently degrade.

## The loop moved to where its tests belong

QA asked for an end-to-end no-network proof that a cap below the first reservation issues
zero provider calls. The loop lived in `scripts/voice_stt_benchmark.py`, and backend tests
live in `flexus_backend/tests/` mirroring the source tree — a script has no mirror. Rather
than bend the test layout, the measurement loop moved to
`voice_stt_bench.run_bounded_benchmark()`, which is where the repository's own rule already
put it: money-critical domain logic belongs in `services/`, not in a script. The script is
now a CLI that builds the corpus, loads clips, and writes the report.

## Usage numbers are money, so they are validated like money

`{"cost": true}` was accepted as USD 1.00 and `{"input_tokens": 1.9}` as 1 token, because
`bool` is an `int` in Python and `int(1.9)` truncates without complaint. Negative and
non-finite figures passed through untouched — and a negative `cost` does not merely corrupt
a metric, it *decreases* the benchmark's running spend and buys extra requests.

Every `usage` figure is now rejected unless it is a real, finite, non-negative number, and
token counts must additionally be whole. Zero stays valid: a free request and a silent clip
are both ordinary results.

## Reports now say what bound they ran under

`va002_openrouter_stt.json` never recorded its `language_hint`, and neither report recorded
its budget. New reports carry `language_hint`, `budget_max_cost_usd`,
`budget_reservation_method`, and the per-model ceilings. The two existing reports were
backfilled from the documented reproduction command and carry a `metadata_backfill_note`
saying so — including that they ran under the original post-request spend check, not the
reservation described above. Claiming otherwise would have made the artifacts lie about
their own provenance. No sample was touched; both still re-derive their recorded
percentiles and costs.
