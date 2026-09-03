# VA-404 iteration 21: integration bulkheads and durable background operations

## Goal

Stop a slow or rate-limited third-party integration from consuming a voice worker, and give
background-declared work a durable operation identity instead of an inline wait.

## What was tried

The bulkhead was added as a third bind-time wrapper rather than as executor logic. `run_executor`
only observes `tool_call`/`tool_result`/`tool_error` activities; it never wraps the tool coroutine,
so it cannot bound anything. `resolve_run_tools` already funnels every resolved tool through
`compose_tool_wrappers`, which is the one place every tool of every run passes through.

Bulkhead identity reuses the integration registry instead of a new table of provider names. The
descriptors already map each integration to its tool names, so the bulkhead key is derived, not
duplicated.

## What broke

The first wrapper was a plain `BaseTool` delegate. It passed its own tests and broke real code:
`AttributeError: '_BulkheadTool' object has no attribute 'coroutine'` in the CRM internal-runtime
test. Call sites read `.coroutine` off a composed tool. The wrapper was rebuilt as a `StructuredTool`
with a wrapped coroutine, mirroring `wrap_tool_with_activity_title`, preserving `__signature__` so
downstream argument introspection still works. A tool with no coroutine is now returned unchanged.

The policy module first called `tool_resolution.ensure_integrations_discovered()`. That closed an
import cycle — `tool_bulkhead_policy` to `tool_resolution` to `tool_policy_bind` to `bulkhead_bind`
to `tool_bulkhead_policy` — and every collection failed on a partially initialized module. The
policy module now calls `registry.discover_integrations()` directly; the registry depends on nothing
in the execution package.

The initial queue-wait bounds (2s external, 5s internal) were written for a voice-only path. The M4
gate forbids a parallel voice-only tool implementation, so one bulkhead serves every run, and those
values would have rejected ordinary text-run tool calls that previously just succeeded. They were
raised to 10s external and 30s internal, which sheds load under genuine saturation without changing
normal operation.

The breaker counted tool confirmations as integration failures. `interrupt()` raises `GraphInterrupt`,
which derives from `GraphBubbleUp` and therefore from `Exception`, so the broad handler that exists to
record failures swallowed it into `_record_failure`; five confirmation-gated calls in a row would have
opened an external integration's circuit, and a read-only confirmation tool such as `ask` was retried,
re-running it. `activity_title.py` already guards its timeout against exactly this and the guard was
not mirrored. Two tests now pin it: the breaker stays closed across five interrupts, and an
interrupted call runs its body once.

## What was decided and why

One global bulkhead path, not a voice-only branch. The M4 gate requires no parallel voice-only tool
implementation, and a provider quota is not voice-specific. Semaphores are per process, and voice
and text workers are separate processes, so a per-process bulkhead already scopes to the worker
whose audio loop is being protected. A globally shared provider quota needs Redis and stays VA-601
capacity work; this is stated rather than implied.

Deadlines were not reimplemented. `activity_title.py` already applies a 60s default with per-tool
overrides and `FlexusToolTimeoutError`. The bulkhead sits outside it so queue wait and execution time
stay separate metrics and a queued call is not charged an execution timeout it never used.

The breaker is disabled for the internal pool. A Flexus-internal failure opening a breaker would
block unrelated internal tools, which is worse than the failure it reacts to. External integrations,
where the failure is a real remote dependency, keep it.

Retry is restricted to operations the VA-403 declaration calls read-only, and an unknown tool is
never retryable. Retrying a possibly committed side effect without an idempotent status probe would
contradict the VA-403 cancellation contract.

`CancelledError` is never a breaker failure and never retried. Barge-in must not open a circuit.

Durable operation IDs are derived, not stored in a new table. The digest over workspace,
conversation, tool, and idempotency key is stable across restarts, so it works directly as an ARQ
`_job_id`, whose duplicate-reservation behavior is the repository's existing idempotency mechanism.
The ledger refuses a new entry when full rather than evicting, because silently forgetting an
operation ID would break the idempotency it exists to provide.

## Verification

63 tests across the three new execution modules, plus bind-layer tests proving the wrapper preserves
`.coroutine`, its signature, the tool contract, and that the bulkhead is actually engaged rather than
a no-op. Voice tool-operation tests cover the durable background identity: a background start mints
one ID, a repeat returns the same one, an unknown operation gets none, IDs are scoped per turn, and
the same operation in two conversations gets different IDs.

`flexus_backend/tests/langgraph_runtime` and `flexus_backend/tests/services/voice` pass at 2561
passed, 2 expected SDK-gated skips. The 1221-test tool tier is the regression guard that caught the
`.coroutine` break and is green.

Metrics use the existing OpenTelemetry facility rather than a new one: three instruments follow the
`flexus_metrics.py` idiom of a name constant, a no-op instrument, and a record function registered in
`metrics_init`. They are no-ops unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set, so tests are unaffected.
Queue wait is recorded as its own histogram because saturation and a slow provider are different
incidents with the same symptom.

Per-user and per-workspace rate limits were deliberately not added. `flexus_v1/rate_limit.py` is the
repository's limiter, but it raises `HTTPException` and disables itself outside production, so calling
it from inside a LangGraph tool would put an API-layer exception on the tool path and would not run in
the environments this protects. The plan lists rate/spend limits alongside bulkheads; they belong with
VA-601 capacity work, where a Redis-backed cross-worker limit is already required.

## Next

VA-405 proves the foreground/confirmation/background matrix end to end and is the M4 gate evidence.
The in-process `snapshot()` stays available for it and for the VA-603 load suite alongside the
exported metrics.
