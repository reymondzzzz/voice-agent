# VA-301 iteration 15: client processing compatibility and physical echo evidence

## Goal

Replace the assumption that requested browser audio processing is effective with a privacy-safe,
client-visible compatibility report and a physical speaker-to-microphone echo test. Desktop Chrome
must be classified from observed settings and acoustic evidence before the initial pilot calls its
speaker mode supported. Safari and Firefox remain recorded but are deferred from this pilot.

## What was tried

The existing post-capture AEC, noise-suppression, and automatic-gain settings were projected into a
sanitized browser/platform report. Raw user-agent strings, device IDs, device labels, audio, and
transcripts never enter the report. Effective settings alone remain unverified because a Boolean
track setting proves selected configuration, not real acoustic attenuation.

A local-only physical probe now requests two isolated microphone trials, first with AEC disabled
and then enabled. Noise suppression and automatic gain are disabled for both trials. It plays a
short deterministic speech-band signal, measures only in-memory RMS, closes both audio contexts,
stops every track, and returns only observed settings plus attenuation. The initial provisional
pass threshold is 12 dB with a baseline at least 6 dB above background; VA-305 owns later tuning.

The prototype UI runs the probe only from a user gesture while no LiveKit call is active. It warns
about the short sound and local-only processing, serializes probe versus call start, and combines
the physical result with the real call track settings. A pass can produce Supported, a measured
failure produces Degraded, and missing or inconclusive evidence remains Unverified.

A bounded review found that UI serialization alone did not protect another caller of the probe
module. The probe runner now owns one shared in-flight operation, so concurrent callers receive the
same result instead of opening competing microphones and contaminating the acoustic comparison.
Deterministic tests prove track, graph-node, oscillator, and audio-context cleanup after success and
after a sampling failure.

## What broke

Headless browser automation cannot prove acoustic coupling or real WebRTC DSP behavior. It can
prove constraint requests and reporting only. The repository browser harness also launches only
Chromium, and the current Mac has Chrome and Safari but no Firefox installation.

The approved physical run used the exact production probe from a local Vite page. Chrome produced
one warm-up result with an insufficient baseline, followed by three valid passes at 15.32 dB,
14.95 dB, and 17.58 dB attenuation. Each valid run reported AEC disabled for the control, enabled
for the processed trial, and isolated noise suppression and automatic gain control. This closes
Chrome's built-in-speaker measurement. The headphone weak-coupling control was not run.

Safari completed two trials with the requested AEC toggle but could not prove processing isolation:
its track settings did not expose both noise suppression and automatic gain control as disabled.
The apparent third-run hang was Safari leaving `getUserMedia()` pending while the localhost
microphone permission remained Ask; a stage-only local diagnostic proved neither audio processing
nor context cleanup was pending. After the already-approved browser permission was set to Allow,
the unchanged production probe completed three consecutive trials promptly. All three were
correctly inconclusive, so Safari remains unverified because of the settings capability gap, not a
probe lifecycle failure. The temporary diagnostic page and its timing data were not retained.

## Decisions and why

The initial pilot is desktop Chrome only. Safari and Firefox move to a later compatibility
expansion; their current evidence remains in the matrix so the missing work is visible. iOS Safari
and Android Chrome also stay outside the pilot until their own physical-device runs pass; viewport
emulation is not mobile audio evidence.

The user explicitly accepted the three valid Chrome built-in-path trials as sufficient for this
initial milestone and deferred the headphone weak-coupling control. The matrix records the control
as deferred, not passed, so a later compatibility expansion can still close that evidence gap.

An enabled `MediaTrackSettings.echoCancellation` value is configuration evidence, not speaker-mode
support. This follows the browser contract: settings report the selected constraint, while actual
echo reduction depends on the device, output path, room, and browser DSP. The compatibility label
therefore requires both effective call settings and a physical pass.

## Verification so far

```text
Focused compatibility, echo-probe, and UI tests: 24 passed
Full frontend tests:                              1084 passed
Changed-file ESLint:                              passed
Full frontend lint and design/a11y ratchets:       passed
Frontend typecheck and GraphQL generation:        passed
Frontend production build:                        passed
Backend structural guardrails:                    passed
Initial Chrome pilot matrix:                        complete for approved scope
Chrome built-in speaker/microphone:                 3 valid passes
Safari built-in speaker/microphone:                 3 inconclusive; deferred
Firefox physical evidence:                          deferred
Chrome headphone control:                           deferred; not run
```

## Next

Proceed to VA-303. A later compatibility expansion should run the Chrome headphone control, decide
how Safari's missing processing-isolation evidence is handled, and install Firefox for its own
physical trials.
