# The example agents

Three small LangGraph agents on the moved pipeline. Boss answers first and routes; Alice and Bob
each own one tool. Any of them can hand the call to any other, including back to Boss.

| Agent | Voice profile | Speed | Tool |
| --- | --- | ---: | --- |
| Boss | `voice_boss` | 1.00 | none — routes with `call_agent` only |
| Alice | `voice_alice` | 1.08 | `get_current_weather` (placeholder data) |
| Bob | `voice_bob` | 0.92 | `get_current_time` (real, IANA timezones) |

All three use `fish-audio/s2.1-pro` with voice `alloy`, because that is the only voice id the
provider accepts — every other id returns 400, which is why the moved `VOICE_PROFILE_REGISTRY`
pins alloy on every profile too. Speed is therefore the only per-agent audio difference until a
provider with real voice ids is wired. `get_current_weather` returns invented readings and says so
in its output, so the agent tells the caller it is placeholder data rather than presenting a
forecast.

## Running

```bash
cp .env.example .env.local     # fill in FLEXUS_VOICE_LIVEKIT_* and OPENROUTER_API_KEY
uv run python -m examples.voice_app console   # terminal audio, no LiveKit server needed
uv run python -m examples.voice_app dev       # joins a room on FLEXUS_VOICE_LIVEKIT_URL
```

Start with `console`: it exercises the graphs, the tools, and the handoff commit without any media
infrastructure, so a broken handoff is isolated from a broken room.

Say "what's the weather in London" and expect Boss to call `call_agent`, keep talking, and only
then hand over to Alice. Say "boss, come back" while Alice or Bob is active and the call returns to
Boss even though neither has a routing tool for it — that phrase is matched by the moved
`voice_return_intent.classify_voice_return_command`, which also recognizes Spanish and Russian
forms.

## How the handoff works

`langchain.LLMAdapter` only ever emits assistant text, never `tool_calls`, so the ordinary LiveKit
pattern of returning a new `Agent` from a `@function_tool` cannot fire when a graph is the LLM.
The handoff is therefore split, and `PendingHandoff` is the seam:

1. **Authorize, in the graph.** `call_agent` validates the request and *arms* the pending handoff,
   returning text to the model. A refusal returns `Error: <reason>` and nothing is armed.
2. **Commit, in the media layer.** `ExampleCall.on_agent_state_changed` waits for
   `agent_state_changed` to go `speaking` → `listening` — the drained speech boundary — then calls
   `update_agent` with the target and publishes `active_agent_id` as a room attribute so a client
   can follow the switch.

The voice changes because each agent carries its own `tts=FlexusOpenRouterTTS(profile)`. The target
starts with a fresh `chat_ctx` and receives only the bounded summary, matching the decision in
`docs/voice-agent/implementation_plan.md` not to copy history across a handoff.

The return path is the same machinery driven from `user_input_transcribed` instead of a tool call,
which is what makes it work regardless of the active agent's tools.

## What the examples reuse rather than reimplement

`examples/livekit_providers.py` is deliberately thin. `FlexusOpenRouterTTS` builds a
`VoiceTtsRequest` and hands it to the moved `open_openrouter_pcm_stream`, which validates the
provider's declared PCM layout and resamples 44.1kHz down to the 24kHz room rate on its own; the
adapter only forwards bytes into LiveKit's emitter. `FlexusOpenRouterSTT` delegates to
`voice_stt.OpenRouterSttProvider`, which wraps PCM into WAV, bounds the utterance, and parses usage.

## Verified and not

Verified: 520 offline tests pass, both provider endpoints round-trip live (`fish-audio/s2.1-pro`
speech transcribed back correctly by `openai/gpt-4o-mini-transcribe` after downsampling), the
LiveKit dev keys authenticate against the local server, and `google/gemini-2.5-flash` answers over
OpenRouter.

Not verified: a full spoken call through `console` or `dev`. Nobody has run one from this repo yet.
The first person to should record what happened here, including anything that broke.
