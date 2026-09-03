# Iteration 4 — VA-102 token and explicit-dispatch control path

Integrated from `PER-9-va-102-implement-a-prototype-token-and-explicit-dispatch-control-path`.

Task: VA-102. Blocked by VA-101, which is done; this branch is stacked on it.

Goal: an authenticated development caller receives a short-lived room-scoped token, and the
generic worker receives only signed opaque job metadata.

## What was built

One GraphQL mutation and three service modules:

* `flexus_v1/v1_voice.py` — `voice_prototype_session_start`, behind the standard auth chain
  and `FLEXUS_VOICE_ENABLED`;
* `services/voice/voice_livekit_tokens.py` — the participant token and the dispatch-time
  `roomAdmin` token;
* `services/voice/voice_job_metadata.py` — sign/verify the opaque blob the worker receives;
* `services/voice/voice_control_plane.py` — environment, HTTP origin, the explicit dispatch
  call, and the orchestration that ties the three together.

## Decisions and why

**The LiveKit SDK is not imported.** VA-001 put `livekit-api` in a `setup.py` extra that
`requirements-ci.lock` deliberately excludes, so the media stack cannot reach CI or a
production image. A module-scope `import livekit.api` in the control plane would therefore
crash backend startup in every real deployment, and a function-scope import to dodge that is
banned by the repository rules. A LiveKit access token is an HS256 JWT and the dispatch API
is Twirp over HTTP, so both are produced directly with PyJWT and `httpx`. The claim shape was
read out of `livekit/protocol`'s `auth/grants.go` and `auth/accesstoken.go` rather than
guessed, and the live check below is what proves it, since a hand-built token that the server
rejects is the obvious failure mode of this choice.

**Two tokens, not one.** `AgentDispatchService.CreateDispatch` runs
`EnsureAdminPermission(room)`, so dispatching needs `roomAdmin` on that room. Reusing the
participant token would have meant handing the browser room-admin rights. Instead the backend
mints a second, 30-second `roomAdmin` token that never leaves the process. The live check
confirms the participant token gets `401 permissions denied` from the dispatch API.

**Server-side explicit dispatch, not token-embedded dispatch.** LiveKit also supports
carrying `roomConfig.agents` inside the participant token. That would put the signed job
metadata in the browser's hands and would only fire on room creation. Explicit dispatch keeps
the metadata server-to-server and matches the architecture plan's step 6.

**`vsess_` became a contract ID kind.** The plan says the job metadata carries the voice
session ID, and VA-001 only defined `vroom_`/`vpart_`. Adding a third opaque kind is a minor
contract bump under VA-001's own versioning rule; no existing value moved, so the M0
benchmarks stay comparable.

## What broke, and what it changed

A test asserting that a participant token refuses a non-room identifier failed: it did not.
`is_opaque_voice_id` matches any of the three prefixes, which share one shape, so a `vsess_`
ID passed as a room name and a room name would have passed as a session ID. Nothing in the
prototype path fed it bad input today, but the checks read like validation while validating
almost nothing, and VA-104's worker parses the same metadata.

Fixed at the contract level with `is_opaque_voice_id_of(vid_value, vid_prefix)`, which raises
for an unknown kind and is now the only form used at any mint or parse boundary. The loose
`is_opaque_voice_id` remains for "is this opaque at all" questions. Both are covered,
including the correctly-signed-but-swapped-IDs case, which is refused.

## Evidence

Verified against the real pinned `livekit/livekit-server:v1.13.6` from `compose.voice.yml`,
not only against mocks:

* the minted participant token returns `success` from the server's own `/rtc/validate`;
* a token an hour past `exp` and a token with a flipped signature byte both return 401;
* the server stored one dispatch for `flexus-voice-agent`, and its `metadata` round-trips
  through `load_voice_job_metadata` to the same `vsession_id`/`vroom_name` the caller got;
* the participant token cannot call `CreateDispatch`.

LiveKit accepts a token a few seconds past `exp` and rejects it at a minute; that is its
clock-skew leeway, so the TTL constants are bounds rather than second-accurate fences. The
runbook says so rather than implying otherwise.

## Left for later milestones

No durable session/leg/speech rows, no quotas, no audit records, no idempotency key, no token
refresh, no `voice_session_end` — those are VA-201/VA-202, and `voice_prototype_session_start`
should be deleted when `voice_session_create` lands rather than renamed into it. Nothing
consumes the dispatched job yet; VA-104 owns the worker that will.
