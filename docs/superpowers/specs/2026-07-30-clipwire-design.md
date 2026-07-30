# ClipWire — design

**Date:** 2026-07-30
**Status:** batya-reviewed, awaiting owner approval

## Problem

Two machines, one clipboard:

- **Mac** — macOS Sequoia 15.7.4, LAN `192.168.1.34`
- **PC** — Ubuntu 25.10, GNOME/Wayland, LAN `192.168.1.45`, **rebooted daily**

Copy on one, paste on the other. No GUI. Two devices only.

Existing tools were evaluated and rejected:

- **ClipCascade** — self-hosted server plus clients. Server sessions are in-memory
  (`spring-session-core` with no jdbc/redis backend) and die on every server restart,
  i.e. daily. A running client never recovers: its reconnect gets `302 → /login` and
  fails on `scheme https is invalid` forever, because `authenticate_and_connect` runs
  only at process start. Auto-login from saved credentials is gated on
  `cipher_enabled == False`, and with encryption off `Config.save()` raises
  `Object of type bytes is not JSON serializable` and never persists the config at all.
- **ClipFan** — architecturally close to what we want, but licensed
  `Copyright (c) 2026 Prime Radiant, Inc. All rights reserved.` with no grant of rights.
  Used as a reference for ideas only.

## Goals

1. Bidirectional clipboard sync for text.
2. Zero manual action after the PC's daily reboot.
3. Failures are visible — no silent death.
4. No server, no accounts, no sessions, no listening ports, no certificates.

## Non-goals (v1)

Images, files, clipboard history, more than two machines, mobile clients, filtering of
secrets (explicitly declined — everything copied is synced), syncing the X11/Wayland
PRIMARY selection.

## Verified constraints

These were measured, not assumed:

| Fact | Consequence |
|---|---|
| Mac does not listen on SSH (port 22 closed; enabling Remote Login needs admin) | The Mac must initiate the connection. The PC cannot dial in. |
| Mac → PC key auth works today; every key on the Mac is passphraseless | A launchd-spawned agent can use the existing key. `SSH_AUTH_SOCK` is irrelevant. |
| No `anatoly-ubuntu` entry in `~/.ssh/config` — it resolves via router DNS, default username, and default identity order | Three implicit dependencies. The agent passes host, user and key explicitly. |
| `wl-clipboard` 2.2.1: `wl-paste --watch` reports *"Watch mode requires a compositor that supports the wlroots data-control protocol"* | No event-based clipboard watching on GNOME/Mutter out of the box. |
| GPaste 45.3 daemon is active (`systemctl --user is-active org.gnome.GPaste`) | Its DBus signal is the event source, with polling as fallback. |
| macOS Sequoia Local Network privacy gate does **not** block launchd-spawned processes or their children on 15.7.4 (verified with a live launchd experiment) | No foreground app or `nohup` workaround needed. Behaviour changed across 15.x, so re-check after macOS upgrades. |
| Toolchains: Mac has Swift 6.1.2, no Go/Rust. PC has python3.13, no Go/Rust. | Swift on the Mac, stdlib Python on the PC. |

## Architecture

One long-lived SSH channel. The Mac agent runs:

```
ssh -i <identity> -o IdentitiesOnly=yes \
    -o BatchMode=yes -o ConnectTimeout=5 \
    -o ServerAliveInterval=5 -o ServerAliveCountMax=2 \
    <user>@<host> <remote-agent-path>
```

`sshd` spawns the remote agent, so **no daemon is installed on the PC** — just one
Python file and nothing else. Frames flow both ways over that process's stdin/stdout.

The defining property: **there is no state that can be lost.** No sessions, cookies,
tokens, ports or certificates. Authentication and encryption come from SSH. The daily
reboot is a non-event — the Mac notices the channel died, waits, and dials again.

## Components

### Mac agent (Swift, single binary, launchd)

- **Pasteboard watcher** — timer compares `NSPasteboard.general.changeCount`. On change,
  reads the string and hashes it. In-process, so polling costs almost nothing.
- **Channel supervisor** — spawns `ssh`, pumps frames over its pipes, reconnects with
  backoff on exit. (Re-dialling proactively on wake from sleep is deferred: `ServerAlive`
  already recovers within ~15 s, and it is not worth the AppKit wiring in v1.)
- **Frame codec** — see Protocol.
- **Status writer** — maintains a small state file for `clipwire status`.

### PC agent (Python 3 stdlib, single file, spawned by sshd)

- **Environment** — an SSH session gets `XDG_RUNTIME_DIR=/run/user/1000` from `pam_systemd`,
  but `WAYLAND_DISPLAY` is empty and there is no session bus address. The agent builds them
  itself: `WAYLAND_DISPLAY=wayland-0`,
  `DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus`, falling back to
  `XDG_RUNTIME_DIR=/run/user/$(id -u)` when the variable is missing.
- **Watcher** — Python has no stdlib DBus binding, so the agent spawns `gdbus monitor` as a
  subprocess and parses its output line by line. GPaste emits
  `Update(s action, s target, ...)` on `/org/gnome/GPaste`; **filter by `target`** — PRIMARY
  selections fire on every mouse drag. Content is then read with `wl-paste -n`. If GPaste is
  unavailable, log once and degrade to polling `wl-paste -n` every second.
- **Writer** — `wl-copy`, spawned **detached with its pipe fds closed**. `wl-copy` does not
  exit: it stays resident as the selection owner. Waiting on it hangs the agent.
- **Subprocess timeouts** — every `wl-paste` / `gdbus` call is killed after 3 s. `wl-paste`
  blocks indefinitely when the current selection owner is a hung application, and without a
  timeout that stalls the whole agent.
- **Frame codec** — the same wire format, implemented independently.

No third-party Python packages. No venv. Deployment is one file.

## Connection lifecycle

The remote agent has two phases, and the distinction matters because after a reboot the
PC is reachable by SSH long before the Wayland session exists.

1. **`clipboard-pending`** — entered immediately on start. The agent sends `hello` right
   away, *before* the clipboard is usable, so the Mac can tell a live peer from a dead one
   instead of staring at an open channel. Incoming clips are accepted but not applied; only
   **the most recent one is retained**. Replaying a queue of older clips into GPaste history
   once the session appears is noise, not a feature.
2. **`ready`** — the Wayland socket exists. The retained clip, if any, is applied and normal
   sync begins.

Losing the socket at runtime (logging out of GNOME mid-day) returns the agent to
`clipboard-pending` — the same path as boot. It is not a crash and must not spam errors.

**The agent exits on stdin EOF in every phase.** This is not optional: `sshd` closes the
pipes when the channel drops, but an agent sleeping inside a `while not socket_exists()`
loop never reads stdin and never notices. Every Mac reconnect during the pre-login window
would then spawn another agent while the previous ones linger, and once the session starts
several agents would race to write the clipboard. The main loop is therefore a `select()`
on stdin with a timeout in *all* phases; EOF or a closed pipe means immediate exit.

**Single-writer discipline.** Both sides have two independent frame sources — the reader
handling incoming frames and the local clipboard watcher. Interleaved writes produce a
corrupt stream, which the receiver dutifully detects and reconnects over, giving an
unexplained flap that is miserable to trace. All writes go through one serial path: a lock
around `write`+`flush` on the PC, a single serial queue or actor on the Mac.

## Protocol

Frames over stdin/stdout:

```
[u32 big-endian payload length][u8 type][payload]
```

| Type | Meaning |
|---|---|
| `0x00` | hello — JSON `{"protocol": <int>, "agent": "<version>"}` |
| `0x01` | clip, `text/plain; charset=utf-8` |

Both sides send `hello` on connect. A protocol-major mismatch is logged and the channel
is closed rather than guessed at — this is what catches a half-updated deployment, which
is the main hazard of having two independent codec implementations.

The type field exists from day one even though v1 carries only text, so adding images
later does not break the format.

**Frame cap: 4 MiB.** The sender skips oversized clips and logs it. A received length
above the cap means the stream is corrupt: close the channel and reconnect. No attempt is
made to resynchronise mid-stream.

**Stream discipline:** the PC agent writes **only frames to stdout**. All diagnostics,
tracebacks and warnings go to stderr, which the Mac merges into its own log. A single
stray `print()` on stdout would desynchronise the protocol and be miserable to debug.

## Echo suppression

Each side remembers the hash of the last value **it wrote into its own clipboard**. When
the local watcher observes a change with that hash, it stays silent. Without this, a clip
ping-pongs forever. The hash lives in memory and survives reconnects, so a reboot cannot
trigger an echo storm.

Concurrent edits on both sides: last write wins. With two nodes and echo suppression this
converges on its own; vector clocks are not warranted.

## Failure handling

| Situation | Behaviour |
|---|---|
| PC off or rebooting | `ssh` fails to connect. Backoff 1→2→5→15→30→60 s, capped at 60 s. Logged at info, not as an error every few seconds. `status` reports "peer unreachable since T". |
| Mac slept | TCP is dead but looks alive. `ServerAliveInterval=5` / `ServerAliveCountMax=2` make `ssh` exit; the supervisor reconnects. A wake notification triggers an immediate re-dial instead of waiting for the timeout. |
| PC booted, user not yet logged into GNOME | Remote agent waits for the Wayland socket and does not exit. |
| GPaste unavailable | Logged once, degrade to polling. Not an error. |
| Clip exceeds frame cap | Sender skips it and logs. Receiver treats an oversized length as corruption and reconnects. |
| Non-text clipboard content on the Mac (e.g. an image) | Skipped in v1, but the `changeCount` is recorded so the same item is not rescanned in a loop. |
| Wayland socket disappears at runtime (user logs out of GNOME) | Back to `clipboard-pending`, same path as boot. `gdbus monitor` dying is expected here, not an error to spam. |
| `wl-paste -n` exits non-zero (empty or non-text clipboard) | Skip, not an error. |
| Empty clip | Never synced, in either direction. |
| `wl-paste` or `gdbus` hangs | Killed after 3 s. A hung selection owner must not stall the agent. |
| Protocol version mismatch | Channel closed, and surfaced in `clipwire status` with the hint to run `clipwire install` — not only in the log. Otherwise a forgotten install flaps silently once a minute and the "failures are visible" goal is broken. |
| Fatal config error | Logged, then **exit 0**, paired with `KeepAlive={SuccessfulExit: false}` in the plist. Exiting non-zero here would give an eternal launchd restart loop. The agent stays down and `status` reports it. |

## Observability

- **Log file** with rotation, plus stderr from the remote side merged in.
- **`clipwire status`** — channel state (`up`, `clipboard-pending`, `down`), time of the
  last clip in each direction, reconnect count, and the reason when it is down. Non-zero
  exit when unhealthy, so it can be wired into a statusline or a script.
- **Liveness, not last-known-state.** `status.json` carries a heartbeat timestamp refreshed
  every 5 s and the agent's pid. `clipwire status` checks both: a stale heartbeat or a dead
  pid reports `agent dead`, regardless of what the file last claimed. A status file that
  cheerfully reports "up" because the writer crashed is precisely the invisible failure
  this project exists to avoid.

This exists because the failure that cost us an evening was invisible: the client looped
silently and nothing surfaced it.

## Configuration

`~/.config/clipwire/config.json`, outside the repository (the repo is public and ships
only an example):

```json
{
  "host": "anatoly-ubuntu",
  "fallback_ip": "192.168.1.45",
  "user": "anatoly",
  "identity_file": "~/.ssh/id_ed25519",
  "remote_agent_path": "~/.local/share/clipwire/clipwire-agent.py",
  "mac_poll_interval_ms": 400,
  "pc_fallback_poll_interval_ms": 1000,
  "max_frame_bytes": 4194304
}
```

The two intervals differ on purpose. The Mac's poll only compares an integer
(`changeCount`) in-process, so 400 ms is free. The PC's fallback poll forks `wl-paste` and
reads the entire clipboard every time — up to 4 MiB — so it runs at 1 s. That path is a
degraded mode anyway; the GPaste subscription is event-driven and has no interval.

`fallback_ip` is tried when the hostname does not resolve — today the name works only
because the router serves it.

## Files and paths

**Mac**

| Path | Purpose |
|---|---|
| `~/.config/clipwire/config.json` | Configuration. |
| `~/.local/bin/clipwire` | The binary — agent and CLI in one, `swiftc`-built, no dependencies. |
| `~/.local/state/clipwire/clipwire.log` | Log, rotated at 5 MiB, two generations kept. |
| `~/.local/state/clipwire/status.json` | State read by `clipwire status`. Written by the agent, never read by it. |
| `~/Library/LaunchAgents/dev.b1rdex.clipwire.plist` | `RunAtLoad`, plus `KeepAlive={SuccessfulExit: false}` as a crash backstop only — reconnects are handled inside the agent, and a clean exit (config error) must not be restarted. |

**PC**

| Path | Purpose |
|---|---|
| `~/.local/share/clipwire/clipwire-agent.py` | The whole remote side. Placed by `clipwire install`; nothing else is installed. |

No systemd unit on the PC: `sshd` spawns the agent per connection.

## Installation

`clipwire init` writes a config from the example and refuses to overwrite an existing one.
Running the agent without a config is a fatal error with a message naming the expected
path — not a silent default that connects somewhere unintended.

`clipwire install` copies the Python agent to the PC over SSH, sets the executable bit and
shebang, and verifies it by running `clipwire-agent.py --selftest` — which checks the
Python version, encodes and decodes a codec fixture, and reports whether GPaste and
`wl-copy`/`wl-paste` are present, without requiring a live Wayland session. Exit code
decides whether the install succeeded.

Explicit, not an automatic self-deploy on every connect: version negotiation and the
bootstrap ordering problem are not worth it for two machines. The `hello` frame catches a
version mismatch if the install step is ever forgotten.

## Testing

Test-driven, per the usual flow.

- **Unit** — frame codec (round-trip, truncated frame, oversized length, unknown type),
  echo-suppression state machine, backoff sequence.
- **Cross-language conformance** — shared byte-vector fixtures that both the Swift and the
  Python codec must encode and decode identically. Two implementations of one wire format
  is the obvious place for drift.
- **Lifecycle** — the phase machine: `hello` is sent before the clipboard is ready; a clip
  arriving in `clipboard-pending` is retained and only the newest survives; the transition
  to `ready` applies it; stdin EOF exits in *both* phases. The pre-login reconnect path is
  the most fragile part of the design and the least likely to be exercised by accident.
- **Integration** — the Python agent driven as a subprocess over scripted pipes, with no
  SSH and no Wayland, to exercise the protocol in isolation.
- **Acceptance** — copy on the Mac appears on the PC; copy on the PC appears on the Mac;
  **reboot the PC and both still work with no manual action.** That last one is the point
  of the project.
- **Post-upgrade smoke** — after a macOS upgrade, confirm the launchd agent can still
  reach an RFC1918 address. The Local Network gate's behaviour changed between 15.0 and
  15.3 and may change again.

## CI

GitHub Actions on push and pull request. Without it the test-driven flow rests on nothing
but discipline.

| Job | Runner | Runs |
|---|---|---|
| `swift` | `macos-latest` | Build, then the Swift unit tests: codec, echo suppression, backoff, status freshness. |
| `python` | `ubuntu-latest` | `python -m compileall` and the stdlib `unittest` suite: codec, lifecycle phases, frame cap. |

The two codec implementations are kept honest by a **committed golden file** of byte
vectors. Each job independently encodes the fixtures and compares against that file, and
decodes it back. No cross-runner coordination is needed, and any drift fails on whichever
side moved.

The Python suite uses `unittest` only — the agent ships with no third-party packages, and
its tests must not either, or CI would stop resembling the target machine.

**What CI cannot cover, stated plainly:** the real clipboard (needs a live Wayland
session), the SSH channel, launchd behaviour, and the Local Network gate. Green CI means
the pure logic holds — it is not evidence that sync works end to end. The acceptance test
stays manual and is the only thing that proves the project does its job.

## Known risks

1. **Local Network gate drift on macOS upgrades.** Mitigated by the smoke check above; the
   agent logs connect `errno`, so `EHOSTUNREACH` with a reachable host is the signature.
2. **GPaste dependency.** Soft: removing GPaste degrades to polling rather than breaking.
3. **Two codec implementations.** Mitigated by shared fixtures and the `hello` handshake.
4. **Hostname resolution depends on the router.** Mitigated by `fallback_ip`.
