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
uv run python -m examples.meet_bridge https://meet.google.com/abc-defg-hij   # joins as «Мэгги»
```

The agent was built as Karen and is called «Мэгги» in the meeting (the code and the page still say
Karen internally). Meet's sender-side noise gate eats the start of each utterance, which is where
the name sits: over a real Meet call "Карен" came out as "Арен", "Таран", "Аарон" or nothing, and
the transcriber kept it in 2 of 5 read lines. Measured with Qwen speaking each candidate in 5
phrases in two voices, the onset cut the way the gate does (name recognised, of 10):

| Name | 0ms cut | 60ms | 120ms |
| --- | --- | --- | --- |
| Аврора | 10 | 10 | 10 ("Аурора", "Врора") |
| Юки | 10 | 10 | 8 |
| **Мэгги** | 10 | 10 | 7 ("Эгги", "Меги") |
| Хлоя | 9 | 8 | 7 |
| Лотта | 10 | 10 | 6 ("Отто") |
| Ханна | 10 | 10 | 10, but as "Анна" |
| Эмма | 10 | 10 | 2 |
| Грета | 10 | 10 | 1 ("Рита") |
| Фрида | 10 | 5 | 1 ("Ида", "да") |
| Руби | 9 | 10 | 4 |
| Люми | 10 | 10 | 3 |
| Сири | 9 | 9 | 2 ("Тери", "Ирри") |
| Джарвис | 10 | 7 | 2 |
| Карен | 10 | 6 | 4 ("Арен", "Арин", "Лен") |
| Нова | 8 | 8 | 5 |
| Зури | 7 | 7 | 7 |
| Эльза | 10 | 9 | 0 ("Лиза") |
| Тесса | 9 | 0 | 0 |

Three syllables survive best. Мэгги is the short European name that held up: what the gate leaves of it ("Эгги", "Меги") is no one else's name, where "Ханна" became "Анна" and "Грета" became "Рита". The fuzzy
match only accepts a word no longer than the name, since the gate removes sounds and never adds
them: that kept "брюки" and "юбки" from waking a bot called Юки while "руки" already fell under the ratio.
Cleaning the audio instead did not help: a high-pass, a presence boost, comfort noise in the gated
gaps, and Qwen's VAD padding or threshold all scored within noise of the untouched Meet audio
(12–18% word errors on the same 78-word script). The words lost are the ones the gate removed.

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

- **The name is the fast path.** Fuzzy, spaces ignored, Cyrillic transliterated: `Caren`,
  `VoiceAgent`, `Карен` all match, with no model call.
- **Everything else is routed inside Karen's own Qwen session**: a text-only `response.create`
  whose instructions carry the labelled last `GATE_CONTEXT_TURNS` lines and ask for one word, RESPOND
  or IGNORE. Only RESPOND is followed by a spoken response; anything else, a timeout included, is
  silence.

Keyword rules were tried first and failed on a real call: a word count dropped "Почему?" right after
Karen answered, and a filler list cannot tell "Почему?" to Karen from "Почему?" between colleagues.
The prompt also tells the model that speech recognition clips the start of utterances, so "Арон,
который час в Лондоне?" is read as a question to Karen.

How routing got here, all measured on a 9-line Russian dialogue (Kirill and Sasha talking, Karen
asked twice, "Почему?" once for Karen and once for Sasha):

| Setup | Correct |
| --- | --- |
| One session, automatic responses, prompt says "produce no output" | 2/9 on 3.5 Plus (twice) and 3.8 Flash: it answered every line |
| One session, manual mode, text RESPOND/IGNORE routing step, then speech | 9/9 on 3.8 Flash and 3.5 Plus, ~0.9s decision, ~0.9s to first audio |
| Same, routing as a `route_turn` function call | 9/9, 9/9 on 3.8 Flash; 8/9 on 3.5 Plus |
| A separate text-only Qwen judge session | 29/30 on 15 labelled lines, ~0.6s, but a second context |
| GLM 5.2 classifier | 11/11, ~0.7s, but a second model |

Karen runs the second row on `MEET_VOICE_MODEL` (`qwen3.5-omni-plus-realtime`): the routing step hears
exactly what she heard and knows what she last said, with no second session to keep in sync. Asked
to quote its own messages afterwards, the model listed only its spoken answers, not the verdicts.
Qwen 3.8 ignores the text-only request and speaks the verdict as well, about 1.1s of "IGNORE" per line
(measured), which on a real call was heard in the room; Karen's audio output is muted for the length
of every routing step. On the fixture page the five lines of side talk now put 0ms of her voice into
the call.
The session is still in manual response mode (`create_response` off, server VAD on), so VAD commits
each turn and nothing speaks until asked. One response runs at a time, so routing steps, answers and
deliveries take turns on a floor lock; a tool call's follow-up response keeps the floor.

How long a person waits, from the end of their phrase to her voice in Meet: Qwen's VAD reports the
phrase over only after `TURN_SILENCE_MS` of quiet, measured at 1.5s after the speech really ended with
1200ms and 0.9s with 600ms; then ~0.2s for the transcript, ~1s for the routing step unless her name
was in the line, 1.1–1.9s to first audio (a tool call adds a second response), and the trip back
through the bridge. The page's `total` starts at the VAD event, so it leaves the first part out.
`TURN_SILENCE_MS` is 500. From the end of the words to the transcript that took 1.0s, against 1.1s at
600ms and about 1.4s at 900. Committing the turn ourselves on local silence (Meet's gate makes the
pauses exact zeros) was only 0.1s faster, because Qwen then takes 0.5s to transcribe, and it let
noise through as lines ("Что", "так"): 27% word errors on the Meet recording against 17%. 500 splits
three Meet phrases mid-sentence ("Расскажи что-нибудь. | научный факт."), which the continuation
handling below joins again. The person she just answered, speaking
again within `FOLLOW_UP_WINDOW_S` of her reply ending, skips the routing step unless the line names
a colleague who has spoken in the meeting. For comparison, OpenAI's full-duplex GPT-Live-1 measured
~1.1s median end-of-speech to first audio in ChatGPT (Agora, n=30), and OpenAI documents it as built
for one speaker.

Her replies used to open the same formal way ("Хорошо", "Кирилл", "Сейчас"). The persona now asks for
a person's reactions in her own words, "хм", "ага", "так-так", "ой, хороший вопрос", never reused within
the meeting, and welcomes one before a tool call so the person hears her while it runs. A literal list
got parroted ("Секунду, гляну" twice in eight replies); framed as examples, eight test questions came
back with "Ой, хороший вопрос", "Так-так", "Хм" and plain answers.

Fillers come from the model, the way OpenAI's voice models do it: GPT-Live generates its own "хм" and
backchannels as part of its speech, and the gpt-realtime prompting guide asks for a short spoken
preamble before a tool call, with varied sample phrases. Clips recorded in her voice were tried in
between (the ElevenLabs / LiveKit / Pipecat way): a recording stitched onto live speech sounded like a
seam, collided with her own words ("секунду… Секунду, я ищу"), and telling her a clip had played made
her invent the fact instead of waiting for the tool in 3 of 5 follow-ups. So the persona asks for a
short reaction that belongs to the sentence ("хм, ...", "ой, хороший вопрос, ...") and never the same
one twice, a few words of her own before a background task, and nothing before time and weather: live
she added "Сейчас проверю погоду в Лондоне", so those tools are called silently and answered with the
result.

The promise check quotes the reply it asks about. Asked about "your last reply", she also weighed the
earlier ones: live, "мне нужно уточнить город" after "сейчас подберу факт, секунду" came back YES, and
the correction made her call the weather tool for a city nobody named (5 of 5 in a replay). Quoted, 20
of 20 checks were right, promises and questions back alike.

A mid-sentence pause still ends a turn: "найди новый факт… и покажи какая погода" became two turns and
she answered the first half. Qwen's `semantic_vad` (supported on the 3.5 Omni realtime models) ended
turns by meaning and read the clean recording perfectly, but on the Meet recording it still split
"на четверг. | половине четвертого" at 800ms and five phrases at 500ms, for 0.1s saved. So server VAD
stays, and a continuation is handled the way LiveKit handles a false end of turn: if the same person
starts speaking within `CONTINUATION_WINDOW_S` of the turn being heard and no tool has run yet, the
answer is cancelled and muted (or its audio dropped if it already finished), and the next line from
them is answered together with the first half, with no routing step. A line whose speaker is already
talking again by the time it is judged waits for the rest the same way; if no more comes within
`CONTINUATION_HOLD_S` (it was noise), the first half is answered alone.
The page shows such a request as one line too: the agent publishes a `merged` event and the page folds
the later fragments into the first ("а если например. Какая погода на в Лондоне." instead of three lines).

She calls people by their first name as the transcript writes it: live she chose "Кирюш" on her own,
and when "Мэгги" was heard as "Ангел" she told Kirill he was mixing her up with someone. The persona
now rules out diminutives and says a misspelt name is speech recognition, never to be remarked on;
6 of 6 replies to "Ангел, …", "Магер, …" and "Мэгги, …" then said "Кирилл" and answered the question.

Several replies in a row should sound like one answer. A result that is told while her answer still
plays, or within `GOING_ON_S` of it ending, queues right behind that answer, so its line asks her to go
on from her last sentence as part of the same answer, with no name and no fresh start: 6 of 6 replays
then ran on with "Кстати, у осьминогов…", where the plain line opened "Кирилл, у осьминогов…" 6 of 6
times. Live, "Новый факт уже ищу" followed three seconds later by "А ещё, Кирилл, бананы…" had no
connection: the fact was the thing she had promised, not one more thing. So the line now says to pick
up a promise when its result turns up and to use "а ещё" / "кстати" only otherwise; 12 of 12 replays
then went on with "О, а вот и факт: …" or "Уже нашла: …". The page folds her replies since a person
last spoke into one bubble.
The latency line of a reply that follows her own previous reply without anyone speaking between
carries `gap`: the silence the room heard between the two, 0 when the new audio queued straight behind
the old.

A review of the turn handling (GPT, offline reproductions) found four real faults, each now with a test:
- a cancelled response kept playing its late audio deltas, and reported itself completed (fixed in the
  adapter, see `docs/REALTIME_ARCHITECTURE.md`);
- quick-tool results were cleared before the follow-up said them, so a follow-up interrupted before
  its first word lost the answer; they are now kept until a completed follow-up has spoken, and queued
  otherwise. A background delivery that timed out without playing is retried instead of dropped;
- any response's completion ended the current turn. The agent now owns the response its last request
  created (`AssistantSpeechStarted`), ignores completions and text of any other, cancels the previous
  owner when it takes the floor over after `FLOOR_TIMEOUT_S`, and ends the turn itself when DashScope
  refuses a request because another response is active;
- a transcript took whoever Meet highlighted when the text arrived, 0.5-1s after the words; it now
  takes the speaker recorded when that utterance's speech stopped.
Not changed: the browser's playback queue in the bridge has no clear command, but it is fed one 20ms
frame at a time as LiveKit plays it out, so what remains there after a clear is the transport buffer,
not the answer. Relevance-aware batching of results (P2) is left for later.
A second review pass found four lifecycle gaps, each fixed with a test: a quick-tool answer counted as
told once generated rather than once played, so talking over its playout lost it (now tool results
share the delivery watcher and go back unless the reply played out); a routing step that timed out set
a global discard flag that muted and swallowed the next answer, leaving the floor locked (now the
routing response is cancelled and disowned, so its late completion is simply stale); a cancel in the
window before `response.created` hit the previous response (now held for the one being created); and
speaker attribution was a queue that one lost transcript shifted for good (now keyed by item id and
cleared when the session is replaced).

A result counts as told only by what the room heard. The sink counts the seconds of each reply it hands
to the room; at a barge-in, what is still queued was never heard, and that share of her audio maps onto
the reply's transcript ("В Токио сейчас 23 градуса…" cut after 0.4s of 2.4s is "В"). The result goes
back to the queue with those words, and the retelling tells her where she was cut off, so she says only
what was not heard: after "В То" 3 of 3 replays retold the weather, after "В Токио сейчас 23 градуса" 3 of
3 said just "в Токио сейчас ясно". DashScope has no `conversation.item.truncate`, so this line is also
what tells the model its reply did not get through. The retelling first also invented the still-running
fact 4 times in 6 ("some wasps can recognise faces"); told to say only what the results hold, 1 in 9.
A result already cut off once is not folded into the next answer: live, folded into "давай новый факт",
the model started the fact search, skipped the talked-over weather, and it counted as told. It gets its
own retelling right after that answer instead. The page marks a talked-over reply with how far she got.
Playout is tracked per reply. Each response she owns gets a number and ends up played or cut; a result's
watcher waits for the reply numbered right after its request, and only that one. With one shared
"played" flag, "сейчас гляну" playing out before the tool answer marked the answer as heard, and talking
over the answer then lost the result.

The line is logged the moment it is heard and judged off the event pump, so waiting for the gate
never delays barge-in; the page gets a separate `addressed` event and tags the line then.

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

A finished background answer is added to the log and put on a delivery queue that a single worker
drains, always after anything Karen was asked directly. Everything waiting is told in one turn, so two
facts asked back to back come out as one flowing answer ("Bananas are slightly radioactive… Also, a
day on Venus…") instead of two answers with a pause between; only people's silence counts as the
pause, so Karen may go straight on after her own sentence. Asking for the same delegated check again
replaces the first; asking for another science fact does not, which on the first try silently
cancelled the first fact. Qwen 3.8 often says "I'm on it" in the same response that starts background work,
so a background tool's result is returned without asking for another spoken response when something
was already said; otherwise it acknowledged twice. Each waits for `QUIET_BEFORE_DELIVERY_S` (3s) of
quiet. One that someone talks over goes back to the front and is finished at the next pause, after
Karen has answered the interruption if it was for her. That was a real-call bug: interrupted mid-fact
by "Какая погода в Лондоне?", she answered the weather and dropped the fact. Rehearsed on the fixture
page: interrupted by "Karen, what's the weather in London?", she answered it, then said "So, about
that science fact, octopuses have three hearts…". If Karen answered last she still holds the floor: the queue does not wait for the people's pause and
starts the next reply as soon as her answer has finished generating, so its audio queues right behind
the answer. Measured on the fixture page with a fact landing during a weather question: 11s of
silence before this change, 1.8s after dropping a redundant sustained-quiet window, 0.8-0.9s now.
Results already waiting when someone asks her something are folded into that answer. Told separately,
she first said "I have not got the fact yet" with the fact waiting, then told it in a second reply. An
early 3.8 Flash test skipped the weather tool when asked to do both; on 3.5 Plus, with the line saying
to deal with the question first, calling its tool if it needs one, 10 of 10 answers (weather, time,
arithmetic, small talk, "what about the fact?") called their tool and ran on into the result in one
breath ("…14 градусов и небольшой дождь. Кстати, насчёт того научного факта: …"). The promise check is
skipped while work runs or a result waits: "I'll tell you when it's ready" was judged a broken promise
and started a second fact search.

**Tools carry their own weight** (`examples/meet_tools.py`). Each `MeetTool` is LIGHT or HEAVY, and the
agent dispatches on that alone: `get_current_time` and `get_current_weather` are light and answered inside
the reply; `research` (GLM 5.3 with the 10-minute log) and `science_fact` are heavy, always run on the
`TaskSupervisor`, and are told when they finish. A heavy tool's schema tells the model it returns at once,
so it says it is on it; which tools are heavy is the tool's decision, not the model's.

**No fillers.** Short fillers in Karen's voice ("Секунду.", played the moment she was addressed) were tried
and removed: on a real call they sounded strange rather than responsive.

**Identity first.** The rules open with who Karen is: a woman (feminine forms, "я рада"), the one people
call Karen. Buried deeper, she answered "Карен, как дела?" with "Привет, Карен!" and spoke of herself in the
masculine. Two tiles carrying one name (the bot signed in as a participant's own account) no longer
produce "Kirill Starkov, Kirill Starkov" as the speaker.

**Why 3.5 Plus, not 3.8 Flash.** On a real call 3.8 Flash called a tool once and then only promised
("сейчас подберу ещё один", "сейчас посмотрю погоду") without calling any. Replaying that call's exact
sequence (fact, its delivery, "А ещё какой-нибудь факт?", "Какая погода в Лондоне?"), 3.8 Flash called
the right tool in about half the cases whatever the prompt held, and once told a fact from memory; 3.5 Plus
called it 20/20. The session prompt now lists only the people's lines: with Karen's own "сейчас найду"
listed as text beside them, 3.8 Flash imitated the promise (1/3). Routing still gets the full labelled log.

**Recognition quality: the audio path, measured.** A real call was recorded at two points while 12 known
Russian phrases were read: A, the audio as the bridge received it from Meet, and B, exactly what Qwen got.
Replayed into a listen-only Qwen session and scored against the text, A had 9-10% word errors and B
19-21%: our path doubled them. Pushing A back through the local LiveKit per variant (two runs each):

| Path | Word errors |
| --- | --- |
| as it was: DTX on, default bitrate, received at 24kHz, resampled to 16kHz | 19%, 19% |
| DTX off, 64kbps | 21%, 17% |
| received directly at 16kHz | 17%, 12% |
| both | 9%, 10% |

So the bridge now publishes with DTX off at `MEET_PUBLISH_BITRATE` (64kbps), and the agent takes the
bridge audio straight at `HEARING_SAMPLE_RATE_HZ` (16kHz, Qwen's input rate). Recognizer hints were tried
on the same data and left out: DashScope accepts `corpus` and `prompt` in `input_audio_transcription`;
`prompt` changed nothing; a `corpus` listing the script's own words cut errors to 6%, but that vocabulary is
not known before a real meeting, names alone did not help (10%, 14%), and a bare names list was copied into
the transcript verbatim ("Карен, Кирилл Старков" for "Кирилл, ты отправил отчёт…"). A VAD pre-roll of
500ms and a 0.3 threshold changed nothing. The recordings also showed two ~30ms digital dropouts inside the
bridge's own capture, before LiveKit, in two of the worst phrases; the page's ScriptProcessor running on
Meet's busy main thread is the suspect.

**A turn, not a response.** Karen's reply to a line can take several Qwen responses, so the agent tracks
the turn. Tool results go back without asking for a response each: when Qwen called two tools in one
response, the second request collided ("Conversation already has an active response") and speech that
arrived meanwhile was never transcribed. One follow-up is asked for when the tool-call response ends
(none for heavy work she already announced). A request cut off before any reply or tool call, because
someone started talking, is asked again once at the next pause instead of being forgotten; a reply
that called no tool gets a silent YES/NO check for an unstarted promise ("я начала проверку" without
`research`), and a YES tells her to call the tool; a spoken reply that begins with a routing verdict
("RESPOND" was once heard) is muted but not cancelled: Qwen says the verdict and then calls the tool in
the same response, and cancelling it dropped the call, so the retry leaked again (a live tool turn took
12s through 12 retries). Only a response that called no tool is asked again. A light tool result
whose follow-up never came because someone started talking joins the delivery queue instead of being
dropped (a live time lookup was lost to a stray "Вин."). And the follow-up that speaks a tool result takes
along whatever is already waiting in that queue, so "weather in London" plus a finished fact is one answer,
not two back to back. Time and weather results are marked as
valid only at that moment, after a live answer repeated a Tokyo time from eleven minutes earlier without
calling the tool (not reproducible in a short replay, 5/5 called there either way). Rehearsed: "Карен,
какая погода в Лондоне, и расскажи научный факт" got both tools, one spoken answer and the fact later;
"Карен, который час в Токио?" talked over by Anna was answered once she finished. Known gap: the speaker
label is read when the transcript arrives, so a line followed quickly by someone else can be credited
to them.

**Bridge capture is not the dropout source.** The ~10-30ms runs of digital silence in the recordings were
checked against the page's ScriptProcessor: with a continuous tone and the page's main thread blocked
150ms in every 300, and then 500ms in every 700, the bridge delivered 0 gaps of 3ms or more. Chrome
queues the capture input rather than dropping it, so the silences arrive from Meet and an AudioWorklet
rewrite would not change them.

**One language.** `MEET_LANGUAGE` pins Qwen's transcription (`input_audio_transcription.language`, which
DashScope accepts): unpinned it wrote Russian speech as Polish ("karol daj proszę…") and Chinese, and
Karen then answered the garbled line from her own knowledge instead of calling the tool. The rules tell
her to always speak `MEET_LANGUAGE_NAME`. Pinned to Russian, English speech is transcribed as Russian.

**Barge-in is local.** Qwen's VAD reports speech only after a round trip to the endpoint, and Karen kept
talking meanwhile. The agent now watches the bridge audio itself: a person above `BARGE_IN_DBFS` for
`BARGE_IN_S` (150ms) while her audio is queued clears it and cancels her response, and counts as human
speech until Qwen reports its end. Measured on the fixture page: Karen still audible 0.65s after Anna started talking. Without that last part the delivery queue re-sent a talked-over result
into the speech, Qwen cancelled that response without finishing it, and the floor stayed locked.

**One session, kept alive.** The 10-minute renewal is gone: the session accumulates the whole meeting
(audio, routing, replies) until DashScope closes it, which it does after "no response was generated for
300 seconds" (its words). A silent one-word keepalive response goes out after `KEEPALIVE_S` (240s) without
one, so a quiet stretch no longer costs Karen her context. If it is closed anyway it reopens seeded with
the text log.

Every Karen reply logs `latency heard=… decide=… voice=… total=…` and the page shows it under the
line: `heard` is VAD end of speech to transcript, `decide` transcript to decision (0 when she was named,
the routing step otherwise), `voice` request to the first audio chunk, `total` end of speech to first
audio. VAD declares the end of speech `TURN_SILENCE_MS` after the person stops, so add that for the
wait a person actually hears. On the fixture page: total 1.1-2.2s after the VAD end (the longest with a
tool call), heard ~0.2s, voice ~1.0s.

Qwen's server VAD waits `TURN_SILENCE_MS` (1.2s)
before ending a turn, so a pause mid-sentence is not taken as the end of a question. Also rehearsed:
"Why is that?" after a fact got an answer; "Anna, can you send me the report?" and "How are you doing
today?" got silence. While it runs, the prompt
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

DashScope closed Karen's Qwen session exactly every five minutes on a real call (20:09, 20:14, 20:19, …,
while nobody was speaking; whether that is an idle timeout or a hard cap is not known). `pump_events`
reopens it seeded with the 10-minute log, but the first close caught the audio pump mid-write, and
`ClientConnectionResetError` killed it: every later session was healthy and heard nothing.
`MeetCall.forward_frame` now drops the one frame that hits a closing session instead;
`tests/test_meet_agent.py` covers it.

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
