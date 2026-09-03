# VA-504 iteration 26: global return routing and the actor call site

## Goal

Make "Boss, come back" work without the active specialist's cooperation, and give VA-503's coordinator
its first production caller.

## What was tried

The classifier runs in `_VoiceCallRuntime.commit_finalized_transcript` before
`persist_voice_transcript` and before any executor dispatch. That position is the requirement, not an
optimisation: a routing tool the specialist chooses to call cannot satisfy "works even when Sidra
lacks a routing tool", because a specialist without the tool would never route. Placing the check
ahead of the specialist means the specialist is never consulted, so a matched command creates no user
message in its conversation and starts no run. Both are asserted by making persistence and
`execute_run` raise if called.

The return reuses VA-502 for authorization and VA-503 for the commit, with the session's first leg
agent as the target. There is no second switching mechanism.

## What broke

The actor keeps `_vcall_leg_seq` locally to build turn IDs. Without updating it after a switch, the
next turn would carry the specialist's leg sequence and be refused by the active-leg check in
`persist_voice_transcript` — the call would survive the handoff and then reject the caller's next
sentence. `VoiceCallActor.set_active_leg` now moves it with the committed leg.

Four pyright errors shipped in the previous commit were found here and fixed: an absent-pool
dereference in `kanban_delegation`, two `ToolRuntime` parameters annotated non-optional while
defaulting to `None`, and four `.vaudit` attribute assignments onto exception classes that do not
declare it. `verify.py --stage commit` does not run pyright, which is why they landed; CI does.

Correcting the `ToolRuntime` annotations to `| None` broke a test that pinned the literal annotation.
`langgraph.prebuilt.tool_node._is_injection` recurses into unions, so the runtime is still injected —
confirmed by resolving `voice_call_agent`, `boss_delegate`, and `flexus_kanban` and observing
injection detected for all three. The test now asserts that property instead of the annotation's
spelling, so a cosmetic type change cannot fail it while a real loss of injection still does.

## What was decided and why

The classifier requires an explicit reference to the originating agent. Bare "come back" is not
enough, because unlike the confirmation classifier — which only runs while a confirmation is pending —
this one runs on every utterance of every specialist turn. A global classifier needs higher precision
than a scoped one. "Tell Boss later", "I'll ask Boss about that", and "come back later" all resolve
ambiguous and fall through as ordinary input.

Saying "come back" to the originating agent itself is a no-op rather than an error, so the phrase
stays harmless when the caller is already where they asked to be.

The return target is always the session's first leg agent, not whoever handed off most recently, so a
nested chain returns straight to Boss rather than unwinding one level at a time. That matches the
plan's "the original Boss conversation".

The return context item carries a fixed server-owned summary. The whole point of the global command is
that the specialist's model is bypassed, so there is no LLM-composed "outstanding actions" summary to
thread through, and inventing one would mean consulting the specialist the command exists to bypass.

## Known limitation

The canned speech plan reports the pre-switch conversation, agent, and workspace, while the session
state flips to the originating agent. `voice_echo_pipeline._record_run_completion` raises if a plan's
identity disagrees with the pipeline's trace authority, and that authority is still bound to the
specialist because the LiveKit `update_agent` rebind is out of scope here. The switched session is
read fresh when the reply is synthesised, so the caller already hears the originating agent's voice;
only that one turn's trace attribution stays with the specialist. This is latent rather than active —
nothing constructs the real pipeline swap yet — and it resolves when VA-505 rebinds the pipeline.

## Generic-worker violation, fixed

The return prompts named Boss directly, which `test_no_voice_runtime_source_names_a_predefined_agent`
rejects: one worker image serves every agent, so its runtime sources must not name one. It was also
simply wrong — any agent can originate a call, so a caller returning to Sidra would have been told
they were back with Boss. The prompts are now agent-neutral.

The classifier itself still keys on the word "boss", which the plan specifies verbatim and which the
generic-worker test does not scan. That is correct for the Boss-to-Sidra pilot and becomes wrong the
moment another agent originates a call; resolving it needs the originating agent's display name at
classification time, which the pure DB-free classifier does not have today.

## Verification

93 tests across the intent classifier, the return operation, and the call runtime. Seventeen positive
phrases in the languages the confirmation classifier already supports, nine negatives. The runtime
tests are constructed with no registry, checkpointer, or ARQ pool at all, which is what proves the
command needs none of the specialist's executor machinery. A refused commit leaves the specialist
active with its leg sequence unchanged and nothing persisted.

`flexus_backend/tests/services` and `flexus_backend/tests/langgraph_runtime` pass at 4349. The pyright
ratchet is back to the five pre-existing files it reported before this work began.

## Not wired

LiveKit `AgentSession.update_agent` and the media/TTS rebind. The durable state switches; the real
WebRTC session still needs the pipeline rebound to the originating agent, which is VA-505.
