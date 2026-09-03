# Iteration 3: isolated local LiveKit media stack

Integrated from `PER-8-va-101-add-pinned-local-livekit-server-and-isolated-development-configuration`.

## Goal

Make the VA-001 LiveKit server pin runnable for local signaling, direct RTC, ICE/TCP, and
TURN-required paths without adding a production service or a managed media dependency.

## What was tried

The pinned `v1.13.6` source and image were checked directly for strict configuration names,
the root health handler, embedded TURN behavior, relay defaults, and tools present in the
runtime image. The companion stack was then started with generated local credentials and a
dedicated Redis container rather than extending the shared application Redis service.

## What broke

The normal Docker client configuration blocked on a credential helper while pulling a
public image. Pulling with an empty run-owned Docker configuration proved both exact image
tags exist without changing repository configuration.

The generic `rtc.AudioSource` and production port guidance were not sufficient for this
task. The exact pinned server source shows that the root handler returns 406 until node
statistics are current, embedded TURN/UDP also supplies STUN, and development TURN relays
default to only three ports unless the range is set explicitly.

## Decisions and reasons

`compose.voice.yml` is a standalone opt-in stack, not an extension of `compose.yml`, so a
developer can operate or remove voice media without touching PostgreSQL, MongoDB, Jaeger,
or the application's Redis container.

LiveKit gets a dedicated Redis service with no host port and no persistent volume. Database
0 is isolated by container and network; database 13 remains reserved separately for future
Flexus voice state in application Redis.

The local server uses one UDP mux port and a bounded eleven-port TURN relay range. Every
published port binds to host loopback. Embedded TURN/UDP is enabled and therefore prevents
the server from adding public default STUN servers to join responses. TURN/TLS is excluded
because a trustworthy certificate and hostname are intentionally outside a localhost stack.

Credentials have no checked-in defaults. Compose refuses to start with blank key or secret,
and the ignored `.env.voice` file is the only expected local persistence point.

Health uses the pinned server's root readiness behavior rather than a socket-open check.
Prometheus is exposed on loopback and startup logs include transport ports without logging
credentials, room names, participant identities, or audio.

## Rollout and rollback

Rollout is an explicit `docker compose -f compose.voice.yml up`; no production process reads
the file. Rollback is `docker compose -f compose.voice.yml down`. There are no migrations,
volumes, runtime feature activations, or durable audio records.
