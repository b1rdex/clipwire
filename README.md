# clipwire

One clipboard across a Mac and a Linux box on the same network, over a single SSH channel.

No server, no accounts, no sessions, no listening ports, no certificates. The Mac dials
out; `sshd` on the other end spawns a single-file Python agent. Authentication and
encryption come from SSH, so there is no state that a reboot can invalidate.

Built for a specific pair of machines — macOS Sequoia and Ubuntu 26.04 LTS with GNOME Shell
50.1 on Wayland (upgraded from 25.10 mid-project), where `wl-paste --watch` does not work
because Mutter has no wlroots data-control protocol.

**Status:** implemented. For architecture and the constraints that shaped it, read
[the design doc](docs/superpowers/specs/2026-07-30-clipwire-design.md), then
[the protocol v2 amendment](docs/superpowers/specs/2026-07-31-protocol-v2-freshness-design.md),
which supersedes it on the wire format and on what happens at connect time: the two sides
now exchange what each holds and how old it is, and the fresher one sends. [The protocol v3
amendment](docs/superpowers/specs/2026-07-31-protocol-v3-images-design.md) adds image sync
on top of that, unchanged on the freshness rule itself. The Swift and Python test suites
both run in CI. The acceptance test is manual: the v2 amendment holds the base checklist
and the v3 amendment adds items to it, covering images specifically. The two halves of the
program have never been exercised against each other by any automated test, because each
suite drives one side against scripted pipes.

**Images sync too, up to 4 MiB, and text wins when the clipboard holds both.** A screenshot
or a copied image syncs the same way text does. When both are on the clipboard at once —
which is what Excel, LibreOffice Calc and Numbers all do, placing a bitmap of the copied
cells alongside the text — text wins, so a spreadsheet range arrives as the text of the
cells, not a picture of the table.

**Passwords land in GPaste's history on the PC and stay there.** Anything copied on the
Mac is written to the PC's clipboard, and GPaste records it in its on-disk history. A
password copied out of 1Password is no exception. 1Password clears the Mac's clipboard
after about ninety seconds, but a cleared clipboard is empty and empty clips are never
synced, so the clearing does not replicate. Remove it on the PC with:

```sh
gpaste-client delete-history
```

**The same is true of screenshots, which may hold more than the person copying them
intended.** GPaste writes image items to its on-disk history exactly as it writes text —
verified: `images-support` is `true`, `~/.local/share/gpaste/images` holds them, and a
screenshot taken on the PC was confirmed to appear in the history listing. As with the text
case above, anything that reaches the PC's clipboard as an image lands in that history too.

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

## Upgrading

The steps above are for a first install. Upgrading is not the same, because launchd is
executing the binary you are about to replace — copying over it gets the running process
killed and leaves the file inconsistent, after which *every* invocation exits 137 and prints
nothing. Stop the agent first:

```sh
launchctl bootout gui/$UID/dev.b1rdex.clipwire
swift build -c release
cp .build/release/clipwire ~/.local/bin/clipwire
~/.local/bin/clipwire install                    # push the matching agent to the PC
launchctl bootstrap gui/$UID ~/Library/LaunchAgents/dev.b1rdex.clipwire.plist
```

The `install` step is not optional on an upgrade that changes the wire protocol. The two
sides negotiate a version in their `hello` frames and refuse to talk across a mismatch, so
a new binary against an old agent does not sync at all.

## A locked PC cannot serve its clipboard

While the PC's session is locked, `wl-paste` hangs instead of answering. Unlocked, the same
call returns quickly — and this file now carries **two figures** for that, which is worth
naming rather than leaving for a reader to trip over. "About 20 ms" was recorded here on
2026-08-01, out of the v3 acceptance run. The next section quotes about a tenth of a second,
measured 2026-08-03 with the agent stopped. Two days apart, on the same machine, with nothing
recorded in between that would account for a fivefold difference. The later campaign wrote
down its instrument and its controls; the earlier reading's conditions were not recorded at
all. Nobody has re-run the two side by side, and neither is retracted on the strength of the
other. What matters *in this section* is unaffected either
way: the contrast is *answers* against *hangs indefinitely*. The next section flags the one
claim that does depend on which figure is right.

*Why* the lock screen has this effect
was not established, and the GPaste daemon, the session bus and the compositor all keep
answering normally throughout — so the usual health checks all pass while nothing works.
Nothing syncs in either direction until the screen is unlocked, and the Mac's log fills with
lines like:

```
remote: wl-paste failed: TimeoutExpired(['wl-paste', '--list-types'], 3)
```

This is a property of the desktop, not a fault in the sync, and it is why those timeouts
appear in bursts overnight. Left-over `wl-copy` and `wl-paste` processes belonging to reads
that could never finish are part of the same picture; they clear on their own once the
session is unlocked and the selection can change hands again.

Nothing degrades as a result: a read that fails proves nothing about whether the event
source is alive, so it never counts toward the verdict that switches the agent to faster
polling.

## Watching the clipboard steals much less keyboard focus

Not *none*, and the last two bullets below say exactly where it survives.

Measured on this PC: `wl-paste --list-types` **takes keyboard focus while it runs**, which
shows up as the foreground window blinking. Blind A/B, 2 Hz for 15 seconds a phase, phases
unlabelled until they had been judged; `xclip -o -t TARGETS` against the same selection does
not blink. The `wl-paste` phase is the **positive control** — it is what proves the observer
could see the effect at all, so xclip's silence is a real negative rather than an instrument
that was never sensitive enough. Mutter implements no
`wlr-data-control`/`ext-data-control`, so a focus grab is how wl-clipboard reads a selection
here in the first place.

**The five-second history check** no longer forks that call. It asks GPaste over D-Bus for
the identifier of the newest item in its history instead, and that takes no focus. **It is
not faster** — both calls cost about a tenth of a second. The design doc claimed the D-Bus
call was twenty to thirty times cheaper until it was timed the way the agent actually makes
it, as a subprocess; that claim is retracted. The entire difference is the focus grab.

**This is the claim that depends on the unreconciled figure above.** It rests on
`wl-paste --list-types` costing about a tenth of a second, measured over ten samples on
2026-08-03. Under the section above's older "about 20 ms" reading it would not hold: the
D-Bus call would be several times *slower* in wall clock, and the difference would not be
the focus grab alone. The focus grab itself is measured either way and is not in doubt —
what a re-measurement could move is the *cost* comparison, not the conclusion that this
check stopped taking focus.

What that changes, and what it does not:

- The blinking that actually hurt was never **the 30-second clipboard poll**, which costs one
  blink every 30 seconds. It was the *fallback*:
  once the agent concluded GPaste had stopped working it polled `wl-paste` once a second for
  the rest of the connection, blinking on every tick and dropping keystrokes while typing.
  It fired three times, ever. One of the three was reconstructed to the second and was
  caused by GPaste re-offering its own image a few seconds after a copy, on a machine where
  nothing was wrong; a second is consistent with the same mechanism without having been
  reconstructed; the third involved text and has no established cause. That mechanism no
  longer reaches a verdict.
- 30-second poll ticks are skipped entirely once nobody has touched the keyboard or mouse for
  five minutes. With nobody copying there is nothing for them to find.
- **Where the blinking survives, first:** the 30-second clipboard poll itself is **unchanged**
  and still forks `wl-paste` on every tick it is not gated out of. It is the only thing that
  can tell a clipboard manager that has stopped recording from a clipboard nobody is using,
  so it stays.
- **And second:** when a verdict *is* reached, that poll keeps running and keeps blinking.
  What changed is the rate — it now starts at one second after each observed change and
  doubles toward 30 seconds while nothing changes, instead of staying at one second forever.
  It is **not** true that the poll is then the connection's only way of seeing a change, and
  a first draft of this bullet said so: the agent goes on listening for GPaste's signals
  through the switch and never tears that subscription down, and one of the two ways this
  verdict can be reached — the excluded-clip case in the next section — leaves GPaste tracking
  and signalling normally. The poll is the only detector left **in the state the verdict
  describes**, where the clipboard manager really has stopped recording; the verdict can also
  be reached when it has not.

## A clip GPaste refuses to record still syncs

GPaste does not add everything to its history. A password manager can mark its selection
sensitive, and GPaste can be configured to exclude clips outright. Nothing is broken when
that happens — GPaste simply records nothing and signals nothing for that clip, by design.

Before v3.3 that was indistinguishable from a broken clipboard manager: the clipboard had
changed, no signal had arrived, and a single such copy at the wrong moment could tip the
agent into the permanent one-second polling described above. Now the two are told apart by
GPaste's own history, which moves for a recorded clip and stands still for an excluded one.
A single excluded copy raises a suspicion that the next look drops, because by then the
clipboard has settled.

The clip itself still reaches the Mac, through the same 30-second clipboard poll that
carried it before — that part is unchanged. What is worth knowing is that a *run* of
excluded copies, landing on consecutive poll ticks with the clipboard moving each
time, still looks exactly like a clipboard manager that has stopped recording, and still
produces the fallback. Nothing is lost when it does; the clips keep syncing, the poll just
runs faster for that connection.

## After a GNOME upgrade

Check that the GPaste shell extension is still enabled:

```sh
gnome-extensions list --enabled | grep -i gpaste
```

GPaste tracks the clipboard through that extension, and an upgrade can leave it disabled.
Nothing looks broken when it happens: the GPaste daemon keeps running and keeps answering
on the session bus, so every liveness check that probes the bus still passes — but the
`Update` signal the agent watches for never fires again.

The agent notices on its own and keeps working, and since v3.3 it has two independent ways
of noticing, which behave very differently. Which one applies turns on whether GPaste is
still *recording* clips or only failing to *announce* them — a disabled extension stops
both, so it lands in the second case, but the two are worth telling apart because the log
lines are different and only one of them changes how the agent behaves.

**If GPaste is still recording clips and only the signal is missing,** the agent sees it
within five seconds and changes nothing else. It asks GPaste over D-Bus for the identifier
of the newest item in its history, every five seconds; that identifier moves whenever a clip
is recorded, whether or not a signal was ever sent. Syncing carries on at that pace, no
polling speeds up, and the Mac's log gets one line:

```
remote: the uuid tier saw the clipboard history move while the accepted-signal count held at 0; the signal path may be silent, but tracking itself is alive -- the fast tier is already this connection's sync, every 5s, at zero focus cost. Informational only; no interval changes because of this line.
```

**If GPaste has stopped recording clips at all,** that identifier stops moving too, so the
only thing left that can tell is a direct look at the clipboard. That is the 30-second
clipboard poll — the safety net, and the same one the two sections above describe. When it sees the clipboard change twice running while GPaste's history
stands still, it concludes the tracker is dead and falls back to polling — starting one
second after each change it observes and doubling toward 30 seconds while nothing changes,
for the rest of the connection. It says so in the Mac's log
(`~/.local/state/clipwire/clipwire.log`), reporting what it observed rather than guessing
why:

```
remote: GPaste reported no clipboard change while the content changed (signals=0 signals_at_last_tick=0 pump_alive=True worker_alive=True gpaste_Active=true). Polling every 1s after each observed change and doubling to at most 30s while nothing changes, for the rest of this connection.
```

`gpaste_Active=` is GPaste's own answer to "are you tracking the clipboard", read at the
moment the verdict is reached — `true`, `false`, or `unavailable` when the question could
not be asked. The line used to end "the gnome-shell extension being disabled is one possible
cause", and that clause is gone: the extension was measured enabled and active during every
incident it was ever printed for, so it named a cause it could not know.

The command above is still worth running when this line appears, because a disabled
extension does produce this state — the log simply no longer claims that is what happened.
It prints nothing when the extension is off; drop `--enabled` to get its name, then
`gnome-extensions enable <name>`.

Re-enabling it is the actual fix, and reconnecting is not. The fallback never stops
listening for signals — it only changes how often the safety-net poll looks — and that
faster interval is scoped to one connection, so the next connection starts back at 30
seconds whether or not anything was repaired. With the extension still disabled, the agent
just spends another detection budget before reaching the same conclusion again.

One thing that poll deliberately does *not* do is read images. Copied text it compares
byte for byte; for an image it compares only the list of formats the clipboard is offering,
and fetches the picture itself only once that list changes. Pulling a 4 MiB screenshot back
out of the clipboard on every tick is not a price worth paying to notice a copy a little
sooner — every 30 seconds on a healthy connection, where this poll is only a safety net, and
between one and 30 seconds once it has fallen back. The trade is that while the tracker is
dead, one image replacing another is noticed when the offered formats change rather than the
instant the pixels do. That only bites while the tracker is dead, which is the one state
nothing else covers: in normal operation the `Update` signal carries the change and nothing
waits at all, and if only the signal has stopped, the five-second history check carries it
instead.

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

## GPaste re-encodes images, and that is not clipwire

This is not something clipwire's write path does: it happens to images copied directly on
the PC too, not only to ones clipwire writes there. Copy a screenshot, and a few seconds
after it lands on the PC's clipboard, GPaste takes over selection ownership and silently
replaces it with its own re-encoding of the same picture.

Measured on the PC: a 105,700-byte PNG written to the clipboard read back a few seconds
later as a *different* PNG — 180,287 bytes, 70% larger, and not the bytes that were
written. The read-back value is stable after that, but it is never byte-identical to the
one that was copied.

This is the same disease as `trim-items` above: the clipboard does not necessarily hold
what you put in it. clipwire is built around that rather than surprised by it: the re-encode
does not confuse the two machines into re-sending the same screenshot forever, though a
screenshot copied directly on the PC does currently cost two frames to the Mac — the
original, then the re-encode, back to back — before it settles. The picture that lands is
still GPaste's re-encoding, though, not a byte-identical copy of what was on the Mac's
pasteboard.

**A screenshot used to come back at double size, and no longer does.** The re-encode drops the
PNG `pHYs` chunk, which is what records pixel density. That does not matter on the PC, but the
re-encoded picture used to win the next reconnect, so the Mac ended up holding a copy of its
*own* screenshot with the density gone — 100×100 pixels that displayed at 50×50 before the
round trip displayed at 100×100 after it.

**The PC now says where its bytes came from.** It is the only witness to the substitution: it
wrote the Mac's bytes and read different ones back, with nobody touching the clipboard in
between. So it reports the hash it was *given* alongside the hash it read, and when the two
machines see that one side's content descends from the other's, both stand down and neither
sends anything.

**This rests on an assumption about the machines, not on anything the protocol guarantees.**
The rule that decides is symmetric — both sides run the same comparison — but the *witnessing*
is not: only the PC recognises a substitution, because only GPaste performs one here. Install a
clipboard manager on the Mac that rewrites what it stores, and the same bug reappears with the
roles swapped and nobody in a position to report the origin. Nothing detects that
automatically; it is written down because it is the kind of thing a future reader would
otherwise have to rediscover.

There is also a one-off cost if the Mac's clip-state file is lost: it alone carries the origin,
so one degraded copy can arrive before the two sides agree again. It stops there rather than
compounding.

Two earlier attempts tried to recognise the two pictures as the same one, and both are worth
knowing about because they failed for the same reason. GPaste does not merely strip metadata —
it applies the image's embedded colour profile as it loads it and writes the result untagged,
so the pixel samples genuinely move: measured on a 480×320 screenshot, 398,267 of 614,400
bytes. Nothing that compares the two pictures can work, and comparing them was always the wrong
question. The PC knew where its bytes came from and was throwing that away.
