# Iteration 6 — VA-002: an unbilled request is not a free one

Integrated from `PER-5-va-002-build-and-benchmark-the-openrouter-utterance-stt-adapter`.

Goal: the third QA pass. The declared-ceiling reservation from iteration 5 was accepted in
shape; two ways out of it were not, and one sentence of documentation claimed more than the
code can deliver.

## A reservation released by silence is not a reservation

The loop reserved a request's worst-case cost, sent it, and then settled with
`spent_usd += sample.sttb_cost_usd or 0.0`. Two ordinary outcomes make that zero: a request
that failed (a timeout, a rate limit) and a successful event whose `usage.cost` is absent —
which the adapter deliberately treats as valid. Both released the reservation, so under a
cap sized for exactly one request, QA got five.

The mistake was modelling the budget as *money reported spent* when what a budget has to
track is *money possibly owed*. A timed-out request may well have completed upstream and
been billed; an unpriced success certainly ran. Accounting now carries both halves —
`reported_usd` for provider invoices and `unpriced_usd` for reservations still held — and
every authorization is checked against their sum. An unknown cost is settled at its full
reservation — the configured exposure estimate, which is the largest number available at
that moment, but an estimate rather than a floor on what was actually billed.

## Saying "cannot spend past its cap" was a claim the client cannot make

Iteration 5's doc said a run cannot spend past its cap. It cannot spend past it
*voluntarily*: no request is authorized unless its reservation fits. But a provider that
charges above the declared ceiling has already taken the money by the time the invoice
arrives, and no client-side guard can undo that. The old code then compounded it by
returning the run as a success.

The guarantee is now stated in the two halves that are actually true. Authorization is
bounded from request one, unconditionally: the number of requests a run may send follows
from the reservations, not from any invoice. Settlement is bounded only where the provider
reports a cost — a reported charge above its reservation raises instead of returning, so
real spend passes the cap by at most that request, and the observed rate supersedes the
ceiling for every reservation that would follow.

Where no cost is reported at all, there is no such bound and it would be dishonest to write
one. A timeout or a success without `usage.cost` stays accounted at its estimate; if the
true charge was higher, the response does not say so, the observed-rate escalation never
fires, and several unpriced requests can each exceed their estimate undetected. The only
thing that closes it is reconciliation against the provider's own billing, using the
generation id every successful sample already records. That is a real limitation of a
client-side guard over a provider that invoices after the fact, not a bug to be papered
over, and it is the reason the ceilings sit at 3-4x measured rates instead of at them.

## The ceilings are assumptions, and only one of them shares the provider's unit

QA checked the pricing pages, and the distinction is worth writing down.
`whisper-large-v3-turbo` is billed per audio second, so a usd-per-audio-second ceiling is
denominated in the provider's own unit and a reprice is the only thing that can move it.
`gpt-4o-mini-transcribe` is token-priced: its ceiling is a claim about how many tokens a
second of speech yields, measured on this corpus. Denser speech or a different language mix
can exceed it without anything being repriced at all. That is exactly the case the overrun
path above exists for, and it is now labelled as an assumption in `stt_adapter.md` rather
than presented as a bound.

## A number too big to be a float is still not a number

`{"cost": <400-digit integer>}` passed the `isinstance(value, int)` check and then overflowed
inside `math.isfinite`, escaping as a raw `OverflowError` — the exact class of leak
iteration 4 was supposed to have closed. The fix removes the float conversion from the
validation path entirely: `0.0 <= value <= USAGE_VALUE_MAX` compares an arbitrarily large
Python integer against a float exactly, without converting it, and rejects `NaN`, the
infinities, and negatives in the same expression. `USAGE_VALUE_MAX` is `1e9` — orders of
magnitude above any real transcription's seconds, tokens, or dollars.
