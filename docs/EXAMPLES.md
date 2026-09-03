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

## Streaming, chunking and the buffer

`FlexusSynthesizeStream` declares `streaming=True` and drives the moved pipeline directly:

- **Chunking** is the moved `VoiceSpeechStreamSegmenter`. Text deltas from the graph are buffered
  until a sentence boundary, then released as segments of at least `VOICE_SEGMENT_MIN_WORDS` (5)
  words and at most `VOICE_SEGMENT_MAX_CHARS` (160) characters; shorter fragments are merged into
  their neighbour rather than spoken alone.
- **Prefill** is the moved `VoiceTtsPrefetcher` at `VOICE_TTS_PREFETCH_SEGMENTS` (2), so while
  segment n plays, n+1 and n+2 are already being fetched.
- **Buffering** is `DrainedSegment`. Prefetching alone was not enough: httpx does not fetch a
  streamed body until it is iterated, so an unplayed prefetch only hid time-to-first-byte and the
  downloads never overlapped. `DrainedSegment` starts draining into memory as soon as the request
  opens, and playback waits for `SEGMENT_PREROLL_SECONDS` (0.75) of audio before starting a
  segment.

The cushion exists because this provider's throughput is *variable*, not slow. The same request
usually returns faster than realtime (measured 1.09–3.91s wall clock for 3.25s of audio, first byte
0.54–1.32s) but occasionally takes several times longer. Without a cushion a slow segment starves
playback mid-sentence and the output glitches. Measured through the full streaming path after
buffering: first audio at 1.15s, 4.86s of audio produced in 3.38s of wall clock, realtime ratio
0.70, zero starvation events.

## Interruption and turn taking

The session takes its thresholds from the moved
`voice_interruption_policy.VOICE_DEFAULT_INTERRUPTION_CONFIG`:

| Moved setting | Value | Wired into |
| --- | ---: | --- |
| `vprobable_speech_ms` | 100 ms | silero `min_speech_duration` |
| `vend_silence_ms` | 600 ms | silero `min_silence_duration`, endpointing `min_delay` |
| `vpre_roll_ms` | 150 ms | silero `prefix_padding_duration` |
| `vminimum_speech_ms` | 250 ms | interruption `min_duration` |
| `vutterance_end_silence_ms` | 1200 ms | endpointing `max_delay` |

`turn_detection` and the interruption `mode` are both pinned to `"vad"`. That is not a preference:
left on their defaults, livekit-agents calls `agent-gateway.livekit.cloud` for adaptive
interruption and a cloud turn detector, which returns 401 here and violates AGENTS.md rule 4.

### Microphone sensitivity

Silero alone is far more trigger-happy than the flexus pipeline, because flexus gates on PCM
energy before anything counts as speech and only commits an interruption once STT returns real
words (`voice_echo_pipeline.py:978` and `transcript_commits_interruption`). Both are wired here:

- `has_speech_energy` applies the moved `venergy_threshold` (mean absolute amplitude ≥ 500) in
  `FlexusOpenRouterSTT` before a request is sent, so room noise never reaches the provider and
  never becomes a turn. It also saves the STT spend on silence.
- A transcript that comes back as a backchannel (`mhm`, `uh-huh`, `угу`, …) is replaced with an
  empty one via the moved `transcript_commits_interruption`, so acknowledgements do not take the
  floor.
- silero's `min_speech_duration` uses the committed `vminimum_speech_ms` (250 ms) rather than the
  probable 100 ms, so brief spikes do not start a turn.

The one thing not reproduced is ordering: flexus applies the energy gate *before* VAD candidacy,
whereas here silero can still mark speech and briefly pause the agent before the gate rejects the
audio. LiveKit's false-interruption resume covers that case, and the log shows it firing as
`resumed false interrupted speech`.

`voice_echo_pipeline`, `voice_output_queue`, `voice_stream_queue`, `voice_call_actor`,
`voice_session_runtime`, `voice_livekit_pcm_sink` and `latency_trace` remain unwired too: they
belong to the flexus worker that was not moved, and `AgentSession` provides its own equivalents.

## Verified and not

Verified: 520 offline tests pass, both provider endpoints round-trip live (`fish-audio/s2.1-pro`
speech transcribed back correctly by `openai/gpt-4o-mini-transcribe` after downsampling), the
LiveKit dev keys authenticate against the local server, and `google/gemini-2.5-flash` answers over
OpenRouter.

Not verified: a full spoken call through `console` or `dev`. Nobody has run one from this repo yet.
The first person to should record what happened here, including anything that broke.
