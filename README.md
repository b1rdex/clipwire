# clipwire

One clipboard across a Mac and a Linux box on the same network, over a single SSH channel.

No server, no accounts, no sessions, no listening ports, no certificates. The Mac dials
out; `sshd` on the other end spawns a single-file Python agent. Authentication and
encryption come from SSH, so there is no state that a reboot can invalidate.

Built for a specific pair of machines — macOS Sequoia and Ubuntu 25.10 on GNOME/Wayland,
where `wl-paste --watch` does not work because Mutter has no wlroots data-control protocol.

**Status:** implemented. See
[the design doc](docs/superpowers/specs/2026-07-30-clipwire-design.md) for architecture,
protocol, and the constraints that shaped both. The Swift and Python test suites both run
in CI; the acceptance test — copy on one machine, paste on the other, survive a PC reboot
with no manual action — is manual, per the design doc's Testing section.

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
off; drop `--enabled` to get its name, then `gnome-extensions enable <name>`. The fallback
is scoped to one connection, so event-driven watching resumes on the next reconnect.
