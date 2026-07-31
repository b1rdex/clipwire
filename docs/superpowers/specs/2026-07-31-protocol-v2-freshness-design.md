# ClipWire protocol v2 — freshness at the handshake

**Date:** 2026-07-31
**Status:** pending batya review
**Amends:** [2026-07-30-clipwire-design.md](2026-07-30-clipwire-design.md)

## The defect this fixes

v1 has no way to reconcile two clipboards at connect time, so it guesses — and the guess
is wrong in the product's central use case.

The v1 agent seeds `_last_seen` from the clipboard when the Wayland session appears, so
that a spurious GPaste signal cannot push stale content over a fresh copy on the Mac. That
seed was justified as making the two sides symmetric. The justification rested on a false
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

The v1 spec asserts that "with two nodes and echo suppression this converges on its own;
vector clocks are not warranted." The first half stands. The conclusion drawn from it — that
no principled reconciliation exists — was wrong. One does, and it is one field.

## The fix

**Each side reports the age of what it holds, in the hello it already sends, and the
fresher clipboard wins.**

### Wire change

`hello` (frame type `0x00`) gains two fields:

```json
{"protocol": 2, "agent": "0.2.0", "clip_sha256": "<hex>", "clip_ts": 1785400000.0}
```

- `clip_sha256` — SHA-256 of the exact bytes currently held, lowercase hex. Absent or
  `null` when the clipboard is empty or not yet readable.
- `clip_ts` — Unix seconds, float, of **when this side last observed that content being
  set locally by a human**. Content that arrived from the peer carries the peer's original
  `clip_ts`, unchanged, so it can never look fresher than it is and bounce back.

`PROTOCOL_VERSION` becomes `2`. The existing mismatch handling already closes the channel
and surfaces the reason in `status`, so a half-updated deployment fails loudly rather than
silently misbehaving — which is exactly why that check was built.

### Resolution rule

On receiving the peer's hello, each side compares:

| Condition | Action |
|---|---|
| hashes equal | nothing — both sides already agree |
| my `clip_ts` is greater | send my clip immediately |
| peer's `clip_ts` is greater | do nothing; the peer will send theirs |
| timestamps equal, hashes differ | the **Mac** sends; ties must break deterministically or both sides send and one clobbers the other |

Only the side holding the newer clip acts. There is no negotiation round and no new frame
type.

### What replaces the seed

`_last_seen` stays — it is what stops a non-change GPaste signal from re-sending current
content, which was a real defect. What changes is that the connect-time seed no longer
*silences* a genuinely newer clip: the handshake now decides that explicitly instead of the
seed deciding it by omission. The `Do not "fix" this back` comment on the seed must be
rewritten, because the premise it defends is the one this amendment corrects.

### Clock skew

Both machines are on one LAN and both run NTP; second-level accuracy is ample for a
comparison whose granularity is "which of these did a human do more recently". No
correction is attempted. Each side logs the skew it observes at handshake — the difference
between the peer's `clip_ts` and its own clock at receipt — and logs a warning above five
seconds, because a badly skewed clock makes one side win every reconciliation and that
should be visible rather than mysterious.

## Also in this amendment

Four further gaps, all found in the same review, none of them protocol changes.

### Event-source liveness — "available" is not "working"

`GPasteWatcher.available()` probes the bus name. But GPaste tracks the Wayland clipboard
through a gnome-shell extension, and a GNOME upgrade can leave the daemon running and the
bus answering while the extension is disabled. Then `Update` never fires, PC→Mac sync is
silently dead, and the polling fallback does not engage, because it is keyed on the bus
being unreachable rather than on events being absent.

Fix: run a slow safety-net poll — every 30 seconds — alongside the GPaste subscription.
If the safety net observes a content change that the signal path did not report, log it
once and switch to polling for the rest of the connection. The cost is one `wl-paste` fork
per 30 seconds in the healthy case, which is negligible next to sync being silently dead.

The README gains a line for after a GNOME upgrade:
`gnome-extensions list --enabled | grep -i gpaste`.

### Representation fidelity

The two implementations have never exchanged a byte with each other; every test drives one
side against scripted pipes, and the golden vectors are symmetric by construction, so
neither can detect a round trip that alters bytes. `NSPasteboard` → UTF-8 → `wl-copy` →
GPaste → `wl-paste` → `NSPasteboard` is where Unicode normalisation (NFC versus NFD), a
trailing newline, or CRLF could change what comes back. If it does, the echo hash misses
and the clip makes an extra round — or ping-pongs.

This cannot be closed by a unit test on either side. It goes into the acceptance checklist
as an explicit matrix, and the hashes stay defined over exact bytes with no normalisation
anywhere, so that any drift shows up as a mismatch rather than being silently absorbed.

### Secrets persist in the peer's history

The user chose deliberately not to filter secrets, and that choice stands. Its consequence
was never written down: 1Password clears the Mac's clipboard about ninety seconds after a
password is copied, but a cleared clipboard is empty, and empty clips are never synced — so
the clearing does not replicate. A password copied on the Mac lands on the PC and stays in
GPaste's on-disk history indefinitely, where the Mac's clearing cannot reach it.

No code change. The README says this plainly, next to a note that
`gpaste-client delete-history` exists.

### CI runs a version production does not

The PC runs Python 3.13. Tests have run on the Mac's 3.14 and on whatever `ubuntu-latest`
ships. The floor is declared as 3.11. Add 3.13 explicitly to the CI matrix so the version
in production is the version under test.

## Acceptance checklist — replaces the v1 list

The v1 checklist would have passed while the central flow was broken, because it never
tested a reconnect that was not a reboot.

1. **Wake flow.** Copy on the PC with the Mac asleep. Wake the Mac. Paste. This is the
   case v1 fails and the reason this amendment exists.
2. **Representation matrix**, each direction: Cyrillic, emoji, a composed character in NFD
   (`é` as `e` + U+0301), multi-line text with a trailing newline, a CRLF fragment. The log
   must show one round per clip, not two.
3. **Reboot flow including the pending path.** Reboot the PC. Copy on the Mac before GNOME
   login, then copy again. Log in. Only the second clip arrives.
4. **Five rapid copies** in each direction. Coalescing is acceptable; a broken echo or a
   ping-pong is not.
5. **Event path.** `gnome-extensions list --enabled`; after an hour of use the log shows
   GPaste signals rather than permanent polling.
6. **`clipwire status`** in every state: PC off, channel healthy, and after `kill -9` of the
   Mac agent — the heartbeat must go stale and launchd must restart it.
7. **Secrets.** Copy a password from 1Password on the Mac, then inspect GPaste's history on
   the PC and decide whether the result is acceptable.
8. **Pre-flight, before any of the above:** `ssh -o BatchMode=yes user@host true 2>/dev/null | wc -c`
   must print `0`.

Note that "survives a reboot with no manual action" holds only for boots into Ubuntu; the
machine dual-boots into Windows, where there is nothing to sync with.
