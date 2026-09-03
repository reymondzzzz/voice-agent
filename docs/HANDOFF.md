# Agent-to-agent handoff with voice switching

The goal: the caller asks Boss for Sidra, Boss hands the live call over, and Sidra continues in her
own voice without the WebRTC session reconnecting.

## Sequence, as built

1. The caller says something like "call Sidra about the sales report".
2. Boss's graph decides to call the `call_agent` tool with `target_agent_id` and a scoped
   `handoff_summary`.
3. `authorize_handoff` validates the request and returns a `VoiceHandoffAuthorization` carrying the
   target's frozen `VoiceProfile`, or raises `VoiceHandoffRefused`.
4. On success the tool arms `PendingHandoff` and returns JSON; Boss keeps speaking, typically
   "connecting you to Sidra". On refusal it returns `Error: <reason>` and Boss stays on the call and
   explains that the transfer did not happen.
5. When Boss's speech drains — `agent_state_changed` goes `speaking` → `listening` — the runtime
   takes the pending authorization, calls `session.update_agent(sidra_agent)`, and the voice changes
   because that agent carries Sidra's own `tts`.
6. `generate_reply` instructs the target to greet the caller and confirm the scoped subject.

The same tool is on every persona, so handing back is just another handoff. There is no separate
return path and no "come back" intent recognizer here yet; the flexus branch has one, and it is
worth porting because it must work even when the specialist has no routing tool at all
(see [EXTRACTION.md](EXTRACTION.md)).

## Why the tool cannot switch the voice itself

Because `LLMAdapter` never emits `tool_calls`, a LiveKit `@function_tool` returning a new `Agent`
would never run. [ARCHITECTURE.md](ARCHITECTURE.md) has the evidence. Authorization and commit are
therefore separate, and `PendingHandoff` is the seam.

## Refusals

Every refusal is a `_require_*` helper in `handoff.py` raising `VoiceHandoffRefused` with a reason
that is safe to speak aloud:

| Reason | Cause |
| --- | --- |
| `target_agent_id is required` | empty or whitespace target |
| `target agent '<id>' not found` | unknown persona |
| `target agent is already active on this call` | handing off to the current agent |
| `handoff summary is required` | empty summary |
| `handoff summary exceeds 500 characters` | over `MAX_HANDOFF_SUMMARY_CHARS` |
| `handoff summary contains control characters` | any codepoint below `0x20` |
| `a handoff is already pending on this call` | a second handoff before the first commits |

Target ids are trimmed and lowercased, so "  SIDRA " resolves. Summaries are trimmed but otherwise
preserved.

What is deliberately *not* checked here, because this repo has no identity model: workspace
membership, group hierarchy, whether the human may access the target conversation, and whether the
requested expert exists on the target. The flexus version checks all four against Postgres. Any
port has to re-ground them on a real identity model rather than drop them —
[EXTRACTION.md](EXTRACTION.md) records this.

## Context passed across

The target agent starts with a fresh `chat_ctx` and gets exactly one thing: the bounded summary,
delivered through the `generate_reply` instruction. The source agent's system prompt and history are
not copied. This is a decision, not an oversight — copying the transcript would leak one agent's
private instructions into another, and the 500-character bound is what keeps a "summary" from
quietly becoming a transcript.

## Voice profiles

`personas.py` holds two registries. A `Persona` references a semantic `vprofile_id`; `VOICE_PROFILES`
maps that id to a provider model, voice, and speed. Provider migration edits the registry and leaves
persona identity alone. An unknown id falls back to `voice_default` rather than raising, so a
misconfigured voice degrades a call instead of dropping it.

The flexus branch extends this to four inheritance points — agent instance, blueprint, workspace,
global — with the leg freezing the resolved profile for its duration. That inheritance is worth
porting whole; the fallback-never-raises behavior here is the same invariant in miniature.
