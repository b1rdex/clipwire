# ClipWire protocol v2 — freshness reconciliation

**Date:** 2026-07-31
**Status:** batya-reviewed (blockers 1–5 folded in), awaiting owner approval
**Amends:** [2026-07-30-clipwire-design.md](2026-07-30-clipwire-design.md)

## The defect this fixes

v1 has no way to reconcile two clipboards at connect time, so it guesses — and the guess
is wrong in the product's central use case.

The v1 agent seeds `_last_seen` from the clipboard when the Wayland session appears, so a
spurious GPaste signal cannot push stale content over a fresh copy on the Mac. That seed
was justified as making the two sides symmetric. The justification rested on a false
premise: **that reconnects are rare recovery events.** They are not. Every time the Mac
sleeps, `ServerAliveInterval`/`ServerAliveCountMax` drop the channel within about fifteen
seconds, `sshd` reaps the remote agent, and waking spawns a fresh one — with a fresh seed.

The consequence, in the flow this project exists for:

1. Copy something on the PC while the Mac is asleep.
2. Wake the Mac and press Cmd-V.
3. Nothing arrives. The clip was seeded into `_last_seen` at connect and is never sent.

It is worse than a one-off loss: re-copying the same text on the PC is also suppressed,
because it equals the seeded value. To push it the user must copy something else first,
then copy what they actually wanted. Nothing is logged.

The same gap explains a loss v1 documented as unavoidable — clips copied on the Mac while
the PC is off never arrive — and the reason `status` can never durably report
`clipboard-pending`.

The v1 spec asserted that no principled reconciliation exists for two nodes. One does.

## The fix

**Each side announces what it holds and how old that is, once its clipboard is readable.
The fresher side sends.**

### Wire changes

**Clip frames carry their own age.** Type `0x01` payload becomes:

```
[f64 big-endian ts][utf-8 text bytes]
```

Without this the reconciliation is unimplementable: the receiving side must record *the
peer's* timestamp for content it applies, and it cannot record what the wire never carried.
This is a breaking change to the clip frame, and the golden vectors in `fixtures/frames.json`
change with it.

**A new frame type `0x02`, clip-state**, payload JSON:

```json
{"sha256": "<hex>", "ts": 1785400000.0}
```

`sha256` is `null` when the clipboard is empty or unreadable. Sent **exactly once per
connection, when the clipboard first becomes readable** — on the Mac immediately after
`hello`, on the PC inside `clipboard_became_ready`. Not resent afterwards; the ordinary echo
machinery covers the rest of the session.

It is a separate frame rather than fields on `hello` because `hello` is deliberately sent
*before* the clipboard is ready — that was a v1 blocker, and for good reason: it is how the
Mac tells a live peer from a dead one during the post-reboot window before GNOME login. Had
reconciliation ridden on `hello`, the PC would announce an empty clipboard, the session
would begin, the user would log in, and reconciliation would never happen at all. Splitting
it also gives `status` its long-missing protocol basis for reporting `clipboard-pending`
durably.

**`hello` gains `sent_at`** — the sender's clock at the moment of sending — used only for
skew measurement, described below.

`PROTOCOL_VERSION` becomes `2`. The existing mismatch handling already closes the channel
and surfaces the reason in `status`, so a half-updated deployment fails loudly.

### Resolution rule

On receiving the peer's clip-state, a side that has already determined its own compares:

| My state | Peer state | Action |
|---|---|---|
| hashes equal | — | nothing; both sides agree |
| hash `null` | hash present | nothing; the peer sends |
| hash present | hash `null` | **I send my clip** — this also closes the v1 loss of Mac copies made while the PC was off |
| both `null` | — | nothing |
| both present, my `ts` greater | — | I send |
| both present, peer `ts` greater | — | nothing; the peer sends |
| both present, `ts` equal, hashes differ | — | the side whose **hex hash sorts greater** sends |

Timestamps are never compared when either hash is `null`; that comparison is what would
otherwise put a `float` next to a `None` and take the Python agent down on every handshake
with an empty clipboard — which is to say after every PC reboot.

The tie-break is a hash comparison rather than "the Mac wins" so that both implementations
run *the same formula* instead of a mirrored pair of conditions. Mirrored conditions
drifting apart is a failure this project has already had twice.

### Where a timestamp comes from

Three sources, in order of precedence:

1. **A local change observed by the watcher** — `ts` is the moment of observation.
2. **A clip received from the peer** — `ts` is the peer's value, carried in the frame,
   stored unchanged. Content that came from elsewhere must never look freshly copied here,
   or it bounces straight back.
3. **Content that predates this agent process** — the hard case, and exactly the wake flow.
   The PC agent is born fresh on every connection, so a clip copied while the Mac slept was
   never observed by anyone still running.

For the third case, both sides keep a **persistent store** — `~/.local/state/clipwire/clip-state.json`
on each machine — holding the last known `(sha256, ts)`. It is written on every observed
local change and on every applied remote clip. At startup the agent hashes the current
clipboard: if it matches the store, the stored `ts` is authoritative; if it does not, the
content changed while nothing was watching, so `ts = now` and the agent logs
`clipboard changed while apart`.

**The consequence, stated rather than left to emerge from the code:** when both clipboards
changed while the channel was down, the side whose agent was born more recently wins — in
practice the PC, whose agent restarts on every connection. That is a real trade-off. It is
better than v1, which silently kept both machines out of sync forever, and it is visible in
the log rather than mysterious.

### Clock skew

Skew is measured as `|my_clock_now − peer_sent_at|` from the `hello` frame, with LAN
round-trip time treated as negligible. It is **not** the difference between the peer's
`clip_ts` and the local clock: that is the *age of the clip*, and a clip legitimately copied
this morning is hours old. Warning on that quantity would fire on nearly every handshake and
teach everyone to ignore the log.

No correction is attempted — both machines run NTP on one LAN and second-level accuracy is
ample. Skew above five seconds is logged as a warning, because a badly skewed clock makes
one side win every reconciliation and that should be visible.

### What stays

`_last_seen` and its connect-time seed **both remain**, and their role is now explicit:
they suppress *intra-session non-events* — a GPaste signal that reports no actual change.
The clip-state exchange handles *inter-session freshness*. These are different problems and
removing either reopens a defect that has already been fixed once. An implementer who
concludes the seed is "replaced by the handshake" will reintroduce the re-send bug.

## Also in this amendment

Four gaps from the same review, none of them protocol changes.

### Event-source liveness — "available" is not "working"

`GPasteWatcher.available()` probes the bus name. But GPaste tracks the Wayland clipboard
through a gnome-shell extension, and a GNOME upgrade can leave the daemon running and the
bus answering while the extension is disabled. Then `Update` never fires, PC→Mac sync is
silently dead, and the polling fallback does not engage, because it is keyed on the bus
being unreachable rather than on events being absent.

Fix: a slow safety-net poll every 30 seconds alongside the subscription. If it observes a
content change the signal path did not report, log once and switch to polling for the rest
of the connection.

**The safety net must go through the same `_local_change` path as a signal** — the same
observation, the same one-shot suppression, the same `_last_seen`. A parallel code path
would be a second copy of the echo logic, and this project has already fixed two races in
the first copy. Its cost, worth writing down: a clip caught only by the safety net gets a
timestamp up to 30 seconds late.

The README gains a line for after a GNOME upgrade:
`gnome-extensions list --enabled | grep -i gpaste`.

### Representation fidelity

The two implementations have never exchanged a byte with each other; every test drives one
side against scripted pipes, and the golden vectors are symmetric by construction, so
neither can detect a round trip that alters bytes. `NSPasteboard` → UTF-8 → `wl-copy` →
GPaste → `wl-paste` → `NSPasteboard` is where Unicode normalisation, a trailing newline or
CRLF could change what returns. If it does, the echo hash misses and the clip makes an extra
round — or ping-pongs.

No unit test on either side can close this. It goes into the acceptance checklist as a
matrix, and hashes stay defined over exact bytes with no normalisation anywhere, so drift
surfaces as a mismatch instead of being silently absorbed.

### Secrets persist in the peer's history

The user chose deliberately not to filter secrets, and that choice stands. Its consequence
was never written down: 1Password clears the Mac's clipboard about ninety seconds after a
password is copied, but a cleared clipboard is empty, and empty clips are never synced — so
the clearing does not replicate. A password copied on the Mac lands on the PC and stays in
GPaste's on-disk history indefinitely, where the Mac's clearing cannot reach it.

No code change. The README says this plainly, alongside `gpaste-client delete-history`.

### CI runs a version production does not

The PC runs Python 3.13; tests have run on the Mac's 3.14 and on whatever `ubuntu-latest`
ships. Add 3.13 explicitly to the CI matrix.

## Acceptance checklist — replaces the v1 list

The v1 checklist would have passed while the central flow was broken, because it never
tested a reconnect that was not a reboot.

1. **Wake flow.** Copy on the PC with the Mac asleep. Wake the Mac. Paste. This is the case
   v1 fails and the reason this amendment exists.
2. **Mutual divergence.** With the channel dead, copy different text on both machines.
   Reconnect. The PC's copy must win, and the conflict must appear in the log.
3. **Mac agent restart with a non-empty clipboard.** Exercises the persistent-store path:
   the restored `ts` must come from the store, not from `now`.
4. **Representation matrix**, each direction: Cyrillic, emoji, a composed character in NFD
   (`é` as `e` + U+0301), multi-line text with a trailing newline, a CRLF fragment. One
   round per clip in the log, not two.
5. **Reboot flow including the pending path.** Reboot the PC. Copy on the Mac before GNOME
   login, then copy again. Log in. Only the second clip arrives.
6. **Five rapid copies** each direction. Coalescing is acceptable; a broken echo or a
   ping-pong is not.
7. **Event path.** `gnome-extensions list --enabled`; after an hour the log shows GPaste
   signals rather than permanent polling.
8. **`clipwire status`** in every state: PC off, channel healthy, and after `kill -9` of the
   Mac agent — the heartbeat must go stale and launchd must restart it.
9. **Secrets.** Copy a password from 1Password on the Mac, inspect GPaste's history on the
   PC, and decide whether the result is acceptable.
10. **Pre-flight, before all of the above:**
    `ssh -o BatchMode=yes user@host true 2>/dev/null | wc -c` must print `0`.

"Survives a reboot with no manual action" holds only for boots into Ubuntu; the machine
dual-boots into Windows, where there is nothing to sync with.

## Known imprecision, accepted

When the agent is on the polling fallback, an observed timestamp lags by up to the poll
interval. It is recorded in a comment, not corrected: the comparison's granularity is "which
of these did a human do more recently", and a one-second lag does not change that answer.
