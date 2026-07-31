# clipwire

One clipboard across a Mac and a Linux box on the same network, over a single SSH channel.

No server, no accounts, no sessions, no listening ports, no certificates. The Mac dials
out; `sshd` on the other end spawns a single-file Python agent. Authentication and
encryption come from SSH, so there is no state that a reboot can invalidate.

Built for a specific pair of machines — macOS Sequoia and Ubuntu 25.10 on GNOME/Wayland,
where `wl-paste --watch` does not work because Mutter has no wlroots data-control protocol.

**Status:** implemented. For architecture and the constraints that shaped it, read
[the design doc](docs/superpowers/specs/2026-07-30-clipwire-design.md) and then
[the protocol v2 amendment](docs/superpowers/specs/2026-07-31-protocol-v2-freshness-design.md),
which supersedes it on the wire format and on what happens at connect time: the two sides
now exchange what each holds and how old it is, and the fresher one sends. The Swift and
Python test suites both run in CI. The acceptance test is manual and lives in the
amendment; the two halves of the program have never been exercised against each other by
any automated test, because each suite drives one side against scripted pipes.

**Passwords land in GPaste's history on the PC and stay there.** Anything copied on the
Mac is written to the PC's clipboard, and GPaste records it in its on-disk history. A
password copied out of 1Password is no exception. 1Password clears the Mac's clipboard
after about ninety seconds, but a cleared clipboard is empty and empty clips are never
synced, so the clearing does not replicate. Remove it on the PC with:

```sh
gpaste-client delete-history
```

## Installing

Everything below runs on the Mac, from inside a clone of this repository. `clipwire init`
and `clipwire install` both resolve `config.example.json` and `agent/clipwire-agent.py`
relative to the current directory, not relative to the installed binary, so both need to
be run from the repo root.

1. Build the binary:

   ```sh
   swift build -c release
   ```

2. Install it:

   ```sh
   mkdir -p ~/.local/bin
   cp .build/release/clipwire ~/.local/bin/clipwire
   ```

3. Write a config, then edit it:

   ```sh
   ~/.local/bin/clipwire init
   ```

   This writes `~/.config/clipwire/config.json` from `config.example.json` and refuses to
   overwrite an existing one. At minimum, set `host` (or `fallback_ip`), `user`, and
   `identity_file` to point at your PC and the key that already authenticates to it.

4. Pre-flight check: the remote login shell must print nothing on stdout. `sshd` runs the
   remote agent through that shell, and any greeting it prints — a MOTD, a startup banner —
   gets prepended to the frame stream and desyncs the protocol from the very first byte.

   ```sh
   ssh -o BatchMode=yes user@host true 2>/dev/null | wc -c     # must print 0
   ```

   If this prints anything but `0`, silence the shell's startup output for non-interactive
   sessions before continuing.

5. Install the remote agent:

   ```sh
   ~/.local/bin/clipwire install
   ```

   This copies `agent/clipwire-agent.py` to the PC over SSH, sets its executable bit, and
   runs it with `--selftest` to check the Python version and the presence of
   `wl-copy`/`wl-paste`/GPaste.

6. Load the launchd agent. Copy the plist, substitute the real path to the installed
   binary (the shipped copy has a placeholder), then bootstrap it:

   ```sh
   mkdir -p ~/Library/LaunchAgents
   cp launchd/dev.b1rdex.clipwire.plist ~/Library/LaunchAgents/
   # edit ~/Library/LaunchAgents/dev.b1rdex.clipwire.plist:
   # replace /Users/YOUR_USER/.local/bin/clipwire with the real path
   launchctl bootstrap gui/$UID ~/Library/LaunchAgents/dev.b1rdex.clipwire.plist
   ```

7. Check it:

   ```sh
   ~/.local/bin/clipwire status
   ```

## After a GNOME upgrade

Check that the GPaste shell extension is still enabled:

```sh
gnome-extensions list --enabled | grep -i gpaste
```

GPaste tracks the clipboard through that extension, and an upgrade can leave it disabled.
Nothing looks broken when it happens: the GPaste daemon keeps running and keeps answering
on the session bus, so every liveness check that probes the bus still passes — but the
`Update` signal the agent watches for never fires again.

The agent notices on its own and keeps working: its safety-net poll compares the clipboard
every 30 seconds, and once it sees content change with no signal to account for it, it
falls back to polling every second for the rest of the connection. It says so in the Mac's
log (`~/.local/state/clipwire/clipwire.log`):

```
remote: GPaste is not reporting clipboard changes (is the gnome-shell extension enabled?), polling every 1.0s for the rest of this connection
```

The command above is how you answer that question. It prints nothing when the extension is
off; drop `--enabled` to get its name, then `gnome-extensions enable <name>`.

Re-enabling it is the actual fix, and reconnecting is not. The fallback never stops
listening for signals — it only speeds the safety-net poll up from 30 seconds to 1 — and
that faster interval is scoped to one connection, so the next connection starts back at 30
seconds whether or not anything was repaired. With the extension still disabled, the agent
just spends another detection budget before reaching the same conclusion again.

## GPaste trims whitespace, and that is not clipwire

Copy a block of text ending in a newline on the PC, paste it on the Mac, and the trailing
newline is gone. The same text copied on the Mac arrives on the PC without it too. This
looks exactly like a fidelity bug in the sync and it is not: GPaste ships with

```sh
gsettings get org.gnome.GPaste trim-items    # true
```

which strips leading and trailing whitespace from every item it records. The PC's clipboard
never holds the newline in the first place, so there is nothing for clipwire to carry. Both
sides were verified byte-exact against what the clipboard actually held — Cyrillic, emoji,
a composed character in NFD, embedded newlines and a CRLF fragment all survive unchanged in
both directions.

Set it to `false` if you would rather keep the whitespace:

```sh
gsettings set org.gnome.GPaste trim-items false
```
