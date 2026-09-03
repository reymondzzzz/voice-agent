# Frontend completion iteration 28: committed conversation scope and recovery

## Goal

Finish the dedicated voice page for the initial desktop Chrome pilot. A committed Boss/Sidra switch
must move every visible and actionable part of the page together, reload must recover from the same
leg, provider failure must lead clearly to durable chat, and loss of WebGL must not erase the primary
call control.

## What was tried

The existing page already used the room state to change the agent name and avatar. The same
committed `activeConversationId` now drives the message query, conversation subscription, Redux
message and streaming selectors, tool activity, pending confirmations, transcript, and chat link.
Session lifecycle mutations remain anchored to the original conversation because one durable call
owns all later legs.

The room client now exposes its validated active leg to the controller. The controller persists only
the leg sequence and opaque agent/conversation IDs beside the existing session and participant IDs;
it still stores no LiveKit URL or token. Older recovery records normalize to leg 1, and a partially
formed active-leg tuple also falls back as one unit rather than combining unrelated fields.

The provider `DEGRADED` state gets fixed localized copy and a link to the active durable chat. The
raw provider error is not rendered. The WebGL orb keeps a semantic CSS gradient underneath the
canvas and releases partial shader/program resources on every failed allocation path.

## What broke

The earlier VA-505 tests proved only header identity. A real committed handoff changed the name to
Sidra while leaving Boss's transcript, tool activity, confirmation mutation scope, subscription,
and chat link on screen. The implementation and `docs/FRONTEND.md` therefore contradicted each
other even though the focused suite passed.

Reconnect had a second presentation split. `voiceCallController.ts` hardcoded leg 1 for every token
refresh, so a browser reloading during Sidra's leg started with Boss's identity until another higher
sequence packet arrived. Persisting the last validated leg closes that window without expanding the
GraphQL token response or treating browser state as backend authority.

The first new page tests leaked `sessionStorage` between cases and accidentally entered the recovery
path with no refresh mock. Clearing the actual recovery store per test exposed the intended handoff
and degradation paths.

## What was decided and why

One committed leg is one presentation scope. Mixing an agent header from one leg with messages or an
approval from another is both confusing and unsafe, so there is no independent fallback per UI
subsystem. A lower or malformed leg still never reaches React because the room client rejects it
first.

Browser recovery data is a cursor, not durable truth. The refreshed LiveKit token and subsequent
server state still authorize and correct the call; the stored tuple only prevents the client from
rewinding its local sequence boundary during the gap.

Degradation does not auto-end the durable session and does not select an unapproved speech provider.
The user can continue in the same text conversation, while the explicit end control remains
available for closing the voice session.

## Verification

The focused voice and chat selection covers 138 tests. New cases prove that a committed handoff
switches the transcript, streaming answer, tools, subscription, approval mutation, and chat link;
that the active leg survives remount; and that degraded provider details stay hidden behind the
localized text-chat path. The orb test proves the semantic fallback remains when WebGL is absent.
TypeScript, lint, accessibility, design-token, file-size, i18n, full
frontend tests, and production build are the final landing gates for this iteration.
