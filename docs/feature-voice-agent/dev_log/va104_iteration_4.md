# Iteration 4: the generic voice worker and the per-call actor skeleton

Integrated from `PER-11-va-104-add-the-generic-voice-worker-entry-point-and-per-call-actor-skeleton`.

## Goal

Give one dispatched LiveKit room one isolated call actor with an enforced lifecycle, a
scoped cancellation boundary, a bounded initialization deadline, and structured correlation
IDs — with exactly one generic worker, never a worker per Flexus agent.

## What was tried

The pinned `livekit-agents==1.7.1` wheel was read before any entry-point code was written,
because the framework's real shape decides the design. Three facts changed the plan:

`AgentServer.__init__` does not take `entrypoint_fnc`, `request_fnc`, or `agent_name`;
`rtc_session()` registers all three afterwards. That ordering is what makes the capacity
gate possible at all: the `on_request` closure can capture the already-constructed server
and read `server.active_jobs` and `server.draining`.

The framework already owns the two things a hand-rolled ledger would have duplicated —
the active job list and drain — so the worker keeps no registry of its own. A duplicate
ledger could only drift from the framework's truth.

Jobs run in a subprocess (`_default_job_executor_type` is `PROCESS` off Windows), so the
capacity policy runs in the main process and the actor runs in the job process. Only
`entrypoint_fnc` and `setup_fnc` cross that boundary and must stay pickle-able; the
`load_fnc` and `on_request` closures never do.

## What broke

The LiveKit SDKs are not installed in the developer venv and are deliberately excluded from
`requirements-ci.lock`, so nothing in the normal test tier can import the entry point. The
first design put lifecycle and capacity logic in the entry point, where no committed test
could ever reach it.

`.env.example` rejected the new `FLEXUS_VOICE_WORKER_MAX_SESSIONS` variable — VA-001's
env-contract test caught the omission before the change was finished. It works.

## Decisions and reasons

The SDK boundary is one file. `voice_call_actor.py` and `voice_worker.py` are pure Python
with no LiveKit import, so the lifecycle, cancellation scopes, ID minting, capacity, and
admission rules are unit-tested in the normal tier without the media stack.
`service_voice_agent.py` is thin wiring over them and is the only module in the repository
allowed to import `livekit`; `test_generic_voice_worker.py` fails if a second one appears,
which protects the CI/image isolation VA-001 established.

The lifecycle states are transcribed from the architecture plan rather than invented, and
the transition rule is the plan's own sentence — `CREATING` reaches only `CONNECTING` or
`CLOSING`, `CONNECTING` reaches only `LISTENING` or `CLOSING`, any active state reaches any
other active state or `CLOSING`, and `CLOSED` is terminal. Five rules, not a sixteen-row
table nobody can audit. The boundary invariants are what the tests pin: a call cannot skip
initialization, cannot open a turn before it is listening, and cannot start work once it is
closing.

Cancellation is two scopes, not one. Turn-scoped work dies on `cancel_turn` and on the next
`begin_turn` (reason `stale_turn`); call-scoped work survives both and dies only in
`aclose`. A single scope would make barge-in tear down the room event loop, and no scope
would leak a TTS stream into the next turn. `aclose` is idempotent through an
`asyncio.Event`, so a concurrent close and a shutdown callback cannot both drive the
terminal transition.

Bounded initialization is `asyncio.timeout(VOICE_ACTOR_INIT_DEADLINE_S)` around a
caller-supplied initializer, and `start()` deliberately does not catch the timeout: the
entry point lets the job fail and the registered shutdown callback closes the actor. The
skeleton's initializer re-asserts the worker environment contract; VA-102 adds the
authoritative session fetch and VA-105 adds provider and pool warmup inside the same
deadline.

Dispatch metadata is `{"vsession_id": ...}` and nothing else — an extra key is an error,
not an ignored field. A worker that accepted `agent_name` from LiveKit metadata would be
an agent-specific worker wearing a generic name, and would trust the media plane for
authority. The room name must already be an opaque `vroom_` ID for the same reason.

Turn IDs are `<vsession_id>.<vleg_seq>.<vturn_seq>` with `.speech` and `.utterance`
suffixes: monotonic, greppable across Python and SQL, and free of room names, people, and
agent names. `correlation_fields()` is asserted to stay inside
`VOICE_CORRELATION_ID_FIELDS` and is assigned to `JobContext.log_context_fields`, so every
SDK log line for that job carries the call's IDs and nothing else.

Capacity is explicit rather than CPU-derived, as the plan requires. `session_load` feeds
`load_fnc` with `load_threshold=1.0`, so the worker advertises full exactly at
`FLEXUS_VOICE_WORKER_MAX_SESSIONS`. `decide_admission` checks the duplicate room first and
rejects it with `terminate=True` — a second actor for one room must not be created on any
replica — while a full or draining worker rejects with `terminate=False` so another replica
can take the call. The default of 4 slots is an unmeasured placeholder and says so; VA-106
measures the real number.

The contract moved to 1.1.0, a minor bump under VA-001's own rule: four values were added
and none changed, so benchmarks recorded against 1.0.0 remain comparable.

## Verification

The committed tier is SDK-free and green: 103 voice service tests and 241 tests across
`tests/services/voice` and `tests/_project`.

Because CI cannot install the media stack by design, the entry point was verified out of
band against a real `pip install livekit==1.1.15 livekit-api==1.2.1 livekit-agents==1.7.1`
in a throwaway virtualenv. That run proved the server registers under `flexus-voice-agent`
with `load_threshold=1.0` and the correct entrypoint; that load is 0.0/0.5/1.0 at 0/1/2 of
2 slots and 1.0 while draining; that a free worker accepts with an opaque `vpart_`
identity, a duplicate room rejects with `terminate=True`, and full and draining rejects
carry `terminate=False`; that two `fake_job_context` jobs produce two actors with distinct
structured IDs; that agent-selecting metadata and a human-readable room name are both
refused; that the prototype gate refuses to build a server with `FLEXUS_VOICE_ENABLED`
unset; and that `python -m flexus_backend.services.service_voice_agent` exposes the
framework's `dev`/`start` CLI. This gap is a consequence of the deliberate CI exclusion,
not an accident, and closing it belongs with the milestone gate that lets the media stack
into CI.

## Rollout and rollback

No deployment starts the worker, no image contains it, and no migration or durable record
is involved. Rollout is running it locally per `docs/RUNBOOKS.md` after installing the
`voice` extra. Rollback is stopping the process, or reverting the commit. Nothing dispatches
to it until VA-102 mints tokens and creates dispatches.
