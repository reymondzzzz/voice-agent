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
LiveKit room as an ordinary caller. In the room, `examples/meet_agent.py` serves one agent, Karen,
instead of the Boss / Alice / Bob call.

```bash
uv run python -m examples.meet_agent dev                                  # Karen, instead of voice_app
uv run python -m examples.meet_bridge https://meet.google.com/abc-defg-hij --name Karen
```

To try Karen without Meet, run `examples.meet_agent dev` with `examples.token_server` and open
http://127.0.0.1:8080/karen: the page's microphone takes the bridge's place and your lines are
labelled with your participant name. Karen publishes meaning-only events on the `karen` data topic —
every logged turn (speaker, text, whether it was addressed to her), her thinking/listening state,
and each background task as it starts and finishes — and `examples/web/karen.html` renders them as a
transcript, a background-work list and the orb. Checked in Chrome with a fake microphone playing
`say` clips: the state line moved through listening, thinking and speaking, the delegated task went
from working to done, the result note shows only GLM's answer, and hanging up and reconnecting
starts a clean room, at 1280x800 and 390x844, with no console errors.

To watch a real Meet call, run the bridge with a known room and open the same page as an observer:
`/karen?observe=1&room=<room>`. The token server then mints a hidden, publish-nothing token, so Karen
(who listens to every audio track in the room) never hears the page; the page plays nothing aloud and
only uses Karen's track to drive the orb. Rehearsed with the bridge on a fixture Meet page: the
observer saw Anna, Carl, Karen, the tool lines and the background result, and Karen listened only to
`meet-bridge`.

Both agent servers register without an agent name, so run one of them, not both. Karen needs
`DASHSCOPE_API_KEY` in `.env.local` next to the OpenRouter key.

Meet now turns away an anonymous guest from an automated browser before the host is even asked:
the lobby says "System info will be sent to confirm you're not a bot" and, after Ask to join, "You
can't join this video call — No one can join a meeting unless invited or admitted by the host"
(seen on a real call, September 2026). Join with a signed-in Google account instead. Sign in once in
an ordinary, non-automated Chrome, because Google refuses sign-in inside an automation-controlled one,
then hand the profile to the bridge:

```bash
open -na "Google Chrome" --args --user-data-dir="$PWD/.meet-profile" https://accounts.google.com   # sign in, then quit that Chrome
uv run python -m examples.meet_bridge https://meet.google.com/abc-defg-hij --profile .meet-profile --room voice-meet-karen --name Karen
```

The profile runs on the real macOS keychain: under Playwright's default `--use-mock-keychain` Chrome cannot
decrypt the signed-in cookies and deletes them, which silently signs the profile out after one run.
Signed in, Meet asks for no name and shows the account's name, so an account called Karen makes the
tile match what people say; `--name` is what Karen answers to either way. The bot's own tile is left
out of `meet_speaker` by Meet's `data-self-name` marker, since its name no longer equals `--name`.

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

### Silent until addressed

Karen listens to everything and answers only when spoken to. `examples/meet_addressing.py` decides
per turn; the bridge publishes its `--name` as the `meet_bot_name` attribute so the agent knows
what it is called.

| Turn | Result |
| --- | --- |
| Contains the bot's name (fuzzy, spaces ignored, Cyrillic transliterated: `Caren`, `VoiceAgent`, `Карен` match) | Answer, and engage that speaker for `FOLLOW_UP_WINDOW_S` (12s, re-armed when the bot stops speaking) |
| Engaged speaker, inside the window, anything but a filler | Answer without the name, including a one-word "Почему?" |
| Anyone else, or the engaged speaker naming another participant | Silent (`StopResponse`), and the engagement ends |
| Filler (`okay`, `thanks`, `угу`, `понятно`, `спасибо`) | Silent |

The rules only see text. A third-person mention ("like Karen said") still engages, and a follow-up
the same speaker aimed at a human without naming them is answered. The upgrade for both is a small
model that reads the last few labelled turns and returns directed/not, applied only inside the
follow-up window.

### Two models: Qwen Omni hears and speaks, GLM does the slow work

| Job | Model |
| --- | --- |
| Hear the meeting, transcribe it, answer by voice, call fast tools | Qwen Omni (`qwen3.5-omni-plus-realtime`, `voice_agent/realtime/qwen/`), `create_response` off so it speaks only when asked |
| Delegated work | `z-ai/glm-5.3` via `TaskSupervisor` |

Meet audio from the bridge goes straight into the Qwen session; its server VAD and transcription
(`qwen3-asr-flash-realtime`, part of the session) produce the text. Each transcript is labelled with
the `meet_speaker` of the moment and added to `MeetMemory`, which keeps the last
`MEET_CONTEXT_WINDOW_S` (10 minutes) and drops older turns; the full log is
`meet-transcripts/<room>.jsonl`. No notes, no summaries.

When Karen is addressed, the session prompt is replaced (`QwenOmniSession.update_instructions`, a
`session.update`) with her rules, name, running background tasks and the labelled 10-minute log, then
a plain `response.create` asks her to answer. Qwen has heard the audio itself; the log adds who said
it. Two things decided this, both found against the live endpoint:

- `response.create` with its own `instructions` makes the model stop calling tools, so the log has to
  travel in the session prompt, which keeps tools and is replaced rather than accumulated.
- Qwen keeps every utterance it hears server-side. `conversation.item.delete` is acknowledged but
  the model still repeats the deleted audio word for word, so deleting cannot bound it. Instead the
  session is renewed once it has heard a full window, at a quiet moment (nobody speaking, Karen
  silent, 1.5s): the new one starts with the 10-minute log in its prompt and takes over the audio
  before the old one closes. Qwen's context is therefore at most two windows of meeting, never the
  whole call.

| Tool | Runs | Result |
| --- | --- | --- |
| `get_current_time`, `get_current_weather` | Qwen native function call, executed inline | answered in the same reply |
| `delegate_task(goal)` | `TaskSupervisor.start_background` on GLM 5.3 with `background_brief` (who asked, the goal, the 10-minute log) | returns "started" at once; Karen says she is on it |
| `science_fact()` | a dummy background task: waits `SCIENCE_FACT_DELAY_S` (8s), then returns one of `SCIENCE_FACTS` | exercises the background path without a model |

Every tool call is logged as a `[tool]` line (`get_current_time(timezone='Europe/London') → …`), so it
shows on the page and Karen can see what she actually ran. Two prompt rules came from a live Russian
call where she invented a task: the addressed line is also sent as a message before
`response.create` (with only its audio in the session, Qwen answered every question it had heard,
including one not meant for her, and claimed to have started work for it; 3/3 runs, fixed 3/3), and
she may only say she started work if a tool call did.

A finished background answer is added to the log and spoken, once the room has been quiet for
1.5s, to the person who asked, who is then engaged again for follow-ups. While it runs, the prompt
lists it, so Karen says it is in progress rather than guessing. When anyone starts speaking over
Karen, the endpoint cancels her response and her queued audio is dropped.

Verified end to end against a local LiveKit, live Qwen and live GLM, with `say`-generated speech and
a 90s window patched in the test process only, so the session renewed mid-meeting. All transcripts
came from Qwen. Five turns of side talk got no reply. "Karen, what time is it in Tokyo?" called
`get_current_time(Asia/Tokyo)` and answered 11:26 PM, correct. After the renewal, "please check in
the background whether the deadline still works if Dmitry only starts the webhooks on October 13th"
called `delegate_task`, got "I'm on it, Carl", and GLM's answer (he would finish on the 16th; start
by the 12th) was spoken 8s later. "What was the migration deadline again?" got October 15th. Not
verified: a real Meet call, or a real 10-minute window.

`FlexusOpenRouterSTT` (used by the Boss / Alice / Bob call, no longer by Karen) raised its own
`SttError`, which LiveKit does not retry: one 15s TLS stall ended recognition for the rest of the
call. It now translates it into `APIConnectionError` (retryable unless `auth`/`invalid_request`) and
drops `invalid_audio` as an empty transcript; `tests/test_stt_recovery.py` covers it.

Meet's **Noise cancellation**, on the speaking participant's own client, trims the first 100-300ms of
speech after a pause. On a real call it turned «Карен, который час…» into «Арон, …» and "Karen, what
time…" into "And what's time…"; a second, independent recogniser heard the same, so the audio itself
was clipped before the bridge. With it turned off (Settings → Audio) the same phrases were recognised
exactly. Ask participants to turn it off, or to put a word before the name ("Слушай, Карен").

Known limits:

- The speaker is whoever was last highlighted when the turn ends, so a turn two people shared is
  credited to the later one. The upgrade is Meet's own roster: RTP contributing sources mapped
  through the `collections` data channel, which is what Attendee and MeetingBaas do.
- The speaking check and the join flow read Meet's DOM, which Google changes without notice. The
  selectors are the ones current open-source bots used in September 2026.
- Editing the turn invalidates LiveKit's preemptive generation, so the reply starts only after the
  turn is committed.
- A delegated answer can run long when spoken; the reasoning model is asked for five sentences at
  most, and Karen relays it in her own words.
- Anyone speaking cuts Karen off, including a cough.
- A session renewal and an utterance can overlap only if someone starts talking in the instant the
  new session takes over; the renewal waits for a 1.5s pause to make that unlikely, not impossible.
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
