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

The LLM is `z-ai/glm-5.2` over OpenRouter, chosen because it emits tool calls reliably through
`LLMAdapter` (measured: Boss decides a handoff in ~5s, Bob answers with a tool round trip in
~10s). Set it in `examples/voice_app.py` as `EXAMPLE_LLM_MODEL`.

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
   `handoff_summary` carries the caller's *actual question*, not a topic, because the receiving
   agent answers it directly: asking Boss for the time in Tokyo hands Bob
   `"What time is it in Tokyo?"`, and Bob calls `get_current_time` and answers without greeting
   or asking again.
2. **Commit, in the media layer.** `ExampleCall.on_agent_state_changed` waits for
   `agent_state_changed` to go `speaking` → `listening` — the drained speech boundary — then calls
   `update_agent` with the target and publishes `active_agent_id` as a room attribute so a client
   can follow the switch.

The target's answer is produced from `PersonaAgent.on_enter`, not from a `generate_reply` issued
straight after `update_agent`: the swap leaves speech scheduling draining, and a reply queued in
that window is cancelled with `cannot schedule new speech`, which left the incoming agent
silent. `on_enter` runs once the new agent is actually active.

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

## Tool results must not be spoken

`SpokenOnlyGraph` wraps every persona graph. livekit's LangGraph adapter discards the metadata
that says which node produced a token and turns every message into speech, so a tool's return
value is read aloud before the agent's own sentence — the caller hears
"Friday 04 September 2026, 02:16 in Asia/Tokyo" and then "It's 2:16 AM in Tokyo", and for a
handoff the raw authorization string. Filtering `ToolMessage` out of the graph's own stream is the
narrowest fix: the model still sees every tool result in its context, the microphone path is
untouched, and the adapter is used unmodified.

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

`resume_false_interruption` is off: when the caller speaks the agent stops for good rather than
pausing and finishing its sentence afterwards. `turn_detection` and the interruption `mode` are
both pinned to `"vad"`. That is not a preference:
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

## The orb

`examples/web/orb.js` renders a procedural WebGL orb — no CSS gradients, blurred divs or scale
pulses. The GLSL is flexus's own `voiceOrbShader.ts` (simplex noise, FBM, domain warping, fresnel
rim, iridescence, tone mapping) with the lighting reworked; the flexus original stays untouched at
`flexus_frontend/src/components/ui/voiceOrbShader.ts` if you want to diff.

Three bugs had to be fixed before it looked like anything:

- `halo = exp(-3.6 * max(r - radius, 0.0) / …)` evaluates to **1 everywhere inside the body**, and
  `outc = col * body + haloCol` then floods the whole interior with flat rim colour. Multiplying
  the halo by `(1.0 - body)` is what turned a khaki disc into a translucent bead.
- `flow = 0.5 + 0.5 * h` was unclamped while `h` regularly exceeds 1, so `pow(flow, n)` blew up
  into a uniform wash. It is clamped now.
- `uIntensity` scaled brightness *and* silhouette deformation (`0.075 * h * uIntensity`), so
  brightening the orb turned it into a potato. Deformation now has its own budget and follows a
  low-frequency field (`field(p * 0.30)`) plus bass, so loud audio swells it roundly instead of
  growing fur along the rim.

Measured while iterating: the interior read 78–94 flat across the body before these fixes, against
181 at the rim.

### States

Each state interpolates its parameters with a half-life rather than restarting, so transitions are
continuous and interruptible — verified: glow moves 0.850 → 0.867 in one frame toward a 1.300
target rather than snapping.

| State | Motion language |
| --- | --- |
| `idle` | slow breathing, low deformation, amplitude floor so it never dies |
| `listening` | responsive surface, brighter rim, full audio gain |
| `thinking` | larger softer features (low noise scale), deep warp, slow clock, internal swirl |
| `speaking` | strongest reaction, widest halo, agent audio drives it |
| `error` | dark red, calm, no audio reaction |
| `permissionDenied` | amber-grey, dimmer still, distinct from `error` |

`GET /static/orb-states.html` renders all six side by side with synthetic audio — the fastest way
to review the family after a change. `window.voiceOrb` exposes the live instance for probing.

### Audio and performance

Bands come from the moved `voiceAudioLevel` port: amplitude drives deformation and glow, bass the
large slow swell, mid the interior light, treble fine shimmer. Nothing updates page state per
frame; everything lives in shader uniforms and mutable render-loop values, and the parameter key
list is hoisted so `render()` allocates nothing.

Measured at 1440×900: **120 FPS mean, 8.30 ms median, 10.20 ms p95**. Device pixel ratio is capped
at 2 (1.5 under 760px wide), rendering stops on `visibilitychange`, WebGL context loss is caught
and the program rebuilt on restore, and `prefers-reduced-motion` slows the shader clock to ~11% of
normal rather than freezing it. `pagehide` disposes the program, buffer and audio meter.

## Google Meet

`examples/meet_bridge.py` puts the agent into a Google Meet call. Meet has no API a bot can speak
through, so a Playwright-driven Chrome joins as a guest and the bridge carries that page into a
LiveKit room as an ordinary caller. The agent code does not know Meet exists.

```bash
uv run python -m examples.voice_app dev                                   # the agent, as usual
uv run python -m examples.meet_bridge https://meet.google.com/abc-defg-hij --name "Voice Agent"
```

The bot asks to join; someone in the call has to admit it. It leaves when the call ends or on
Ctrl-C. Chrome runs headed by default because every maintained Meet bot does — Meet treats
headless guests with suspicion. `--headless` exists for when that stops being true. Meeting audio
plays out of the local speakers while it runs; `--mute-audio` would silence it but also stops Chrome
rendering Web Audio, which kills the capture.

| Direction | How |
| --- | --- |
| Meet → agent | `examples/meet/bridge.js` wraps `RTCPeerConnection`, mixes every remote audio track in a 48kHz `AudioContext`, and hands 20ms PCM frames to Python through a Playwright binding. The bridge publishes them as its microphone track. |
| agent → Meet | The bridge subscribes to the agent's track and pushes 20ms frames into the page. `getUserMedia` is replaced so Meet's microphone is a stream fed from those frames. |
| who is speaking | Every 250ms the page reads the participant tiles (`div[data-participant-id]`, name in `span.notranslate`) and reports those whose speaking border is visible. The bridge drops its own name and publishes the rest as the `meet_speaker` participant attribute. |

Only meaning crosses into the agent: the speaker name is an attribute, never audio (rule 14).
`PersonaAgent.on_user_turn_completed` reads it and prefixes the turn, so the graph sees
`[Anna] What time is it in Tokyo?`. Overlapping speakers come through as `Anna, Bob`.

### Silent until addressed

In a meeting the agent listens to everything and answers only when spoken to.
`examples/meet_addressing.py` decides per turn; the bridge publishes its `--name` as the
`meet_bot_name` attribute so the agent knows what it is called.

| Turn | Result |
| --- | --- |
| Contains the bot's name (fuzzy, so `jarvys` still matches `Jarvis`) | Answer, and engage that speaker for `FOLLOW_UP_WINDOW_S` (12s, re-armed when the bot stops speaking) |
| Engaged speaker, inside the window, two words or more | Answer without the name |
| Anyone else, or the engaged speaker naming another participant | Silent, and the engagement ends |
| Backchannel (`okay`) | Silent |

A silent turn raises `StopResponse`, which LiveKit treats as "drop this turn", so it is appended to
the agent's `chat_ctx` first: when someone finally asks, the graph has heard the whole discussion.
Every turn, the bot's included, is appended to `meet-transcripts/<room>.jsonl` as
`{ts, speaker, text}`.

The rules only see text. A third-person mention ("like Jarvis said") still engages, and a follow-up
the same speaker aimed at a human without naming them is answered. The upgrade for both is a small
model that reads the last few labelled turns and returns directed/not, applied only inside the
follow-up window.

Verified end to end against a local LiveKit and the real agent, with `say`-generated speech played
through a fixture page whose speaking tiles switch between Anna and Carl: side talk got no reply,
"Voice Agent, what time is it in Tokyo?" and the unnamed "And what about London?" were answered,
and Anna turning to Carl silenced it again. Asked "who sent the quarterly report?" after two silent
turns, Boss answered "That was Anna". STT wrote the name as `VoiceAgent`, which is why names are
compared with spaces removed. A handoff still starts the target with a fresh `chat_ctx` (rule 9),
so room context gathered before a transfer does not follow it. Not verified: a real Meet call.

Known limits:

- The speaker is whoever was last highlighted when the turn ends, so a turn two people shared is
  credited to the later one. The upgrade is Meet's own roster: RTP contributing sources mapped
  through the `collections` data channel, which is what Attendee and MeetingBaas do.
- The speaking check and the join flow read Meet's DOM, which Google changes without notice. The
  selectors are the ones current open-source bots used in September 2026.
- Editing the turn invalidates LiveKit's preemptive generation, so the reply starts only after the
  turn is committed.
- `RTCRtpReceiver.createEncodedStreams` is deleted before Meet loads; with it present Meet decodes
  audio in its own worklet and the receiver tracks go silent.
- The bridge mints its own token with `can_update_own_metadata`. Without it LiveKit refuses the
  attribute update with `NOT_ALLOWED` and the Python SDK does not raise, so names silently vanish.

Verified: `tests/test_meet_bridge.py` (marked `integration`, needs a LiveKit server and Chrome)
runs the bridge against a local page with real WebRTC — a 440Hz remote track arrives in the room
at 440Hz, a 660Hz agent track arrives on the page's microphone at 660Hz, and a highlighted tile
becomes `meet_speaker`. End to end with the real agent and a spoken question on that page, the
graph received `[Anna] Hi, what time is it in Tokyo right now?`, Boss handed off to Bob, and Bob's
answer played back into the page's microphone. Not verified: a real Meet call. The join flow and
tile selectors have not been exercised against meet.google.com.

## Verified and not

Verified: 520 offline tests pass, both provider endpoints round-trip live (`fish-audio/s2.1-pro`
speech transcribed back correctly by `openai/gpt-4o-mini-transcribe` after downsampling), the
LiveKit dev keys authenticate against the local server, and `google/gemini-2.5-flash` answers over
OpenRouter.

The orb was verified live during a real call: state derived as `listening`, real bands flowing
(bass 0.96, amplitude 0.178), no console errors, and the transcript populated.

Not verified: `console` mode, and the orb on a real mobile GPU — it was checked at a 390px
viewport on desktop hardware, not on a phone.
