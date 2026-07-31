# ClipWire protocol v3 — images, and two production defects

**Date:** 2026-07-31
**Status:** draft, pending batya review
**Amends:** [2026-07-31-protocol-v2-freshness-design.md](2026-07-31-protocol-v2-freshness-design.md),
which amends [2026-07-30-clipwire-design.md](2026-07-30-clipwire-design.md)

## Why this exists

Two things arrived at once.

**Images.** The owner asked for them the first time the text-only scope actually bit: they
wanted to show a screenshot from the PC and could not. Scope was chosen deliberately in v1
and is now being widened deliberately.

**Two defects found on the first day of real use.** Both were invisible to the entire review
chain because both need a running deployment to appear. They are fixed here rather than
separately, because the image work makes one of them materially worse.

## Corrections to the record, before anything else

The environment moved during v2's development and both prior specs are now wrong about it:

- The PC runs **Ubuntu 26.04 LTS with GNOME Shell 50.1**, not 25.10. It was upgraded mid-project.
- Production Python is therefore **3.14.4**, not the 3.13 the plan recorded. CI's matrix was
  corrected to `["3.11", "3.13", "3.14"]` after the first deployment measured it.
- GPaste is still **45.3** — five major versions behind the shell it plugs into.

Two other facts worth stating so nobody plans around them:

- **There is no transport override and no local pairing harness.** It was recommended after
  v2 and deliberately not built; the owner chose to go straight to v3. The two implementations
  still have no automated way to run against each other.
- **`org.gnome.GPaste trim-items` was `true`** and stripped surrounding whitespace from every
  clipboard item, which looked exactly like a fidelity bug in the sync and was not. The owner
  has set it to `false`. Hashes are over exact bytes, so this changes what the two machines
  agree on — it does not change the protocol.

## Part 1 — the observation path

### The two defects

**The `gdbus monitor` child leaks on every agent death.** Seven were found alive on the PC,
six of them orphans reparented to init, ages 20–33 minutes, exactly one per agent restart.
`GPasteWatcher.stop()` calls `terminate()`, but `sshd` reaps the agent when the channel drops
and `stop()` never runs. Reproduced deterministically: restarting the agent immediately
produced a fresh orphan. Over a daily reboot-and-reconnect cycle this accumulates one
DBus-connected process per connection.

`terminate()` is insufficient in principle, not merely in this path: glib programs set
`SIG_IGN` on `SIGPIPE`, so an orphan does not die even once its output pipe is closed.

**The safety net declared a healthy event source dead.** At 15:35:56 on the first day it
logged `GPaste is not reporting clipboard changes (is the gnome-shell extension enabled?)`
and degraded to 1-second polling. The event source was verified healthy on the live machine:
the extension reported `Enabled: Yes`, `State: ACTIVE`, the bus name was owned, the daemon was
running, and a direct `gdbus monitor` probe caught three `Update` signals for three copies in
exactly the parsed format, all three landing in GPaste's history.

**The mechanism was never established, and this design does not pretend otherwise.** Two
candidates were considered and neither was proven:

1. *The blocking `_observe_lock` starves the pump.* Weakened by the code: the signal counter
   increments **before** `on_change()` is dispatched, with a comment explaining that exact
   risk. A single blocked dispatch cannot freeze the counter. Freezing it for a whole 30-second
   tick would need a block longer than any observed `wl-paste` timeout.
2. *An exception killed the pump thread.* `pump()` has no guard around `on_change()`, and
   `send()` writes to stdout with no error handling, so one exception ends the thread silently
   and permanently while the `gdbus` child stays alive — which matches every observed symptom.
   Still not proven.

Six minutes before the verdict the same log carries
`wl-paste failed: TimeoutExpired(['wl-paste', '-n', '--type', 'text/plain;charset=utf-8'], 3)`,
so clipboard reads were failing in that window either way.

This defect is the project's signature shape for the third time: it lives **between** two
changes that were each correct and each independently reviewed — v2's signal-counter
discriminator and the later blocking lock. Neither review could have seen it, because when
each was written the other did not exist.

### The fix: the pump stops calling the handler

The pump's entire body becomes two statements:

```python
self._signals += 1
self._event.set()
```

It cannot block and it cannot raise. A separate worker waits on the event, clears it, and
calls `_local_change`. The polling fallback and the safety net signal the same event, so v2's
"one observation path" principle survives intact.

**No queue.** The GPaste signal payload is not used — the handler reads clipboard *state*, not
event contents — so there is nothing to queue. Signals arriving during a long send collapse
into one state re-read afterwards, which is coalescing for free and exactly the semantics a
clipboard wants.

This removes **both** candidate mechanisms by construction rather than by diagnosis: the pump
no longer takes the lock and no longer runs code that can throw. That is why the design does
not gate on establishing which one it was.

**The worker must not become the new blind spot.** Moving the handler out of the pump moves
the hazard with it: a worker that dies leaves the pump alive and the counter climbing, so the
safety net — whose criterion reads the counter — stays silent forever while sync is dead. That
is the same defect one layer down. The rule:

- **Non-fatal exceptions**: log `observer error` with the traceback and continue the loop. A
  single observation is disposable; the next one re-reads the clipboard anyway.
- **Fatal ones** — `BrokenPipeError`, a closed stdout — bring the whole agent down, mirroring
  v1's rule for stdin EOF. The agent is one process per connection by design; exiting is how
  it reports a dead channel.

### The verdict now needs two consecutive ticks — and why the one-tick rule died

v2 ruled that the safety net declares the event source dead from **one** tick: content
changed while the signal counter stood still. That ruling was valid, and it is now reversed.
The reason matters more than the fact.

The one-tick verdict was only ever safe because the poll loop called the handler
**synchronously**. `_local_change` always forks `wl-paste`, so several milliseconds always
elapsed between the content read and the verdict — enough for a signal already in flight to
be counted. v2's review accepted the residual race on exactly that basis.

**Decoupling the loop from the handler deletes that grace period.** It was an accidental
side effect of the synchronous call, nobody named it as load-bearing, and the change that
removed it was correct for its own reasons. The result: a copy landing in the last few
milliseconds before a tick's read can be judged missed while its `Update` is still in
flight — a false "source is dead", degrading a healthy installation to 1-second polling for
the rest of the connection. That is the very defect this release exists to fix.

**The rule: a divergence must survive two consecutive ticks before the verdict fires.** The
discriminator compares two asynchronous observation streams — content and counter — with no
happens-before between them, and any single-point check of such a pair has a window. It
closes by synchronisation or by hysteresis. Synchronisation was rejected: an acknowledgement
from the worker re-couples the poll thread to it and restores the wedge path the decoupling
exists to remove. So: hysteresis. The divergence must outlive a whole 30-second tick, three
orders of magnitude longer than any signal lag, and a false verdict would need two
independent millisecond-window hits on consecutive ticks.

The cost is worst-case detection moving from 30 to 60 seconds. It is the right trade because
the two errors are not symmetric: a false positive permanently degrades a **healthy**
machine, while a late true positive costs thirty extra seconds on one that is already not
syncing.

**Do not restore the one-tick verdict without restoring the synchronous call.** A future
reader will see two ticks where one would do. This is the project's signature defect —
correctness living between two individually correct changes — caught before production for
the first time. The general lesson is worth as much as the specific rule: **a ruling is
attached to its premise; delete the premise and the ruling must be re-examined.**

Order within a tick is load-bearing and free: read the content first, snapshot the counter
after. The `wl-paste` fork at the start of the tick hands the first strike back a
millisecond of grace, and the second tick covers the tail.

**Backlog, explicitly out of scope here:** degradation is one-way — once the safety net
switches to polling it stays there for the connection. Re-probing the event path every few
minutes and returning to it when signals resume would soften both a false verdict and a real
but temporary failure, such as the extension being re-enabled without a reboot.

**Diagnosis stays permanently**, as insurance against a mechanism nobody has named yet:

- The safety net's verdict line gains `signals`, `signals_at_last_tick`, `pump_alive` and
  `worker_alive`. With those four fields the next occurrence is diagnosable from the log alone,
  which this one was not.
- The verdict's wording stops asserting a cause it cannot know. It currently blames the
  gnome-shell extension; it must report what was observed — signals absent while content
  changed — and offer the extension as one possible cause among others.

**The leak fix:** spawn `gdbus` with `prctl(PR_SET_PDEATHSIG, SIGTERM)` via `ctypes` in a
`preexec_fn`, then re-check `os.getppid()` in the same `preexec_fn` — if the parent already
died between the `fork` and the `prctl` call, the signal will never arrive and the child must
exit itself. The kernel then kills the child whenever the agent dies, including `SIGKILL` from
`sshd`. `stop()` keeps its `terminate()`; it stops being the only defence.

**Read timeouts get room for images.** `SUBPROCESS_TIMEOUT` is 3 seconds, chosen for text, and
text reads have already been observed timing out at that bound in production. A 4 MiB image
through a pipe needs more: image reads get 5–10 seconds, and every read logs its duration so
the next time this bound is wrong there is evidence rather than a guess.

## Part 2 — images

### What syncs

**Screenshots and small images, up to 4 MiB.** Larger is skipped with a log naming the size,
matching how oversized text already behaves. Measured for scale: the owner's screenshots run
105–266 KB.

**Text wins when the clipboard holds both.** An image syncs only when there is no text.

This is the reverse of the first decision taken, and the reversal has a concrete cause: Excel,
LibreOffice Calc and Numbers all place a bitmap of the copied cells *alongside* the text. Under
"image wins", copying any spreadsheet range would arrive on the other machine as a picture of
a table — a regression of the primary text flow in exchange for a new feature. "Text wins"
costs nothing real: screenshots carry no text, a browser's "Copy image" carries no
`text/plain`, and office copies carry both and should arrive as text.

Note for anyone re-opening this: the `wl-copy --type` limitation below is about **writing** to
the clipboard, and this priority is about **reading** from it. They are unrelated, and
conflating them once already produced a wrong answer.

### Both representations at once is not possible on the PC

`wl-copy` takes `--type mime/type` in the singular: one invocation, one type, one body. A
Wayland selection has exactly one owner, so two concurrent `wl-copy` processes do not
coexist — the second replaces the first. Placing text and an image on the PC's clipboard
simultaneously is therefore not achievable with wl-clipboard.

Doing it would mean writing a Wayland client with its own data source: an external
dependency, in a project that is stdlib-only by design, and very likely blocked again by the
missing data-control protocol in Mutter that this whole tool exists to work around.

ClipFan reached the same wall from the other side and its architecture notes say so plainly —
*"text-only — write the file's absolute path to the clipboard (`xclip` has no clean
multi-target write)"*. This is a platform constraint, not an implementation shortcut.

macOS has no such limit: one `NSPasteboardItem` carries several representations natively.

### Wire format

A new frame type `0x03`, image-clip, payload `[f64 big-endian ts][PNG bytes]` — the same
timestamp-first shape as the text clip, for the same reason: the receiver must record the
peer's timestamp for content it applies.

**Only `image/png` crosses the wire**, and on the PC that costs nothing: GPaste re-offers
whatever image it holds in a long list of types — measured on the live machine as `image/png`,
`image/webp`, `image/tiff`, `image/jpeg`, `image/bmp`, `image/avif`, `image/jxl` and more — so
the agent always requests `image/png` and always gets it. A JPEG copied from a file manager
was verified to read back as 307,946 bytes of valid PNG. There is no format-skip path on the
PC and the README must not promise one.

macOS is where the conversion lives: screenshots land on the pasteboard as TIFF, which the Mac
converts via `NSBitmapImageRep`.

### The clipboard is not a faithful store — hash what you read, never what you wrote

Measured on the live machine, writing a 105,700-byte PNG to the PC's clipboard:

| When | sha256 (first 16) | Size |
|---|---|---|
| written | `0f22396b3f46ee45` | 105,700 |
| read back at t+1s | `0f22396b3f46ee45` | 105,700 |
| read back at t+4s | `09feee69b989fcab` | **180,287** |
| read back at t+7s | `09feee69b989fcab` | 180,287 |

GPaste takes over selection ownership a few seconds after the write and **re-encodes the
image**. The result is stable afterwards, but it is not what was written, and it was 70%
larger. The same probe on text returns the written bytes unchanged.

This is the same disease as `trim-items`, which silently stripped whitespace and looked like a
sync bug: **the clipboard does not necessarily hold what you put in it.**

Two failures follow if the hash is taken from what was written:

1. **A guaranteed extra round trip on every screenshot Mac→PC.** The PC writes PNG₀ (hash A),
   GPaste re-offers PNG₁ (hash B), which raises an `Update`; the worker reads B, does not
   recognise it as our own write, and sends up to 4 MiB straight back. It converges in one
   round because the re-offer is stable, but it wastes the transfer and replaces the Mac's
   clipboard with a re-encoded copy every time.
2. **A systematic false `clipboard changed while apart`.** The store holds hash A while the
   clipboard offers B, so *every* reconnect — that is, every Mac wake — sees a mismatch,
   stamps `ts = now`, and lets a stale image win reconciliation against anything, including
   fresher text on the Mac. That is a direct corruption of freshness, the exact class v2 was
   built to close.

**The invariant, one line:** every hash that reaches `_last_seen`, the echo guard, the
persistent store or a clip-state frame is the hash of bytes **read from the clipboard**, never
of bytes handed to the write tool. For text the two coincide; for images on the PC they
provably do not.

**The mechanism must not depend on guessing when the takeover lands.** The measurement above
puts it between one and four seconds, and that is one observation on one machine — a fixed
sleep would be a race dressed as a constant. Instead the PC consumes **the first subsequent
image-kind observation as its own re-offer**: it records the read-back hash and does not send.
This mirrors the echo guard's existing rule, which is consumed by the first observed change
whatever it is, and which exists because a match-only version was a real v1 defect.

**Size is checked after the read-back, not before the write.** A 105 KB image became 180 KB;
an image near the 4 MiB limit can cross it on re-encode. The limit applies to what will
actually be hashed and sent.

### The size limits must be separated

A 4 MiB image plus its 8-byte timestamp does not fit a 4 MiB frame cap, so under v2's single
constant a maximum-size image would be permanently skipped while appearing to be within the
limit — and the receiving decoder closes the channel on an oversized frame.

Three explicit constants:

| Bound | Value |
|---|---|
| Frame payload cap | 8 MiB |
| Image content limit | 4 MiB |
| Text content limit | 4 MiB, unchanged |

`PROTOCOL_VERSION` becomes **3**. A half-updated deployment fails loudly through the existing
`hello` mismatch handling.

### Where the feature leaks — this is the bulk of the work, not the codec

v2 assumes "content is text" in every path it touches. Each of these must learn about content
kind before the feature is correct:

- **Reconciliation's send branch reads text directly.** With an image on the clipboard after a
  reboot, reconciliation would send the wrong thing — or nothing.
- **Clip-state and the persistent store carry a hash and a timestamp, but no kind.** A hash
  alone cannot tell the two sides what they are agreeing about.
- **`_last_seen` holds and compares text.** It becomes `(kind, hash)` for **both** kinds — not
  text compared by value and images by hash. Two comparison branches is exactly the mirrored
  drift this project has been bitten by twice; one rule for both costs nothing and keeps 4 MiB
  of pixels out of memory.
- **"Bytes that are not valid UTF-8" is currently an error path** that logs and drops. It
  becomes, in part, the signal that the clipboard holds an image.
- **The startup seed** — `resolveCurrentClipState` / `resolve_current_clip_state` — reads the
  clipboard to decide whether it changed while the agent was away. It must read the same way
  every other path does, or a reconnect with an image on the clipboard misreports.
- **The size guards are per-kind now.** Two limits exist; whichever guard runs must pick by
  kind rather than assume text.
- **The polling fallback reads the whole clipboard body every tick.** Reading 4 MiB every
  400 ms is not acceptable; in degraded mode the image body must be checked off a change in
  `wl-paste --list-types` rather than the content itself, with the added latency documented.

Grep both implementations for `text` and `readText` and treat the result as the starting task
list — but note that the three rules below are behaviour, not identifiers, and grep will not
find them.

### Three rules the implementers would otherwise each invent differently

**One canonical clipboard read.** A single function, used by the watcher, the startup seed,
`clipboard_became_ready` and the reconciliation send branch alike: list the types, prefer
`text/plain`, fall back to `image/png`, and return `(kind, bytes)`. Today the read order is
written down only for the degraded path, which is how four call sites end up with four
answers.

**Clip-state and the store carry the kind.** Frame `0x02`'s JSON gains a `kind` field
(`"text"` or `"image"`, `null` when the hash is null), and the persistent store gains the same.
This is a wire change and belongs with `0x03`, with golden vectors covering both kinds and the
null row — the vectors are what have kept the two codecs honest through two protocol versions.

**The send branch verifies before it sends.** When reconciliation resolves to send, read the
content of the kind recorded in `mine`, hash what was read, and compare it against
`mine.sha256`. On a mismatch, send nothing and log: the clipboard changed between the
announcement and the send, and the watcher will carry the new content on its own. Without this
rule the branch sends whatever it happens to find under the announced timestamp — the wrong
kind, at a stale age, which is a clobber the receiver cannot detect.

### Freshness needs no kind, but the log does

The resolution formula is unchanged and needs no notion of kind: SHA-256 of text and of a PNG
will not collide, so "hashes equal → do nothing" stays safe, differing hashes are decided by
timestamp, and the hex tie-break works across kinds exactly as it does within one.

The **log line** does need it. `reconciled with the peer: sendMine` with both sides holding
different kinds is undiagnosable after the fact — "why did a picture overwrite my text" has no
answer in the current line. Both sides' kinds go into it.

### Secrets

GPaste writes image items to its on-disk history exactly as it writes text. The README's
existing note about a copied password persisting on the PC extends to screenshots, which may
contain more than the person copying them intended.

## If the end-to-end test fails: the v3.1 path

Inline image bytes are the simple design and may not survive contact with two real clipboard
stacks. If acceptance fails — Wayland selection ownership, GPaste's handling of large image
items, or re-encode mismatches — the fallback is **not** to iterate on the inline approach but
to switch to ClipFan's file-path design, described at
<https://github.com/prime-radiant-inc/clipfan/blob/main/docs/ARCHITECTURE.md#image-flow-on-receive-the-load-bearing-trick>.

Its shape, from that document:

- The image is written to a file on the receiving machine.
- **On macOS**, a helper writes a single `NSPasteboardItem` carrying *both* the PNG bytes
  (`public.png`) and the file's path as text (`public.utf8-plain-text`), so graphical
  applications paste the picture and terminal applications paste the path.
- **On Linux**, the clipboard receives *only* the absolute path as text, because the CLI
  clipboard tools have no clean multi-target write — the same constraint documented above.

That design trades clipboard fidelity on Linux for reliability, and it sidesteps both the
size question and the multi-target question at once. It is a different product decision, not a
bug fix, which is why it is scoped as v3.1 rather than folded in here.

## Acceptance — additions to the v2 checklist

The v2 checklist still applies in full. New items:

1. **Screenshot each direction.** The image arrives and opens. Mac→PC must show **one send and
   zero image frames coming back** — that is the test for the re-encode echo rule, and it is
   the item most likely to fail.
2. **Reconnect after a screenshot Mac→PC.** Reconciliation must say `doNothing`, never
   `clipboard changed while apart`. If it says the latter, the store is holding the written
   hash instead of the read-back hash and every wake will clobber.
3. **Spreadsheet copy.** Select cells in a spreadsheet and copy: **text** must arrive, not a
   picture of the table. This is the case that reversed the priority decision.
4. **Re-copying the same screenshot sends nothing.** Copy one screenshot on the Mac twice. If
   `NSBitmapImageRep`'s PNG encoding is not deterministic, this is the only place it surfaces.
5. **Mixed-kind reconciliation.** With the channel dead, put an image on one machine and text
   on the other, then reconnect. The fresher side must win, and the log line must name both
   kinds.
6. **Wake flow with an image.** Copy a screenshot on the PC with the Mac's agent stopped, then
   start it. The screenshot must arrive — the v2 wake-flow test, now with the new kind.
7. **Oversized image.** An image above 4 MiB is skipped, and the log names the size. Note that
   re-encoding inflates: a source image comfortably under the limit can cross it.
8. **No false degrade after large transfers.** Send several images in succession and confirm
   the safety net does not declare the event source dead — the defect this release fixes, made
   more likely by large payloads.
9. **No orphaned children.** After several agent restarts,
   `pgrep -f 'gdbus monitor.*GPaste'` returns exactly one process, owned by the live agent.
10. **Degraded-mode image latency.** With the event path forced to polling, confirm an image
    still arrives and note how long it takes.

## What is deliberately unchanged

The freshness rule and its one shared formula; the tie-break on hex-hash comparison; the frame
envelope's shape; `hello` and its mismatch handling; the persistent store's role;
`_last_seen`'s connect-time seed. v2's reconciliation is working in production and is not
reopened here — only taught about a second kind of content.
