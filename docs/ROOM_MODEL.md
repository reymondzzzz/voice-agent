# The multi-agent room

Handoff solves *one* agent at a time. A room is the harder problem: several agents and several
humans present simultaneously, able to hear each other. This is the next piece of work, and it is
explicitly out of scope in the flexus branch — its M5 exit review says group and conference
communication stays out, and its capacity model assumes one speaking agent per room.

Nothing in this document is implemented yet.

## Why a room is not more handoff

Handoff is sequential: exactly one `Agent` is active, `update_agent` replaces it, and the caller
hears one voice at a time. Turn-taking is free because there is only ever one speaker to arbitrate.

A room breaks all three assumptions at once. Presence is simultaneous, so "who speaks next" becomes
a real decision. Multiple humans means the agent must know *which* human spoke. And agents publish
audio into the same room the other agents are subscribed to, so without scoping they hear each
other.

## Two shapes

**A. One process, N personas.** Keep a single `AgentSession` and drive presence by swapping and
mixing at the application layer. Cheapest, reuses everything here, but only one agent can *speak*,
so it models a moderated panel rather than a genuine group call.

**B. N processes, one per agent.** Each agent is a real LiveKit participant with its own identity,
tracks, and session. This is the actual multi-agent room. Agents are placed with explicit dispatch:

```python
await lkapi.agent_dispatch.create_dispatch(
    api.CreateAgentDispatchRequest(agent_name="sidra", room=vroom, metadata=...)
)
```

`list_dispatch(room)` then reports every agent in that room. Because each agent is a separate
participant, per-participant tracks give speaker attribution for free.

Shape B is the goal. The personas written here are unchanged by the move — B is a deployment and
coordination change, not a rewrite.

## The floor problem

Shape B's cost, stated plainly so it is not a surprise: **N sessions means N independent VADs and
turn detectors, and nothing arbitrates between them.** Left alone they interrupt the human, talk
over each other, and — because every published audio track is subscribable — treat another agent's
TTS as user speech and answer it. That last one is a feedback loop, not a glitch.

The toolkit LiveKit provides for building the arbiter:

- `AgentSession(turn_handling=TurnHandlingOptions(turn_detection="manual"))` plus
  `session.input.set_audio_enabled(...)`, `session.commit_user_turn()`, `session.clear_user_turn()`,
  and `session.interrupt()` — explicit control over when an agent may listen and speak.
- `session.room_io.set_participant(identity)` — bind an agent's input to exactly one participant.
  LiveKit documents this specifically for multi-participant rooms.
- Participant `attributes` and the `participant_attributes_changed` event — per-participant state
  that every agent in the room can read.
- Selective track subscription, so an agent never subscribes to another agent's audio.

## Sketch to validate first

Hold the floor centrally rather than letting agents decide. One coordinator grants the floor to at
most one agent; every other agent runs with audio input disabled and speaks only when granted.
Agents are marked as agents in their participant identity or attributes, and no agent subscribes to
a participant marked that way, which kills the feedback loop by construction rather than by prompt.

Under that arrangement, the interesting decision — which agent should answer this human — is a
routing question answered from transcripts, and it belongs in a LangGraph supervisor graph rather
than in the media layer. That reuses the split already proven in
[ARCHITECTURE.md](ARCHITECTURE.md): the graph decides, the media layer commits.

## Open questions

- Does an agent whose input is bound to one human still need the full pipeline running, or can it be
  suspended cheaply between grants? Capacity depends on the answer.
- Handoff assumes one caller. What does a bounded handoff summary even mean when three humans are
  present and the summary might quote the wrong one?
- Does the floor arbiter live in the room as a participant, or outside as a service? Outside is
  easier to reason about and adds a hop to every turn.
- The flexus latency budget was measured for one agent per room. All of it needs remeasuring before
  anyone claims a room is production-viable.
