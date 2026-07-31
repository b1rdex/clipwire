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

**Diagnosis stays anyway, permanently**, as insurance against a third mechanism:

- The worker wraps its handler call and logs a traceback prefixed `observer died` if it ever
  exits. A thread dying silently is a defect in its own right.
- The safety net's verdict line gains `signals`, `signals_at_last_tick` and `pump_alive`, so
  the next occurrence is diagnosable from the log alone.
- The verdict's wording stops asserting a cause it cannot know. It currently blames the
  gnome-shell extension; it must report what was observed — signals absent while content
  changed — and name the extension as one possible cause among others.

**The leak fix:** spawn `gdbus` with `prctl(PR_SET_PDEATHSIG, SIGTERM)` via `ctypes` in a
`preexec_fn`. The kernel then kills the child whenever the agent dies, including `SIGKILL`
from `sshd`. `stop()` keeps its `terminate()`; it stops being the only defence.

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

**Only `image/png` crosses the wire.** macOS screenshots land on the pasteboard as TIFF, which
the Mac converts natively via `NSBitmapImageRep`. The PC has nothing in the standard library
to convert with, so any other `image/*` type is skipped with a log naming the type — a JPEG
copied from a file manager will not sync, and the README must say so.

Hashes and echo suppression are computed over the **final PNG bytes actually written and read
back**, never over a pre-conversion representation. Hashing either side of a re-encode makes
the two machines disagree about identical content and starts a ping-pong.

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
- **`_last_seen` holds and compares text.** For images it must hold a hash: keeping 4 MiB of
  pixels resident to answer "did this change" is the wrong trade.
- **"Bytes that are not valid UTF-8" is currently an error path** that logs and drops. It
  becomes, in part, the signal that the clipboard holds an image.
- **The polling fallback reads the whole clipboard body every tick.** Reading 4 MiB every
  400 ms is not acceptable; in degraded mode the image body must be checked less often — off a
  change in `wl-paste --list-types` rather than the content itself — with the added latency
  documented.

Anyone decomposing this into tasks should grep both implementations for `text` and `readText`
first, and treat the result as the task list.

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

1. **Screenshot each direction.** Byte-identical PNG on arrival, one round per clip in the log.
2. **Spreadsheet copy.** Select cells in a spreadsheet and copy: **text** must arrive, not a
   picture of the table. This is the case that reversed the priority decision.
3. **Oversized image.** An image above 4 MiB is skipped, and the log names the size.
4. **Unsupported format.** A JPEG is skipped, and the log names the type.
5. **No false degrade after a large transfer.** Send several images in succession and confirm
   the safety net does not declare the event source dead — the defect this release fixes, made
   more likely by large payloads.
6. **No orphaned children.** After several agent restarts,
   `pgrep -f 'gdbus monitor.*GPaste'` returns exactly one process, owned by the live agent.
7. **Degraded-mode image latency.** With the event path forced to polling, confirm an image
   still arrives and note how long it takes.

## What is deliberately unchanged

The freshness rule and its one shared formula; the tie-break on hex-hash comparison; the frame
envelope's shape; `hello` and its mismatch handling; the persistent store's role;
`_last_seen`'s connect-time seed. v2's reconciliation is working in production and is not
reopened here — only taught about a second kind of content.
