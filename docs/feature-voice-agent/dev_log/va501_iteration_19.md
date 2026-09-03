# VA-501 iteration 19: stable agent voice profiles

## Goal

Give Boss, Sidra, and future agents stable provider-independent voice identities with deterministic
instance, blueprint, workspace, and global fallback behavior. The selected identity must remain
fixed for an active actor leg and survive reconnect without consulting mutable configuration.

## What was tried

The earlier plan proposed persisting provider, model, voice ID, and speed independently at every
scope. The implementation instead persists one nullable semantic profile key on agent instances,
blueprints, and workspaces. A closed server registry maps those keys to the current speech
provider configuration. Session creation resolves the active agent under the existing transaction
and stores the selected key on the first leg. The generic worker uses that frozen leg key to build
every TTS request.

## What broke

The first integration still advertised a process-level TTS model override even though it would
silently replace the new per-agent authority. That stale environment contract was removed. Review
also found that the YAML validator initially accepted generic names which the database constraint
would reject, and that clearing GraphQL overrides relied on an implicit empty-string convention.
The profile-key grammar is now consistent and bounded to 64 characters, and the clear-to-inherit
contract is explicit and tested.

## What was decided and why

`voice_boss`, `voice_sidra`, and `voice_default` are durable semantic identities. Boss and the
global default currently map to OpenRouter `hexgrad/kokoro-82m`, `am_adam`, speed `1.0`; Sidra maps
to the same model with `af_heart`. Builtin YAML carries only the semantic key, so a future move to
local TTS changes registry adapters without rewriting agent records.

Resolution is instance, source blueprint, workspace, then global. Invalid or retired stored keys
are skipped as complete candidates rather than combined with values from another scope. Agent and
workspace patch inputs accept only registered keys; an empty string clears the override to SQL
`NULL`. Blueprint refresh never copies or clears the separate instance override.

The selected semantic key is written to `vleg_voice_profile_id` before dispatch. Reconnect and
heartbeat reload that leg snapshot and cannot change the voice mid-leg. A later Boss/Sidra handoff
will create and commit a new leg before the worker switches voice; that coordinator remains VA-503.
Group or conference communication remains a future initiative outside this design.

## Verification

Focused tests cover registry validation, every fallback tier, invalid candidate fallback, YAML
installation, distinct Boss/Sidra defaults, first-leg freezing and audit, runtime reload, exact
OpenRouter model/voice/speed propagation, and the authenticated GraphQL output/patch surfaces.

## Next

VA-501 satisfies the profile prerequisite for the authorized voice-only `voice_call_agent`
capability. VA-502 still waits for VA-405, whose VA-403 and VA-303 chain remains blocked on the
physical VA-301 interruption evidence.
