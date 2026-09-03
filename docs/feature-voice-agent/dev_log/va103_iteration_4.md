# Iteration 4: realtime browser room client

Integrated from `PER-10-va-103-add-the-realtime-browser-room-client-and-capture-settings`.

## Goal

Add the browser-owned LiveKit room lifecycle and microphone capture contract needed by
the local media loop: join and leave, continuous microphone publication, agent-audio
playback, requested AEC/noise suppression/automatic gain control, and honest effective or
degraded device state.

## What was tried

The client was built directly against the pinned `livekit-client@2.22.1` API rather than
overloading the upload-oriented recorder. A focused harness exercises room events,
microphone capture and publication, remote audio attachment, autoplay refusal and
recovery, microphone reacquisition, publication failure, explicit leave, and remote
disconnect without a provider or raw-audio fixture.

## What broke

The first raw TypeScript invocation ran before the ignored GraphQL client existed and
therefore reported repository-wide missing generated imports. The supported `pnpm
typecheck` path regenerated Prisma and GraphQL artifacts first and passed. The first test
run also used the Node Vitest environment, so a minimal typed audio-element seam replaced
an unnecessary document dependency.

The exact browser SDK resolves `machina@7.0.1`, whose package metadata requires Node
`>=22.22`, while the repository pinned Node 22.21.1 when this client landed. Import and
typecheck succeeded on that pin, so it did not block the browser change; the repository
then aligned its supported floor and known-good pin in the separate toolchain issue.

## Decisions and reasons

The room client accepts a URL and token but does not fetch, persist, refresh, or interpret
them. Session authorization and token minting stay in the backend control-path lane, and
the browser package contains no LiveKit secret.

The microphone is created and published exactly once per join and remains active while
remote audio is attached. Agent playback does not drive microphone mute state. This keeps
the later barge-in path possible without adding VAD or interruption behavior early.

Requested constraints are not reported as effective constraints. The client reads the
source track settings after capture and after every LiveKit track restart, crosses them
with `getSupportedConstraints()`, and degrades when a feature is disabled, unsupported,
or absent. The diagnostic projection deliberately drops device and group identifiers.

Remote audio uses LiveKit track attachment and explicit detachment. Autoplay refusal does
not tear down a healthy call: it becomes a blocked playback state and the UI can call
`resumeAudio()` from a user gesture. Join and publication failures stop capture and close
the room before the original failure returns.

## Rollout and rollback

No route or production call site activates the client yet. Rollout is limited to importing
the client from the future feature UI and supplying the authenticated short-lived room
contract. Rollback removes the `voice/` frontend module and exact npm dependency/lock
entries; there is no migration, durable audio, server secret, or background process.
