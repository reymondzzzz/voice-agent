# VA-506 iteration 23: active `boss_delegate` durable task contract

## Goal

Give Boss a real, non-voice-only tool that atomically validates a target colleague agent,
creates one target-assigned kanban task directly in Todo, records human/Boss/voice provenance,
and is idempotent on retry. Active `flexus_kanban(create)` always assigns to the calling agent,
so it cannot serve delegation; the plan calls this tool a backend prerequisite for the future
"ask Sidra" voice story, and requires it not be voice-only logic.

## What was tried

`kanban_ops.py` was already at 904 lines against an effective 1000-line cap, so the new contract
went into a new sibling module, `flexus_backend/services/kanban_delegation.py`, rather than
growing `kanban_ops.py` further. Four helpers needed by the new module were promoted out of
`kanban_ops.py` (leading underscore dropped, call sites updated): `resolve_agent_factor_id`,
`validate_title`, `validate_details`, `validate_project`, `group_project_name`. The promoted
`_agent_factor_id` was renamed to `resolve_agent_factor_id` rather than kept as `agent_factor_id`
because `current_task(agent_factor_id: str, ...)` already uses that exact name as a parameter;
keeping the promoted function's name distinct avoids a same-module name collision even though the
parameter shadowing was not itself a runtime bug.

Idempotency reuses VA-404's `tool_background_operation.background_operation_id(workspace_id,
conversation_id, "boss_delegate", idempotency_key)` as the sole derivation, stored as
`operation_id` inside the task's `ktask_inbox_provenance` jsonb. Before inserting, the same
transaction checks for an existing row with that `operation_id` scoped by the target's
`located_fgroup_id` (an indexed column via `fat_group_project_finish_idx` /
`fat_group_position_idx`), mirroring `advancement_queue._find_existing_external_message`'s
"look up the explicit key before writing" shape rather than a new ad hoc scheme.

Target validation (`_validate_target_agent`) checks `flexus_agent_instance` joined to
`flexus_group` for `agent_id = target_agent_id AND ws_id = ctx.workspace_id AND
agent_archived_ts = 0 AND agent_enabled`, inside the same connection/transaction as the insert,
so a disabled-or-cross-workspace target fails closed before the advisory lock and before any
write. The advisory lock (`kanban_blocker_graph.KANBAN_ADVISORY_LOCK_KEY`, same key used by
`kanban_ops`/`kanban_board_ops`) is taken on the *target's own* group only after that group is
known, matching the existing lock-after-validate ordering used by
`v1_agent_kanban.agent_kanban_task_create`. `_lock_kanban_group` is duplicated (not imported)
into the new module, matching the established pattern of each kanban module owning its own copy
rather than sharing one (see the existing `_reviewer_is_human` / `_validate_reviewer_factor`
non-sharing note in `kanban_ops.py`).

The task is inserted with `ktask_todo_ts = now` set directly (no inbox step), which is exactly
the case `prisma/kanban_pickup_notify.sql` already fires on ("a fresh task is inserted directly
into Todo") — no new trigger or migration was needed; the existing scheduler wake-up fires
unmodified.

`boss_delegate` was registered as a new tool factory in
`langgraph_runtime/tools/domains/workspace_ops/kanban.py` (same file as `flexus_kanban`, same
`register_tool`/`tool` pattern), added to `tool_operation_policy.VOICE_BOSS_TOOL_NAMES` and
`_DIRECT_POLICIES` as `vbackground` (BACKGROUND execution mode, COMMITTING, non-cancellable —
this only affects voice-runtime classification, not how the tool's own async body runs; VA-403's
`VoiceToolOperationRuntime` confirmed BACKGROUND is purely a policy/ledger concern), and added to
Boss's `tools:` allowlist in `predefined_agents_yaml/definitions/boss/agent.yaml`, bumping
`version: 8` to `version: 9`.

## What broke

`test_tool_operation_policy.py::test_boss_allowlist_has_an_execution_declaration` and
`test_loader.py::test_boss_has_two_experts` both hardcode expectations against Boss's
tools/version; both were updated in the same change (the version assertion from 8 to 9, matching
`predefined_agents_yaml/install.py`'s sha-vs-version check that rejects a code change without a
version bump).

The guardrail engine's `symbol_from_import` rule flagged `from ...request_context import
RequestContext` in both new files — that exact import shape is already grandfathered into
`kanban_ops.py`'s baseline debt, but a brand-new file starts at baseline 0. Fixed by importing
`request_context` as a module and referencing `request_context.RequestContext`, matching the
already-clean convention in `calendar_public_ops.py` and `client_context_pin.py`.

## What was decided and why

`project_id` has no backing "project" entity in this schema — `ktask_project` is a free string,
truncated to 30 chars, the same as the existing `project` parameter on `flexus_kanban(create)`.
`boss_delegate`'s `project_id` is treated identically: validated/truncated, falling back to the
target group's `fgroup_name` when blank via the promoted `group_project_name`.

`start_mode` is accepted per the plan's signature but only `"todo"` is implemented; any other
value raises `ValueError` rather than being silently ignored, since the plan only specifies the
"immediate delegation into Todo" behavior and nothing else is built yet (YAGNI) — better to fail
loudly than pretend to support a mode that does nothing.

`reviewer_factor_id` is stored as given (trimmed, blank becomes `NULL`) with no additional
workspace-membership validation. The hard requirements this ticket names (atomicity, target
validation, idempotency, provenance, no migration) do not include reviewer validation, and
`agent_kanban_task_update`'s own `_validate_reviewer_factor` is a separate, larger check that
would be speculative scope here.

Provenance extends `_provenance_for_create`'s shape (`source`, `agent_id`, `conversation_id`,
`run_id`, `initiated_by_user_id`) rather than replacing it, adding `agent_factor_id` (Boss's own
factor, resolved via `resolve_agent_factor_id`), `delegated_by_role: "boss"`, `origin_channel`
(from `ctx.app_capture`, e.g. `"voice"` — set on voice conversations by
`voice_session_ops.py`'s `conv_app_capture`, not hardcoded, since this tool is explicitly not
voice-only), `target_agent_id`, and the derived `operation_id` used for idempotency lookups.

`boss_delegate` was added to Boss's live tool allowlist (not left registered-but-unreachable)
because the ticket's own acceptance line ("Boss atomically validates target and creates...") only
becomes true end-to-end if Boss can actually call it, and `VOICE_BOSS_TOOL_NAMES` exists
specifically to keep the declared policy set in lockstep with what Boss can invoke. No
system-prompt copy was added describing when to use it — that conversational/UX judgment belongs
to the later "ask Sidra" voice-story ticket this one is a prerequisite for.

The task backlog's ordered-milestone table (`docs/voice-agent/task_backlog.md`) was left
untouched: it is already several completed tickets behind actual landed work (VA-402 through
VA-405 have dev logs and commits but no table rows yet), which reads as a centrally-batched
update rather than a per-ticket responsibility, and it is a single shared file under concurrent
edit by other agents on this same branch right now.

## Verification

New file: `flexus_backend/tests/services/test_kanban_delegation.py`, 9 tests, all passing,
asserting: happy path assigns to the target factor (not the caller's), the returned string
contains the inserted row's id, same idempotency key replays the same task id with exactly one
insert, a different key inserts a second task, a cross-workspace target and a disabled target are
both refused with zero inserts, a blank title is refused with zero inserts, an unsupported
`start_mode` is refused with zero inserts, and the stored provenance carries
`initiated_by_user_id`, `agent_id`/`agent_factor_id`, `delegated_by_role="boss"`,
`origin_channel="voice"`, and `target_agent_id`. A tenth assertion (in the existing
`test_tool_operation_policy.py` parametrization) proves `boss_delegate` resolves to
BACKGROUND/COMMITTING/non-cancellable/declared.

```
.venv/bin/python -m pytest flexus_backend/tests/services/test_kanban_delegation.py -q
9 passed

.venv/bin/python -m pytest flexus_backend/tests/services flexus_backend/tests/langgraph_runtime -q
4250 passed, 2 skipped (unchanged pre-existing SDK-gated skips)

.venv/bin/python -m pytest flexus_backend/tests/predefined_agents_yaml -q
313 passed, 5 skipped

.venv/bin/ruff check <changed files>
All checks passed!
.venv/bin/ruff format --check <changed files>
7 files already formatted

.venv/bin/python scripts/guardrails_engine.py --check
0 new violations in any file this iteration touched (remaining reported violations are
pre-existing, in flexus_backend/experiments/voice_streaming/ and
langgraph_runtime/tools/domains/multi_agent/voice_call_agent.py, neither touched here)
```

## Next

VA-507 (Boss/Sidra handoff, return, delegation, and permission-boundary proof) depends on this
ticket plus VA-503, VA-504, VA-505, and should exercise `boss_delegate` end-to-end once the
handoff coordinator exists: duplicate-retry idempotency and disabled/cross-workspace target
refusal are already unit-proved here, but the full "ask Sidra" voice story (system-prompt
guidance on when Boss should delegate, and the scheduler picking the task up into an execution
conversation) is still open.
