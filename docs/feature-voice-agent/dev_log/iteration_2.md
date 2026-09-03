# Iteration 2 — VA-001: corrections from QA review

Three findings from the QA review of iteration 1. All three reproduced; all three were
values asserted from documentation or defaults instead of from the pinned artifact.

## The `livekit` pin was unresolvable

`livekit==1.1.16` was chosen as the newest release. `livekit-agents==1.7.1` declares
`livekit==1.1.15` — exactly, not a range — so the three pins together do not resolve:

```text
ERROR: Cannot install -r old.txt (line 3) and livekit==1.1.16 because these package
       versions have conflicting dependencies.
ERROR: ResolutionImpossible
```

Corrected to `livekit==1.1.15` in `setup.py` and `LIVEKIT_PYTHON_RTC_PIN`. `livekit-api`
constrains only `livekit-protocol`, so it was never part of the conflict.
`livekit-agents==1.7.1` is the latest release, so there is no forward exit that keeps
1.1.16; the agents pin is the one that decides.

Newest-available is not a pinning strategy when one of the pins is a framework that
hard-pins its own runtime. The check is a resolver run, and the document now says so.

## The RTC playout queue was the SDK default, not the SDK's value

The contract recorded 1000 ms, sourced from `rtc.AudioSource`'s documented default.
`livekit-agents` does not use that default: `voice/room_io/_output.py` line 52 constructs
`rtc.AudioSource(sample_rate, num_channels, queue_size_ms=200)`, and `RoomOptions` exposes
no way to change it. On the RoomIO path — the path this prototype uses — the queue is
200 ms. Corrected, with both numbers and the reason recorded so the next reader who checks
the rtc docs does not "fix" it back.

VA-302 inherits this: the playout queue is not a tuning knob, it is a property of the
pinned SDK.

## The self-hosted guard and its scan both had holes

`wss://flexus-prod.livekit.cloud.` passed `require_self_hosted_livekit_url`. The trailing
dot is the fully-qualified form of the same DNS name and resolves identically, so the URL
would have reached managed LiveKit. The host is now compared with trailing dots stripped.
The comparison stays on the parsed host boundary rather than becoming a substring test, so
a self-hosted `notlivekit.cloudy.internal` is still accepted.

The repository scan walked `git ls-files` and filtered by a hand-written list of text
suffixes, matching the host case-sensitively. That left 505 tracked non-`docs/` files
unscanned — every `Dockerfile.*`, the extensionless deploy entrypoints, `.mjs`, `.graphql`,
`.prisma`, `.conf`, `.java` — and it would not have seen `LIVEKIT.CLOUD` in any file at
all. A suffix allowlist is a scan that silently shrinks as the repository grows.

Replaced with `git grep --cached -I --ignore-case --fixed-strings`. Git decides what is
text (`-I`, which is git grep's spelling — `--binary-files=without-match` is GNU grep's and
git rejects it), which removes the allowlist and the maintenance it needed. The planted-offender test now plants four: an uppercase host in
`Dockerfile.backend`, a mixed-case host in a `.py` file, a lowercase host in an
extensionless `deploy/entrypoint`, and one in a `.mjs` file. A clean-repository case pins
the negative so the scan cannot pass by matching everything.

## The first fix's test did not test anything

The correction above initially shipped with a test asserting the queue was a whole number
of frames. 1000 is also a whole number of 50 ms frames, so the test passed with exactly the
value it was supposed to reject — a change-detector that detected nothing. QA caught it.

Replaced with an exact assertion that is coupled to the version the value came from: the
test pins `VOICE_ROOM_SAMPLE_RATE_HZ == 24000`, `VOICE_RTC_FRAME_MS == 50`, and
`VOICE_RTC_QUEUE_MS == 200`, and also asserts `LIVEKIT_PYTHON_AGENTS_PIN` is still
`livekit-agents==1.7.1`. Bumping the agents pin now fails until someone re-reads the SDK at
the new version. The sample rate and frame size had the same weakness as the queue and were
fixed with it rather than left for the next review.

Mutation-checked, since the point of this test is that it fails:

```text
VOICE_RTC_QUEUE_MS       200   -> 1000                  1 failed
VOICE_ROOM_SAMPLE_RATE_HZ 24000 -> 48000                1 failed
VOICE_RTC_FRAME_MS        50    -> 20                   1 failed
LIVEKIT_PYTHON_AGENTS_PIN 1.7.1 -> 1.8.0                1 failed
unmutated                                               1 passed
```

The lesson generalises: a relational assertion over a value whose whole purpose is to be
one specific number is not a test of that number.

## Contract version

Still `1.0.0`. Nothing outside this branch has consumed the contract and no benchmark has
been recorded against it, so these are corrections to an unlanded initial version rather
than changes to a published one. The changelog stays a single row.

## Verification

```text
python -m pytest tests/pipeline/test_voice_contracts.py \
  flexus_backend/tests/_project/test_no_livekit_cloud_endpoints.py -q
```

The pin correction was verified with a real resolver rather than by reading metadata:
`pip install --dry-run` on the three pins fails with `ResolutionImpossible` at 1.1.16 and
resolves 69 packages at 1.1.15. The same three pins also co-resolve with `setup.py`'s
`install_requires`, so a developer venv can carry the extra.
