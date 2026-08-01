# ClipWire protocol v3 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Sync images as well as text, and fix the two defects the first day of production use exposed.

**Architecture:** The PC agent's `gdbus` pump stops calling the clipboard handler — it only counts a signal and sets an event, while a separate worker does the work — which removes both candidate causes of the safety net's false positive by construction. Images travel in a new frame type `0x03` shaped like the text clip, and every path that assumed "content is text" learns a content kind. The hash that reaches the echo guard, the store and the wire is always taken from bytes **read back from** the clipboard, because GPaste re-encodes images when it takes over the selection.

**Tech Stack:** Swift 5.9 / AppKit on macOS; Python 3.11+ standard library only on Ubuntu 26.04 / GNOME 50 / Wayland; `wl-clipboard` and GPaste on the PC; golden JSON fixtures shared by both test suites.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-07-31-protocol-v3-images-design.md`, which amends `2026-07-31-protocol-v2-freshness-design.md` and `2026-07-30-clipwire-design.md`. All three are batya-reviewed; do not relitigate their decisions.
- **No third-party dependencies.** Not in the Swift package, not in the agent, not in either test suite.
- **Python floor 3.11.** Production runs 3.14.4. CI matrix is `["3.11", "3.13", "3.14"]`.
- **Swift floor macOS 13.** No bare top-level `let` in the Swift target — namespace constants as `static let` on an enum.
- **Frame envelope is unchanged:** `[u32 BE payload length][u8 type][payload]`, length counting payload only.
- **Frame payload cap becomes 8 MiB** (`8_388_608`). **Text content limit stays 4 MiB** (`4_194_304`). **Image content limit is 4 MiB** (`4_194_304`). These are three separate constants; never reuse one for another.
- **Clip payload (type `0x01`)** stays `[f64 big-endian ts][utf-8 text bytes]`. **Image clip (type `0x03`)** is `[f64 big-endian ts][PNG bytes]`.
- **Clip-state (type `0x02`)** payload JSON becomes `{"sha256": <hex string or null>, "ts": <float>, "kind": <"text" | "image" | null>}`.
- **`PROTOCOL_VERSION = 3`.**
- **Only `image/png` crosses the wire.** On the PC always request `image/png` — GPaste re-offers any image in that type. On the Mac convert TIFF via `NSBitmapImageRep`.
- **Every hash is of bytes read FROM the clipboard, never of bytes written to it.** GPaste re-encodes images on selection takeover; measured, a 105,700-byte PNG read back as a different 180,287-byte PNG.
- **Text wins when the clipboard holds both text and an image.**
- **stdout carries frames and nothing else** on the PC side; diagnostics to stderr.
- **Never append below the `__main__` guard** in `agent/clipwire-agent.py` — `sys.exit(main(...))` means anything after it never exists in a real run while tests still see it. `TestModuleDefinitionOrder` pins this; add a needle for every new module-level name.
- **No test may hang**, require a live peer, a live compositor, or a live GPaste. No `sleep` longer than a few tens of milliseconds in tests.
- **Verification counts only from a clean build:** `rm -rf .build && swift build && swift test`. Use `rtk proxy` if the shell hook compacts output.
- English throughout. The repo is public: no real hostnames, LAN addresses or usernames in tracked files.

## File Structure

| Path | Change |
|---|---|
| `agent/clipwire-agent.py` | Pump/worker split, `prctl` child reaping, canonical read, image support, kind-aware store and state. One deployed file; everything PC-side lands here. |
| `Sources/clipwire/Frame.swift` | Three separate caps, `FrameType.imageClip`, `PROTOCOL_VERSION` 3. |
| `Sources/clipwire/ClipPayload.swift` | Image payload codec beside the text one; both are `[ts][body]`. |
| `Sources/clipwire/Freshness.swift` | `ClipState` gains `kind`; the resolution formula is untouched. |
| `Sources/clipwire/ClipStateStore.swift` | Persists the kind alongside hash and ts. |
| `Sources/clipwire/Pasteboard.swift` | Canonical read returning `(kind, bytes)`; TIFF→PNG; image write. |
| `Sources/clipwire/main.swift` | Image frame handling, kind-aware reconciliation and logging. |
| `fixtures/frames.json` | Type `0x03` vectors; type `0x02` vectors gain `kind`. |
| `fixtures/clipkind.json` | **New.** Canonical read decision table, shared by both suites. |
| `Tests/clipwireTests/*`, `agent/tests/*` | Suites for each of the above. |
| `README.md` | Image behaviour, the GPaste re-encode note, secrets extended to screenshots. |

**A note on file size, deliberately not acted on here.** `main.swift` is 920 lines, `agent/clipwire-agent.py` is 1656, `test_watcher.py` is 2228. All three are past comfortable. Splitting them is worth doing, and this plan does not do it: a protocol version bump and a concurrency rewrite in the same diff as a file split leaves a reviewer unable to tell a moved line from a changed one. It is recorded here as the next piece of work, to be planned on its own once v3 has passed acceptance.

---

## Part A — the observation path

No protocol change, PC side only. Doing this first means the safety net is sound before images start stressing it.

### Task 1: The `gdbus` child dies with its parent

**Files:**
- Modify: `agent/clipwire-agent.py` — `GPasteWatcher.start`
- Test: `agent/tests/test_watcher.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `_pdeathsig_preexec()` — module-level, no arguments, returns `None`, safe to reference on non-Linux (it raises nothing at import time).

Six orphaned `gdbus monitor` processes were found on the production machine, one per agent restart. `stop()` calls `terminate()`, but `sshd` reaps the agent when the channel drops and `stop()` never runs. `terminate()` is also insufficient in principle: glib programs set `SIG_IGN` on `SIGPIPE`, so the orphan survives its output pipe closing.

- [ ] **Step 1: Write the failing test**

```python
class TestPdeathsigPreexec(unittest.TestCase):
    def test_it_requests_sigterm_when_the_parent_dies(self):
        calls = []

        class FakeLibc:
            def prctl(self, option, sig, *rest):
                calls.append((option, sig))
                return 0

        with mock.patch.object(agent, "_load_libc", return_value=FakeLibc()), \
             mock.patch.object(agent.os, "getppid", return_value=42):
            agent._pdeathsig_preexec()

        self.assertEqual(calls, [(agent.PR_SET_PDEATHSIG, signal.SIGTERM)])

    def test_it_exits_when_the_parent_already_died(self):
        """The fork/prctl window: if the parent died in between, the signal
        never arrives, so the child must notice and leave on its own.

        os._exit is mocked rather than expected to raise: it does NOT raise
        SystemExit, it ends the process immediately -- which is correct inside
        a preexec_fn, where an exception would be re-raised in the PARENT and
        take the agent down instead of the child. An assertRaises here would
        kill the test runner.
        """
        class FakeLibc:
            def prctl(self, option, sig, *rest):
                return 0

        with mock.patch.object(agent, "_load_libc", return_value=FakeLibc()), \
             mock.patch.object(agent.os, "getppid", return_value=1), \
             mock.patch.object(agent.os, "_exit") as exit_call:
            agent._pdeathsig_preexec()
        exit_call.assert_called_once_with(0)

    def test_it_is_a_no_op_where_prctl_is_unavailable(self):
        with mock.patch.object(agent, "_load_libc", return_value=None):
            agent._pdeathsig_preexec()   # must not raise
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k TestPdeathsigPreexec`
Expected: FAIL — `AttributeError: module has no attribute '_pdeathsig_preexec'`.

- [ ] **Step 3: Implement, near the other module-level helpers and far above the `__main__` guard**

```python
PR_SET_PDEATHSIG = 1


def _load_libc():
    """libc for prctl, or None where it is unavailable.

    ctypes is standard library, so this costs no dependency. Returns None on
    macOS and anywhere else without a usable libc: the agent only ever runs on
    the PC, but the test suite runs on both, and an import-time failure would
    take the whole module down.
    """
    try:
        import ctypes
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except (ImportError, OSError):
        return None


def _pdeathsig_preexec():
    """Ask the kernel to SIGTERM this child when its parent dies.

    Runs between fork and exec. This is what actually reaps the gdbus child:
    stop() cannot be relied on, because sshd kills the agent outright when the
    channel drops and no cleanup path runs. terminate() is also not enough on
    its own -- glib installs SIG_IGN for SIGPIPE, so the orphan survives its
    stdout closing and lingers until the session ends.
    """
    libc = _load_libc()
    if libc is None:
        return
    libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    # The parent can die between the fork above and the prctl call just made,
    # in which case the signal we just asked for will never be delivered and
    # this child would outlive it anyway. getppid() == 1 means exactly that.
    if os.getppid() == 1:
        os._exit(0)
```

Add `import signal` to the imports if it is not already there, and add `_pdeathsig_preexec`, `_load_libc` and `PR_SET_PDEATHSIG` to `TestModuleDefinitionOrder`'s needle list.

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 -m unittest discover -s agent/tests -k TestPdeathsigPreexec`
Expected: PASS, 3 tests.

- [ ] **Step 5: Wire it into the spawn, with a test that the argument is actually passed**

```python
def test_the_gdbus_child_is_spawned_with_the_pdeathsig_preexec(self):
    with mock.patch.object(agent.subprocess, "Popen") as popen:
        popen.return_value = FakeGPasteProcess([])
        watcher = agent.GPasteWatcher(clipboard=ScriptedReadClipboard([b"a"]))
        watcher.start(lambda: None)
    self.assertIs(popen.call_args.kwargs.get("preexec_fn"), agent._pdeathsig_preexec)
```

In `GPasteWatcher.start`, add `preexec_fn=_pdeathsig_preexec` to the existing `subprocess.Popen(...)` call. Leave `stop()`'s `terminate()` exactly as it is — it stops being the only defence, it does not stop being a defence.

- [ ] **Step 6: Run the whole suite**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS, previous count + 4.

- [ ] **Step 7: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_watcher.py
git commit -m "Make the gdbus child die with the agent, not with its cleanup path"
```

---

### Task 2: The pump stops calling the handler

**Files:**
- Modify: `agent/clipwire-agent.py` — `GPasteWatcher.start`, `GPasteWatcher.stop`
- Test: `agent/tests/test_watcher.py`

**Interfaces:**
- Consumes: `_pdeathsig_preexec` from Task 1.
- Produces: `GPasteWatcher._event` (a `threading.Event`), `GPasteWatcher._worker` (a `threading.Thread`), and `GPasteWatcher.worker_alive()` returning `bool`. Task 3 reads `worker_alive()`.

The pump currently calls `on_change()` — which is `Agent._local_change` — directly. That makes it able to block on `_observe_lock` and able to die of an exception raised anywhere in the handler, silently and permanently, while the `gdbus` child stays alive. Both were candidate causes of the production false positive and neither was ever proven; this task removes both by construction.

**Do not add a queue.** The GPaste signal payload is unused — the handler reads clipboard *state*, not event contents — so there is nothing to queue. Signals arriving during a long send collapse into one state re-read afterwards, which is coalescing for free and exactly what a clipboard wants.

- [ ] **Step 1: Write the failing test**

```python
class TestPumpNeverCallsTheHandler(unittest.TestCase):
    def test_a_slow_handler_does_not_stop_the_pump_counting(self):
        """The pump must keep reading lines while the handler is busy.
        Against the old design the pump blocks inside on_change and the
        second and third signals are never counted."""
        released = threading.Event()
        entered = threading.Event()

        def slow_handler():
            entered.set()
            released.wait(JOIN_TIMEOUT)

        lines = [GPASTE_UPDATE_LINE] * 3
        watcher = agent.GPasteWatcher(clipboard=ScriptedReadClipboard([b"a"]))
        with mock.patch.object(agent.subprocess, "Popen",
                               return_value=FakeGPasteProcess(lines)):
            watcher.start(slow_handler)
            entered.wait(JOIN_TIMEOUT)
            deadline = time.monotonic() + JOIN_TIMEOUT
            while watcher._signals < 3 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(watcher._signals, 3,
                             "the pump must count every line while the handler is busy")
            released.set()
            watcher.stop()

    def test_a_raising_handler_does_not_kill_the_observer(self):
        """One bad observation is disposable. Against a worker with no guard
        the thread dies and every later signal is silently lost."""
        seen = []

        def handler():
            seen.append(1)
            if len(seen) == 1:
                raise ValueError("first observation explodes")

        lines = [GPASTE_UPDATE_LINE] * 2
        watcher = agent.GPasteWatcher(clipboard=ScriptedReadClipboard([b"a"]))
        with mock.patch.object(agent.subprocess, "Popen",
                               return_value=FakeGPasteProcess(lines)):
            watcher.start(handler)
            deadline = time.monotonic() + JOIN_TIMEOUT
            while len(seen) < 2 and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertEqual(len(seen), 2, "the observer must survive a raising handler")
            self.assertTrue(watcher.worker_alive())
            watcher.stop()
```

`GPASTE_UPDATE_LINE` already exists in this file as the parsed-signal literal; reuse it rather than writing a second copy.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k TestPumpNeverCallsTheHandler`
Expected: FAIL — the first test on `watcher._signals == 1`, the second on `len(seen) == 1`.

- [ ] **Step 3: Implement the split**

Replace `GPasteWatcher.start`'s body with:

```python
    def start(self, on_change):
        self._process = subprocess.Popen(
            ["gdbus", "monitor", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=clipboard_env(), preexec_fn=_pdeathsig_preexec,
        )

        def pump():
            # Two statements, deliberately. This thread must never block and
            # never raise: it is the only thing that keeps _signals honest,
            # and _signals is what the safety net uses to tell a dead event
            # source from a live one. Calling the handler here -- as this did
            # until v3 -- made it able to block on Agent._observe_lock and
            # able to die of any exception the handler raised, silently and
            # for the rest of the connection, while this process stayed alive.
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    self._signals += 1
                    self._event.set()

        def observe():
            # Coalescing falls out of the Event: signals arriving while
            # on_change is running collapse into one set() and produce a
            # single re-read afterwards. That is the right semantics for a
            # clipboard -- the handler reads current STATE, not the contents
            # of any particular event, and GPaste's Update payload carries
            # nothing we use.
            while not self._stop.is_set():
                self._event.wait()
                if self._stop.is_set():
                    return
                self._event.clear()
                try:
                    on_change()
                except (BrokenPipeError, ValueError) as error:
                    # Fatal: the channel is gone. ValueError is what a closed
                    # stdout raises on write. Mirrors v1's rule for stdin EOF
                    # -- this agent is one process per connection, and exiting
                    # IS how it reports a dead channel.
                    log("observer stopping, the channel is gone: %r" % error)
                    os._exit(0)
                except Exception:
                    # Non-fatal: one observation is disposable, the next one
                    # re-reads the clipboard anyway. Logged rather than
                    # swallowed, because a thread that dies quietly here is
                    # the exact defect this task exists to remove.
                    log("observer error: %s" % traceback.format_exc())

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()
        self._worker = threading.Thread(target=observe, daemon=True)
        self._worker.start()
```

In `__init__` add `self._event = threading.Event()` and `self._worker = None`. In `stop()`, after `self._stop.set()`, add `self._event.set()` so a worker parked in `wait()` wakes up and sees the stop flag. Add `import traceback` to the imports.

Add:

```python
    def worker_alive(self):
        """For the safety net's verdict line. A live pump with a dead worker
        is silent failure: the counter keeps climbing, so nothing detects it."""
        return self._worker is not None and self._worker.is_alive()
```

- [ ] **Step 4: Run it and watch it pass**

Run: `python3 -m unittest discover -s agent/tests -k TestPumpNeverCallsTheHandler`
Expected: PASS, 2 tests.

- [ ] **Step 5: Prove the fatal path exits rather than logging**

```python
def test_a_broken_pipe_takes_the_agent_down(self):
    """A dead channel is not a disposable observation. Exiting is how a
    one-process-per-connection agent reports it."""
    def handler():
        raise BrokenPipeError("peer went away")

    watcher = agent.GPasteWatcher(clipboard=ScriptedReadClipboard([b"a"]))
    with mock.patch.object(agent.subprocess, "Popen",
                           return_value=FakeGPasteProcess([GPASTE_UPDATE_LINE])), \
         mock.patch.object(agent.os, "_exit") as exit_call:
        watcher.start(handler)
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not exit_call.called and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(exit_call.called, "a BrokenPipeError must take the agent down")
        watcher.stop()
```

- [ ] **Step 6: Run the whole suite**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS. Every pre-existing `GPasteWatcher` test must still pass — if one now needs a sleep or a retry to see its callback, say so in the report rather than adding one, because that is the coalescing changing observable behaviour.

- [ ] **Step 7: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_watcher.py
git commit -m "Split the gdbus pump from the handler it used to call"
```

---

### Task 3: The verdict line reports what was observed

**Files:**
- Modify: `agent/clipwire-agent.py` — `GPasteWatcher._observe_tick`
- Test: `agent/tests/test_watcher.py`

**Interfaces:**
- Consumes: `worker_alive()` from Task 2.
- Produces: nothing later tasks depend on.

The production line read `GPaste is not reporting clipboard changes (is the gnome-shell extension enabled?)`. On the machine that logged it, the extension was enabled and active, the bus name was owned, and a direct probe caught three `Update` signals for three copies. The line asserted a cause it could not know, and carried no evidence, which is why the mechanism was never established.

- [ ] **Step 1: Write the failing test**

```python
def test_the_verdict_reports_evidence_and_does_not_assert_a_cause(self):
    watcher = self._degraded_watcher()          # existing helper in this file
    line = self._switch_line()                  # existing helper: the logged line
    self.assertIn("signals=", line)
    self.assertIn("signals_at_last_tick=", line)
    self.assertIn("pump_alive=", line)
    self.assertIn("worker_alive=", line)
    self.assertNotIn("is the gnome-shell extension enabled?", line,
                     "the verdict must not assert a cause it cannot know")
```

If `_degraded_watcher`/`_switch_line` do not exist under those names, use whatever the existing degrade tests in this file already use to drive and capture the switch — do not add a second harness.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k test_the_verdict_reports_evidence`
Expected: FAIL on the first `assertIn`.

- [ ] **Step 3: Rewrite the line**

```python
            log("GPaste reported no clipboard change while the content changed "
                "(signals=%d signals_at_last_tick=%d pump_alive=%s worker_alive=%s); "
                "the gnome-shell extension being disabled is one possible cause. "
                "Polling every %gs for the rest of this connection."
                % (signals, self._signals_at_last_tick,
                   self._thread.is_alive() if self._thread else False,
                   self.worker_alive(), self._degraded_interval))
```

`%g` rather than `%.1f`: the tests drive this with millisecond intervals, where `%.1f` renders `0.0s` and makes the test output lie about what the code did.

Update `SWITCH_MARKER` in the test file to a substring that still appears — `"reported no clipboard change"` — and check that `make_watcher`'s already-degraded line still deliberately avoids that marker, so the log-once assertions keep working.

- [ ] **Step 4: Run it and watch it pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_watcher.py
git commit -m "Report the evidence in the degrade verdict instead of guessing the cause"
```

---

## Part B — images

Wire changes from here on. Both implementations move together, and the golden fixtures are what keep them honest — that has held through two protocol versions and is not to be weakened.

### Task 4: Three caps, one new frame type, protocol 3

**Files:**
- Modify: `Sources/clipwire/Frame.swift`, `agent/clipwire-agent.py`
- Test: `Tests/clipwireTests/FrameTests.swift`, `agent/tests/test_frame.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: Swift `FrameConstants.maxPayloadBytes = 8_388_608`, `FrameConstants.maxTextBytes = 4_194_304`, `FrameConstants.maxImageBytes = 4_194_304`, `FrameType.imageClip` (raw value `0x03`), `ProtocolConstants.version = 3`. Python `MAX_PAYLOAD_BYTES = 8388608`, `MAX_TEXT_BYTES = 4194304`, `MAX_IMAGE_BYTES = 4194304`, `TYPE_IMAGE_CLIP = 0x03`, `PROTOCOL_VERSION = 3`.

A 4 MiB image plus its 8-byte timestamp does not fit a 4 MiB frame cap. Under v2's single constant a maximum-size image would be permanently skipped while appearing to be inside the limit, and the receiving decoder closes the channel on an oversized frame. The three bounds are genuinely different numbers that happen to share values today; conflating them is what produced this defect.

- [ ] **Step 1: Write the failing tests**

```python
class TestV3Constants(unittest.TestCase):
    def test_the_frame_cap_is_larger_than_either_content_limit(self):
        self.assertEqual(agent.MAX_PAYLOAD_BYTES, 8388608)
        self.assertEqual(agent.MAX_TEXT_BYTES, 4194304)
        self.assertEqual(agent.MAX_IMAGE_BYTES, 4194304)
        self.assertGreater(agent.MAX_PAYLOAD_BYTES,
                           agent.MAX_IMAGE_BYTES + agent.TIMESTAMP_BYTES,
                           "a maximum-size image plus its ts must fit in a frame")

    def test_the_image_clip_type_is_known(self):
        self.assertEqual(agent.TYPE_IMAGE_CLIP, 0x03)
        self.assertIn(agent.TYPE_IMAGE_CLIP, agent._KNOWN_TYPES)

    def test_the_protocol_version_is_three(self):
        self.assertEqual(agent.PROTOCOL_VERSION, 3)
```

```swift
func testTheFrameCapIsLargerThanEitherContentLimit() {
    XCTAssertEqual(FrameConstants.maxPayloadBytes, 8_388_608)
    XCTAssertEqual(FrameConstants.maxTextBytes, 4_194_304)
    XCTAssertEqual(FrameConstants.maxImageBytes, 4_194_304)
    XCTAssertGreaterThan(FrameConstants.maxPayloadBytes,
                         FrameConstants.maxImageBytes + FrameConstants.timestampBytes)
}

func testTheImageClipTypeIsKnown() {
    XCTAssertEqual(FrameType.imageClip.rawValue, 0x03)
    XCTAssertEqual(FrameType(rawValue: 0x03), .imageClip)
}

func testTheProtocolVersionIsThree() {
    XCTAssertEqual(ProtocolConstants.version, 3)
}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k TestV3Constants` and `rtk proxy swift test --filter FrameTests`
Expected: FAIL — the constants do not exist and `maxPayloadBytes` is still `4_194_304`.

- [ ] **Step 3: Implement**

Python, at the top beside the existing constants:

```python
# Three separate bounds, and they must stay separate even while two of them
# hold the same number. The frame cap is what the decoder enforces; the two
# content limits are what the senders enforce BEFORE wrapping a body in its
# 8-byte timestamp. v2 used one constant for all of it, which made a
# maximum-size image unsendable while looking like it was within the limit.
MAX_PAYLOAD_BYTES = 8388608
MAX_TEXT_BYTES = 4194304
MAX_IMAGE_BYTES = 4194304
TYPE_IMAGE_CLIP = 0x03
PROTOCOL_VERSION = 3
_KNOWN_TYPES = (TYPE_HELLO, TYPE_CLIP, TYPE_CLIP_STATE, TYPE_IMAGE_CLIP)
```

Swift, in `Frame.swift`: add `maxTextBytes` and `maxImageBytes` as `static let` on `FrameConstants`, change `maxPayloadBytes` to `8_388_608`, add `case imageClip = 0x03` to `FrameType`, and set `ProtocolConstants.version = 3` in `main.swift`.

Then find every existing use of the old single cap on both sides and repoint it: the decoder keeps `MAX_PAYLOAD_BYTES`, and the two text send guards (`agent/clipwire-agent.py:576` and `:902`, plus their Swift counterparts) become `MAX_TEXT_BYTES`. Getting this wrong is silent — the tests below will not catch a guard that checks the frame cap instead of the text cap, so read each call site.

- [ ] **Step 4: Run them and watch them pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rm -rf .build && rtk proxy swift build && rtk proxy swift test`
Expected: PASS both suites.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/Frame.swift Sources/clipwire/main.swift agent/clipwire-agent.py Tests/clipwireTests/FrameTests.swift agent/tests/test_frame.py
git commit -m "Separate the frame cap from the two content limits, and add type 0x03"
```

---

### Task 5: The image clip codec, both languages, one golden vector

**Files:**
- Modify: `Sources/clipwire/ClipPayload.swift`, `agent/clipwire-agent.py`, `fixtures/frames.json`
- Test: `Tests/clipwireTests/ClipPayloadTests.swift`, `agent/tests/test_clip_payload.py`, `Tests/clipwireTests/FixtureTests.swift`, `agent/tests/test_fixtures.py`

**Interfaces:**
- Consumes: `TYPE_IMAGE_CLIP` / `FrameType.imageClip`, `MAX_IMAGE_BYTES` / `FrameConstants.maxImageBytes` from Task 4.
- Produces: Python `encode_image_payload(ts, png_bytes) -> bytes` and `decode_image_payload(payload) -> (float, bytes)`, raising `ClipPayloadError`. Swift `ImagePayload.encode(ts:png:) throws -> Data` and `ImagePayload.decode(_:) throws -> (ts: Double, png: Data)`.

The payload is `[f64 big-endian ts][PNG bytes]` — the same shape as the text clip, for the same reason: the receiver must record the *peer's* timestamp for content it applies. The only difference is that the body is opaque bytes rather than UTF-8, so there is no decode step that can reject the body.

- [ ] **Step 1: Write the failing tests**

```python
class TestImagePayload(unittest.TestCase):
    def test_round_trip(self):
        png = b"\x89PNG\r\n\x1a\n" + b"body"
        ts, body = agent.decode_image_payload(agent.encode_image_payload(1785400000.5, png))
        self.assertEqual(ts, 1785400000.5)
        self.assertEqual(body, png)

    def test_an_empty_body_is_rejected(self):
        with self.assertRaises(agent.ClipPayloadError):
            agent.decode_image_payload(agent.encode_image_payload(1.0, b""))

    def test_a_payload_shorter_than_the_timestamp_is_rejected(self):
        with self.assertRaises(agent.ClipPayloadError):
            agent.decode_image_payload(b"\x00\x00\x00")

    def test_a_non_finite_ts_is_rejected_on_both_sides(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ts=bad):
                with self.assertRaises(agent.ClipPayloadError):
                    agent.encode_image_payload(bad, b"x")
        # and on decode, since the wire is peer-controlled
        payload = struct.pack(">d", float("nan")) + b"x"
        with self.assertRaises(agent.ClipPayloadError):
            agent.decode_image_payload(payload)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k TestImagePayload`
Expected: FAIL — `AttributeError: module has no attribute 'encode_image_payload'`.

- [ ] **Step 3: Implement, beside the text codec**

```python
def encode_image_payload(ts, png):
    """type-0x03 payload: [f64 BE ts][PNG bytes].

    Same shape as the text clip and for the same reason -- the receiver has to
    record the PEER's timestamp for content it applies, and it cannot record
    what the wire never carried. The body is opaque here: PNG validity is the
    business of whoever read it off a clipboard, not of the codec.
    """
    if not math.isfinite(ts):
        raise ClipPayloadError("refusing to encode a non-finite ts: %r" % ts)
    return struct.pack(">d", ts) + png


def decode_image_payload(payload):
    if len(payload) < TIMESTAMP_BYTES:
        raise ClipPayloadError("image payload shorter than its timestamp")
    (ts,) = struct.unpack(">d", payload[:TIMESTAMP_BYTES])
    if not math.isfinite(ts):
        raise ClipPayloadError("refusing a non-finite ts: %r" % ts)
    body = payload[TIMESTAMP_BYTES:]
    if not body:
        raise ClipPayloadError("image payload carries no image")
    return ts, body
```

Swift `ImagePayload` mirrors it exactly, throwing `ClipPayloadError.nonFiniteTimestamp`, `.truncated` and a new `.emptyBody`, and using `Double(bitPattern: UInt64(bigEndian:))` the way `ClipPayload` already does. Reuse `ClipPayloadError`; do not add a second error type.

- [ ] **Step 4: Run them and watch them pass**

Run: `python3 -m unittest discover -s agent/tests -k TestImagePayload` and `rtk proxy swift test --filter ClipPayloadTests`
Expected: PASS.

- [ ] **Step 5: Add the golden vector both suites read**

In `fixtures/frames.json`, add an entry alongside the existing ones:

```json
{
  "name": "image-clip",
  "type": 3,
  "ts": 1785400000.5,
  "body_hex": "89504e470d0a1a0a0000000d49484452",
  "frame_hex": "0000001803...."
}
```

Compute `frame_hex` from the real encoder rather than by hand — write it once with a throwaway script, paste the result, and then let both suites verify it. **Pin the type byte as the literal `3`, not as a name that resolves through the enum**: a vector that routes the byte through the enum and back cannot detect the enum being relabelled, which was a real defect in v1.

Both fixture tests must decode this vector and assert the ts and the body bytes.

- [ ] **Step 6: Run both suites**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rm -rf .build && rtk proxy swift build && rtk proxy swift test`
Expected: PASS both.

- [ ] **Step 7: Commit**

```bash
git add Sources/clipwire/ClipPayload.swift agent/clipwire-agent.py fixtures/frames.json Tests/clipwireTests Tests/clipwireTests/FixtureTests.swift agent/tests
git commit -m "Add the image clip codec on both sides, pinned by a shared vector"
```

---

### Task 6: Clip-state and the store carry a kind

**Files:**
- Modify: `Sources/clipwire/Freshness.swift`, `Sources/clipwire/ClipStateStore.swift`, `agent/clipwire-agent.py`, `fixtures/frames.json`
- Test: `Tests/clipwireTests/FreshnessTests.swift`, `Tests/clipwireTests/ClipStateStoreTests.swift`, `agent/tests/test_freshness.py`, `agent/tests/test_clip_state_store.py`

**Interfaces:**
- Consumes: nothing from Task 5.
- Produces: Swift `ClipState(sha256: String?, ts: Double, kind: ClipKind?)` where `enum ClipKind: String, Codable { case text, image }`. Python: clip-state tuples become `(sha256, ts, kind)` with `kind` one of `"text"`, `"image"` or `None`; `encode_clip_state(sha256, ts, kind)`, `decode_clip_state(payload) -> (sha256, ts, kind)`, `save_clip_state(sha256, ts, kind, path=None)`, `load_clip_state(path=None) -> (sha256, ts, kind) | None`.

A hash alone cannot tell the two sides what they are agreeing about. **The resolution formula does not change and must not**: SHA-256 of text and of a PNG will not collide, so hash equality stays safe, differing hashes are decided by timestamp, and the hex tie-break works across kinds exactly as within one. The kind is for the send branch (Task 11) and for the log (Task 14).

- [ ] **Step 1: Write the failing tests**

```python
class TestClipStateKind(unittest.TestCase):
    def test_round_trip_carries_the_kind(self):
        for kind in ("text", "image"):
            with self.subTest(kind=kind):
                payload = agent.encode_clip_state("ab" * 32, 1.5, kind)
                self.assertEqual(agent.decode_clip_state(payload), ("ab" * 32, 1.5, kind))

    def test_a_null_hash_carries_a_null_kind(self):
        payload = agent.encode_clip_state(None, 1.5, None)
        self.assertEqual(agent.decode_clip_state(payload), (None, 1.5, None))

    def test_an_unknown_kind_is_rejected(self):
        """Peer-controlled input. An unknown kind must not reach the send
        branch, which switches on it."""
        payload = json.dumps({"sha256": "ab" * 32, "ts": 1.5, "kind": "video"}).encode()
        with self.assertRaises(agent.ClipStateError):
            agent.decode_clip_state(payload)

    def test_a_hash_without_a_kind_is_rejected(self):
        payload = json.dumps({"sha256": "ab" * 32, "ts": 1.5, "kind": None}).encode()
        with self.assertRaises(agent.ClipStateError):
            agent.decode_clip_state(payload)

    def test_the_store_round_trips_the_kind(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "clip-state.json")
            agent.save_clip_state("cd" * 32, 9.0, "image", path=path)
            self.assertEqual(agent.load_clip_state(path=path), ("cd" * 32, 9.0, "image"))
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k TestClipStateKind`
Expected: FAIL — `encode_clip_state()` takes 2 arguments.

- [ ] **Step 3: Implement**

Add to the Python module, near the other constants:

```python
KIND_TEXT = "text"
KIND_IMAGE = "image"
_KNOWN_KINDS = (KIND_TEXT, KIND_IMAGE)
```

`encode_clip_state(sha256, ts, kind)` writes `{"sha256": ..., "ts": ..., "kind": ...}`. `decode_clip_state` validates: `kind` must be `None` exactly when `sha256` is `None`, and otherwise must be in `_KNOWN_KINDS`; anything else raises `ClipStateError`. Keep the existing `sha256` hex validation exactly as it is.

Swift: add `enum ClipKind: String, Codable { case text, image }`, add `let kind: ClipKind?` to `ClipState`, and enforce the same two rules in `decodePayload` — a hash with no kind and a kind with no hash are both malformed. `ClipStateStore` persists the field by virtue of `Codable`; add a store test that a file written by v2 (no `kind` key, non-null hash) is **rejected** rather than silently loaded as kindless, and say so in a comment: a v2 store on disk after an upgrade is a real situation, and treating it as "hash of unknown kind" would feed the send branch a state it cannot act on.

- [ ] **Step 4: Run them and watch them pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rtk proxy swift test`
Expected: PASS. Every existing call site of these four functions changes; the compiler finds them on the Swift side and the tests find them on the Python side.

- [ ] **Step 5: Extend the golden vectors**

Every type-`0x02` vector in `fixtures/frames.json` gains a `kind`, and one new vector covers the null-hash/null-kind row. Both fixture suites assert the decoded triple.

- [ ] **Step 6: Confirm the formula did not change**

Run: `rtk proxy swift test --filter FreshnessTests` and `python3 -m unittest discover -s agent/tests -k test_freshness`
Expected: PASS with **no edits to `fixtures/freshness.json`**. If a freshness test needed changing, the formula was touched — stop and report it.

- [ ] **Step 7: Commit**

```bash
git add Sources/clipwire agent/clipwire-agent.py fixtures/frames.json Tests/clipwireTests agent/tests
git commit -m "Carry the content kind in clip-state and the persistent store"
```

---

### Task 7: One canonical clipboard read on the PC

**Files:**
- Modify: `agent/clipwire-agent.py` — `WaylandClipboard`
- Create: `fixtures/clipkind.json`
- Test: `agent/tests/test_clipboard.py`

**Interfaces:**
- Consumes: `KIND_TEXT`, `KIND_IMAGE` from Task 6.
- Produces: `WaylandClipboard.read()` returning `(kind, bytes)` or `None`; `WaylandClipboard.write(kind, data)`. Tasks 9-12 use both.

Four call sites read the clipboard today — the watcher, the startup seed, `clipboard_became_ready` and the reconciliation send branch — and the read order is written down only for the degraded path. That is how four call sites end up with four answers. One function, used by all of them.

**Text wins.** An image syncs only when there is no text. GPaste re-offers any image as `image/png`, verified on the live machine including a JPEG that read back as valid PNG, so there is no format-skip path here.

- [ ] **Step 1: Write the failing test, driven by a shared fixture**

`fixtures/clipkind.json`:

```json
[
  {"name": "text only",        "types": ["text/plain;charset=utf-8", "TEXT"], "expect": "text"},
  {"name": "image only",       "types": ["image/png", "image/tiff", "TARGETS"], "expect": "image"},
  {"name": "both, text wins",  "types": ["text/plain;charset=utf-8", "image/png"], "expect": "text"},
  {"name": "neither",          "types": ["TARGETS", "TIMESTAMP"], "expect": null},
  {"name": "empty",            "types": [], "expect": null}
]
```

```python
# Note the path shape: the existing FIXTURES in test_freshness.py points at a
# FILE, not a directory, so do not os.path.join onto it.
CLIPKIND_FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "clipkind.json"


class TestCanonicalRead(unittest.TestCase):
    def test_the_kind_matches_the_shared_fixture(self):
        rows = json.loads(CLIPKIND_FIXTURE.read_text())
        for row in rows:
            with self.subTest(row["name"]):
                self.assertEqual(agent.choose_kind(row["types"]), row["expect"])

    def test_read_returns_the_text_body_when_text_is_present(self):
        clip = agent.WaylandClipboard()
        with mock.patch.object(agent.subprocess, "run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\nimage/png\n"),
            _completed(stdout=b"hello"),
        ]):
            self.assertEqual(clip.read(), (agent.KIND_TEXT, b"hello"))

    def test_read_asks_for_png_when_only_an_image_is_present(self):
        clip = agent.WaylandClipboard()
        with mock.patch.object(agent.subprocess, "run", side_effect=[
            _completed(stdout=b"image/tiff\nimage/png\n"),
            _completed(stdout=b"\x89PNG..."),
        ]) as run:
            self.assertEqual(clip.read(), (agent.KIND_IMAGE, b"\x89PNG..."))
        self.assertIn("image/png", run.call_args.args[0])
```

`_completed` is a small helper returning a `subprocess.CompletedProcess` with returncode 0; add it beside the existing helpers in this file if one does not already exist.

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k TestCanonicalRead`
Expected: FAIL — no `choose_kind`, and `read()` returns bytes rather than a pair.

- [ ] **Step 3: Implement**

```python
def choose_kind(types):
    """Which kind to sync, given the clipboard's offered MIME types.

    Text wins. Spreadsheets put a bitmap of the copied cells alongside the
    text, so preferring the image would turn every copied range into a picture
    of a table -- a regression of the primary flow in exchange for the new one.
    Screenshots and "Copy image" carry no text/plain, so they still arrive as
    images.

    Only image/png is considered, and on the PC that costs nothing: GPaste
    re-offers whatever image it holds in a long list of types, PNG among them,
    verified on the live machine down to a JPEG reading back as valid PNG.
    """
    if any(t.startswith("text/plain") or t in ("UTF8_STRING", "STRING", "TEXT")
           for t in types):
        return KIND_TEXT
    if "image/png" in types:
        return KIND_IMAGE
    return None
```

`WaylandClipboard.read()` runs `wl-paste --list-types`, calls `choose_kind`, and then reads the chosen type — `text/plain;charset=utf-8` with `-n`, or `image/png` — returning `(kind, body)`, or `None` when `choose_kind` returns `None` or the body is empty. Keep the existing one-shot timeout logging exactly as it is. `write(kind, data)` picks `--type image/png` for images and the current text path otherwise.

Give image reads a longer timeout than text: text reads have already been seen timing out at 3 seconds in production, and a 4 MiB body through a pipe needs more. Add `IMAGE_SUBPROCESS_TIMEOUT = 10` and log every read's duration, so the next time this bound is wrong there is evidence rather than a guess.

- [ ] **Step 4: Run it and watch it pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS. Every existing caller of `read()` now gets a pair — fix them mechanically here; the behaviour changes land in Tasks 9-12.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py fixtures/clipkind.json agent/tests/test_clipboard.py
git commit -m "Read the PC clipboard through one function that reports its kind"
```

---

### Task 8: One canonical clipboard read on the Mac

**Files:**
- Modify: `Sources/clipwire/Pasteboard.swift`
- Test: `Tests/clipwireTests/PasteboardTests.swift`

**Interfaces:**
- Consumes: `ClipKind` from Task 6, `fixtures/clipkind.json` from Task 7.
- Produces: `PasteboardReading.read() -> (kind: ClipKind, data: Data)?` replacing `readText()`, and `PasteboardWriting.write(kind: ClipKind, data: Data)` replacing `writeText(_:)`.

macOS screenshots land on the pasteboard as TIFF, so this side owns the conversion: `NSBitmapImageRep(data:)` then `representation(using: .png, properties: [:])`. The same text-wins rule applies, and the shared fixture is read here too so the two sides cannot drift on it.

- [ ] **Step 1: Write the failing test**

```swift
func testTheKindMatchesTheSharedFixture() throws {
    // Same fixture the Python suite reads. Two implementations of one rule
    // stay honest only if both are pinned to the same table.
    let rows = try loadClipKindFixture()
    for row in rows {
        XCTAssertEqual(chooseKind(offeredTypes: row.types), row.expect, row.name)
    }
}

func testAScreenshotIsConvertedToPNG() throws {
    let tiff = try XCTUnwrap(makeOnePixelTIFF())
    let board = FakePasteboard(types: [.tiff], data: [.tiff: tiff])
    let read = try XCTUnwrap(SystemPasteboardReader(board).read())
    XCTAssertEqual(read.kind, .image)
    XCTAssertEqual(read.data.prefix(8), Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]),
                   "the body on the wire must be PNG, not the TIFF the pasteboard held")
}

func testTextWinsOverAnImage() throws {
    let board = FakePasteboard(types: [.string, .png],
                               data: [.string: Data("hi".utf8), .png: Data([0x89])])
    let read = try XCTUnwrap(SystemPasteboardReader(board).read())
    XCTAssertEqual(read.kind, .text)
    XCTAssertEqual(read.data, Data("hi".utf8))
}
```

`FakePasteboard` will need adding — `NSPasteboard` is not injectable as-is, so introduce a narrow protocol over the three calls actually used (`types`, `data(forType:)`, `clearContents`/`setData`) and have `SystemPasteboard` wrap the real one. That indirection is what makes the TIFF→PNG path testable without a live pasteboard.

- [ ] **Step 2: Run it and watch it fail**

Run: `rtk proxy swift test --filter PasteboardTests`
Expected: FAIL — `chooseKind` and `read()` do not exist.

- [ ] **Step 3: Implement**

`chooseKind(offeredTypes:)` mirrors `choose_kind` branch for branch. `read()` returns the string as UTF-8 `Data` for text; for images it takes `.png` directly when offered, otherwise converts `.tiff` through `NSBitmapImageRep`, and returns `nil` if the conversion fails — logged by the caller, never substituted.

**Hash what you read.** After `write(kind:data:)` the Mac may re-read; unlike the PC, `NSPasteboard` returns the bytes it was given, so no read-back dance is needed here. Note that in a comment so nobody adds one symmetrically.

- [ ] **Step 4: Run it and watch it pass**

Run: `rm -rf .build && rtk proxy swift build && rtk proxy swift test`
Expected: PASS. `readText()`/`writeText()` disappear; the compiler lists every call site.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/Pasteboard.swift Tests/clipwireTests/PasteboardTests.swift
git commit -m "Read the Mac pasteboard through one function, converting TIFF to PNG"
```

---

### Task 9: `_last_seen` and the echo guard compare `(kind, hash)`

**Files:**
- Modify: `agent/clipwire-agent.py`, `Sources/clipwire/EchoGuard.swift`
- Test: `agent/tests/test_watcher.py`, `Tests/clipwireTests/EchoGuardTests.swift`

**Interfaces:**
- Consumes: `read()` from Tasks 7-8.
- Produces: `Agent._last_seen` holding `(kind, hash)` or `None`.

**One rule for both kinds, not text by value and images by hash.** Two comparison branches is the mirrored drift this project has been bitten by twice; one rule costs nothing and keeps 4 MiB of pixels out of memory.

- [ ] **Step 1: Write the failing test**

```python
def test_last_seen_holds_a_kind_and_a_hash_for_text_too(self):
    """Not text-by-value and images-by-hash: one rule. A second comparison
    branch is how the two sides drift."""
    agent_obj = self._agent_with_clipboard([(agent.KIND_TEXT, b"hello")])
    agent_obj._local_change()
    self.assertEqual(agent_obj._last_seen,
                     (agent.KIND_TEXT, agent.sha256_hex(b"hello")))

def test_the_same_bytes_under_a_different_kind_are_a_change(self):
    agent_obj = self._agent_with_clipboard([(agent.KIND_TEXT, b"x"),
                                            (agent.KIND_IMAGE, b"x")])
    agent_obj._local_change()
    sent_before = len(self.sent)
    agent_obj._local_change()
    self.assertGreater(len(self.sent), sent_before,
                       "kind is part of identity, not decoration")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k test_last_seen_holds_a_kind`
Expected: FAIL — `_last_seen` holds raw text.

- [ ] **Step 3: Implement**

Replace every `_last_seen` assignment and comparison with the pair. On the Swift side `EchoGuard` already hashes; extend `noteWrittenLocally`/`shouldSend` to take the kind alongside the payload and compare both. Keep the existing suppression semantics exactly: consumed by the first observed change **whatever it is**, never match-only — that asymmetry was a real v1 defect and is not to be reintroduced while touching this code.

- [ ] **Step 4: Run both suites**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rtk proxy swift test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py Sources/clipwire/EchoGuard.swift agent/tests Tests/clipwireTests
git commit -m "Make kind part of what the echo guard considers identity"
```

---

### Task 10: The PC hashes what it reads back, not what it wrote

**Files:**
- Modify: `agent/clipwire-agent.py` — `_write_clip` and the image apply path
- Test: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `write(kind, data)` and `read()` from Task 7, `_last_seen` from Task 9.
- Produces: nothing later tasks depend on.

**This is the task most likely to be got wrong, and the one acceptance will fail on if it is.** Measured on the live machine: a 105,700-byte PNG written to the PC clipboard reads back a few seconds later as a *different* 180,287-byte PNG, because GPaste takes over the selection and re-encodes it. The result is stable afterwards. Text is unaffected.

Hashing what was written produces two failures: an extra 4 MiB round trip on every screenshot, and — worse — a false `clipboard changed while apart` on **every** reconnect, letting a stale image win reconciliation against fresher content.

**Do not implement this with a sleep.** The takeover was measured between one and four seconds on one machine on one day; a fixed wait is a race dressed as a constant. Instead the agent consumes **the first subsequent image-kind observation as its own re-offer**: after writing an image it records that a re-offer is expected, and the next observation of an image whose hash differs updates `_last_seen` and the store without sending. This mirrors the echo guard's existing rule.

- [ ] **Step 1: Write the failing test**

```python
def test_a_gpaste_re_encode_after_our_write_is_not_sent_back(self):
    """GPaste re-encodes an image when it takes over the selection, so the
    bytes we wrote and the bytes the clipboard then offers differ -- always,
    and by a lot: measured 105,700 in and 180,287 out. Hashing what we wrote
    makes the PC send the re-encoded copy straight back."""
    written = b"\x89PNG-original"
    reoffered = b"\x89PNG-reencoded-by-gpaste-and-larger"
    clip = ScriptedClipboard(reads=[(agent.KIND_IMAGE, reoffered)])
    agent_obj = self._agent(clipboard=clip)
    agent_obj._write_clip(agent.encode_image_payload(1000.0, written),
                          kind=agent.KIND_IMAGE)
    self.sent.clear()
    agent_obj._local_change()          # the Update GPaste's takeover raises
    self.assertEqual(self.sent, [], "the re-offer is our own write, not a new clip")
    self.assertEqual(agent_obj._last_seen,
                     (agent.KIND_IMAGE, agent.sha256_hex(reoffered)),
                     "and the hash we keep must be the one the clipboard reports")

def test_the_stored_hash_is_the_re_offered_one(self):
    """Otherwise every reconnect sees a mismatch and stamps ts=now, letting a
    stale image beat fresher content on the peer -- a false 'clipboard changed
    while apart' on every single Mac wake."""
    written = b"\x89PNG-original"
    reoffered = b"\x89PNG-reencoded-by-gpaste-and-larger"
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "clip-state.json")
        clip = ScriptedClipboard(reads=[(agent.KIND_IMAGE, reoffered)])
        agent_obj = self._agent(clipboard=clip, clip_state_path=path)
        agent_obj._write_clip(agent.encode_image_payload(1000.0, written),
                              kind=agent.KIND_IMAGE)
        agent_obj._local_change()
        stored = agent.load_clip_state(path=path)
    self.assertEqual(stored[0], agent.sha256_hex(reoffered))
    self.assertEqual(stored[2], agent.KIND_IMAGE)
    self.assertNotEqual(stored[0], agent.sha256_hex(written),
                        "storing the written hash is what breaks every reconnect")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `python3 -m unittest discover -s agent/tests -k test_a_gpaste_re_encode`
Expected: FAIL — the re-offer is treated as a fresh local change and sent.

- [ ] **Step 3: Implement**

After a successful image write, set `self._expect_reoffer = True`. In `_local_change`, when an image observation arrives and `_expect_reoffer` is set, clear the flag, update `_last_seen` and the store from the **read-back** hash, and return without sending. Clear the flag on any text observation too — a text copy means the user moved on and the re-offer will never come.

Every hash written to `_last_seen`, the echo guard, the store or a clip-state frame comes from the bytes `read()` returned, never from the bytes handed to `write()`. Apply the size limit to the read-back body as well: re-encoding inflates, and an image comfortably under 4 MiB going in can exceed it coming out.

- [ ] **Step 4: Run it and watch it pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_lifecycle.py
git commit -m "Hash the image the clipboard offers back, not the one we wrote"
```

---

### Task 11: The send branch verifies before it sends

**Files:**
- Modify: `agent/clipwire-agent.py`, `Sources/clipwire/main.swift`
- Test: `agent/tests/test_watcher.py`, `Tests/clipwireTests/HandleFrameTests.swift`

**Interfaces:**
- Consumes: `read()` from Tasks 7-8, the kind-bearing `ClipState` from Task 6.
- Produces: nothing later tasks depend on.

When reconciliation resolves to send, read the content of the kind recorded in `mine`, hash what was read, and compare it against `mine.sha256`. On a mismatch send nothing and log — the clipboard changed between the announcement and the send, and the watcher will carry the new content on its own. Without this rule the branch sends whatever it finds under the announced timestamp: the wrong kind, at a stale age, which is a clobber the receiver cannot detect.

- [ ] **Step 1: Write the failing test**

```python
def test_the_send_branch_stays_silent_when_the_clipboard_moved_on(self):
    """mine says text with hash A; by the time we send, the clipboard holds
    an image. Sending it under A's timestamp is a clobber the peer cannot
    detect -- the watcher will carry the real change a moment later."""
    clip = ScriptedClipboard(reads=[(agent.KIND_IMAGE, b"\x89PNG-something-else")])
    agent_obj = self._agent(clipboard=clip)
    mine = ("aa" * 32, 5000.0, agent.KIND_TEXT)      # announced: text, hash A
    peer = ("bb" * 32, 1000.0, agent.KIND_TEXT)      # older, so we resolve SEND_MINE
    agent_obj._resolve_clip_state(peer, mine=mine)
    self.assertEqual(self.sent, [],
                     "the clipboard no longer holds what we announced")
    self.assertIn("clipboard changed before the send", "\n".join(self.logged))

def test_the_send_branch_sends_when_the_clipboard_still_matches(self):
    """The positive half: without it the test above passes against a branch
    that never sends anything at all."""
    body = b"still here"
    clip = ScriptedClipboard(reads=[(agent.KIND_TEXT, body)])
    agent_obj = self._agent(clipboard=clip)
    mine = (agent.sha256_hex(body), 5000.0, agent.KIND_TEXT)
    peer = ("bb" * 32, 1000.0, agent.KIND_TEXT)
    agent_obj._resolve_clip_state(peer, mine=mine)
    self.assertEqual(len(self.sent), 1)
    self.assertEqual(self.sent[0][0], agent.TYPE_CLIP)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k test_the_send_branch`
Expected: the first FAILs — the branch sends the image under the text state's timestamp. The second passes already; it is there so the fix cannot be "never send".

- [ ] **Step 3: Implement on both sides, symmetrically**

Read by `mine.kind`, hash, compare to `mine.sha256`, send only on a match. Both log lines use identical wording, the way the frame-cap and skew lines already do.

- [ ] **Step 4: Run both suites**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rtk proxy swift test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py Sources/clipwire/main.swift agent/tests Tests/clipwireTests
git commit -m "Verify the clipboard still holds what we announced before sending it"
```

---

### Task 12: Images end to end on the PC

**Files:**
- Modify: `agent/clipwire-agent.py` — `on_frame`, `_write_clip`, `_local_change`, `clipboard_became_ready`, `resolve_current_clip_state`
- Test: `agent/tests/test_mainloop.py`, `agent/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: everything from Tasks 4-11.
- Produces: nothing later tasks depend on.

Wire `TYPE_IMAGE_CLIP` into the dispatch, send images the watcher observes, and make the startup seed report the right kind. Reuse the pending-clip machinery unchanged — an image arriving before the clipboard is ready must queue exactly as text does.

- [ ] **Step 1: Write the failing test**

```python
def test_an_image_frame_is_applied_to_the_clipboard(self):
    png = b"\x89PNG-from-the-peer"
    clip = RecordingClipboard()
    agent_obj = self._agent(clipboard=clip)
    agent_obj.on_frame(agent.TYPE_IMAGE_CLIP, agent.encode_image_payload(1000.0, png))
    self.assertEqual(clip.written, [(agent.KIND_IMAGE, png)])

def test_an_image_observed_locally_is_sent_as_type_3(self):
    png = b"\x89PNG-copied-here"
    agent_obj = self._agent(clipboard=ScriptedClipboard(reads=[(agent.KIND_IMAGE, png)]))
    agent_obj._local_change()
    self.assertEqual(len(self.sent), 1)
    frame_type, payload = self.sent[0]
    self.assertEqual(frame_type, agent.TYPE_IMAGE_CLIP)
    self.assertEqual(agent.decode_image_payload(payload)[1], png)

def test_an_oversized_image_is_skipped_with_its_size_in_the_log(self):
    huge = b"\x89" * (agent.MAX_IMAGE_BYTES + 1)
    agent_obj = self._agent(clipboard=ScriptedClipboard(reads=[(agent.KIND_IMAGE, huge)]))
    agent_obj._local_change()
    self.assertEqual(self.sent, [])
    self.assertIn(str(len(huge)), "\n".join(self.logged),
                  "a silent skip is how a user concludes the tool is broken")

def test_an_image_at_exactly_the_limit_is_sent(self):
    """The boundary the separated caps exist for: this body plus its 8-byte
    timestamp exceeds the OLD single 4 MiB cap and must still go out."""
    exact = b"\x89" * agent.MAX_IMAGE_BYTES
    agent_obj = self._agent(clipboard=ScriptedClipboard(reads=[(agent.KIND_IMAGE, exact)]))
    agent_obj._local_change()
    self.assertEqual(len(self.sent), 1)

def test_the_startup_seed_reports_image_kind(self):
    png = b"\x89PNG-already-here"
    clip = ScriptedClipboard(reads=[(agent.KIND_IMAGE, png)])
    state = agent.resolve_current_clip_state(clip, None, 1234.0)
    self.assertEqual(state, (agent.sha256_hex(png), 1234.0, agent.KIND_IMAGE))
```

`RecordingClipboard` records `(kind, data)` pairs passed to `write`; add it beside the existing fakes if this file has no equivalent.

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k Image`
Expected: FAIL on all five — `on_frame` has no `TYPE_IMAGE_CLIP` branch and `resolve_current_clip_state` returns a pair.

- [ ] **Step 3: Implement.** `on_frame` gains a `TYPE_IMAGE_CLIP` branch calling the same `_write_clip` with `kind=KIND_IMAGE`; `_local_change` sends `TYPE_IMAGE_CLIP` for image observations, guarded by `MAX_IMAGE_BYTES` and logging the size on a skip; `resolve_current_clip_state` returns the kind from `read()`. The pending-clip machinery is reused unchanged — an image arriving before the clipboard is ready queues exactly as text does.

- [ ] **Step 4: Run them and watch them pass**

Run: `rtk proxy python3 -m unittest discover -s agent/tests`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests
git commit -m "Carry images through the PC agent end to end"
```

---

### Task 13: Images end to end on the Mac

**Files:**
- Modify: `Sources/clipwire/main.swift`
- Test: `Tests/clipwireTests/HandleFrameTests.swift`

**Interfaces:**
- Consumes: everything from Tasks 4-11.
- Produces: nothing later tasks depend on.

The mirror of Task 12: handle `.imageClip`, send images the watcher observes, and report the kind from the startup seed. `PasteboardWatcher` keeps polling `changeCount` — that stays an integer read, and only a change triggers the (now possibly large) body read.

- [ ] **Step 1: Write the failing tests**

```swift
func testAnImageFrameIsAppliedToThePasteboard() throws {
    let png = Data("\u{89}PNG-from-the-peer".utf8)
    let spy = RecordingPasteboard()
    handleFrame(Frame(type: .imageClip, payload: try ImagePayload.encode(ts: 1000, png: png)),
                send: { _ in }, noteWrittenLocally: { _, _ in },
                pasteboard: spy, status: status, log: log,
                clipStateStore: store, clipStateAnnouncement: announcement, now: 2000)
    XCTAssertEqual(spy.written.map(\.kind), [.image])
    XCTAssertEqual(spy.written.first?.data, png)
}

func testAnOversizedImageIsSkippedWithItsSizeLogged() {
    let huge = Data(repeating: 0x89, count: FrameConstants.maxImageBytes + 1)
    let sent = sendImageObserved(huge)
    XCTAssertTrue(sent.isEmpty)
    XCTAssertTrue(log.lines.contains { $0.contains("\(huge.count)") },
                  "a silent skip is how a user concludes the tool is broken")
}

func testAnImageAtExactlyTheLimitIsSent() {
    // Body + its 8-byte timestamp exceeds the OLD single 4 MiB cap. This is
    // the boundary the three separated constants exist for.
    let exact = Data(repeating: 0x89, count: FrameConstants.maxImageBytes)
    XCTAssertEqual(sendImageObserved(exact).count, 1)
}

func testAFailedPNGConversionIsLoggedRatherThanDropped() {
    // try? swallowing a decode was a real v2 defect; the same shape applies
    // to a TIFF that NSBitmapImageRep cannot read.
    let board = FakePasteboard(types: [.tiff], data: [.tiff: Data("not a tiff".utf8)])
    _ = SystemPasteboardReader(board).read()
    XCTAssertTrue(log.lines.contains { $0.contains("could not convert") })
}
```

`RecordingPasteboard` records `(kind, data)` pairs; `sendImageObserved` is a one-line helper around whatever this file already uses to drive a local observation.

- [ ] **Step 2: Run them and watch them fail**

Run: `rtk proxy swift test --filter HandleFrameTests`
Expected: FAIL — `.imageClip` is unhandled.

- [ ] **Step 3: Implement.** Handle `.imageClip` by writing `(.image, png)`; guard sends with `FrameConstants.maxImageBytes` and log the size on a skip; log a failed PNG conversion rather than dropping it.

- [ ] **Step 4: Run them and watch them pass**

Run: `rm -rf .build && rtk proxy swift build && rtk proxy swift test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/main.swift Tests/clipwireTests/HandleFrameTests.swift
git commit -m "Carry images through the Mac agent end to end"
```

---

### Task 14: The log says which kinds, and the README says what to expect

**Files:**
- Modify: `Sources/clipwire/main.swift`, `agent/clipwire-agent.py`, `README.md`
- Test: `Tests/clipwireTests/HandleFrameTests.swift`, `agent/tests/test_watcher.py`

**Interfaces:**
- Consumes: the kind-bearing `ClipState` from Task 6.
- Produces: nothing.

`reconciled with the peer: sendMine` with the two sides holding different kinds is undiagnosable after the fact — "why did a picture overwrite my text" has no answer in the current line.

- [ ] **Step 1: Write the failing test**

```python
def test_the_reconciliation_line_names_both_kinds(self):
    """'why did a picture overwrite my text' must have an answer in the log."""
    agent_obj = self._agent(clipboard=ScriptedClipboard(reads=[(agent.KIND_IMAGE, b"\x89P")]))
    mine = (agent.sha256_hex(b"\x89P"), 5000.0, agent.KIND_IMAGE)
    peer = ("bb" * 32, 1000.0, agent.KIND_TEXT)
    agent_obj._resolve_clip_state(peer, mine=mine)
    line = next(l for l in self.logged if "reconciled with the peer" in l)
    self.assertIn("mine=image", line)
    self.assertIn("peer=text", line)

def test_the_line_says_none_when_a_side_holds_nothing(self):
    agent_obj = self._agent(clipboard=ScriptedClipboard(reads=[None]))
    agent_obj._resolve_clip_state((None, 1000.0, None), mine=(None, 5000.0, None))
    line = next(l for l in self.logged if "reconciled with the peer" in l)
    self.assertIn("mine=none", line)
    self.assertIn("peer=none", line)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `python3 -m unittest discover -s agent/tests -k test_the_reconciliation_line`
Expected: FAIL — the line carries only the decision.

- [ ] **Step 3: Implement on both sides**, appending ` (mine=<kind> peer=<kind>)` with `none` for a null kind, wording byte-identical across the two languages the way the frame-cap and skew lines already are.

- [ ] **Step 4: Run both suites**

Run: `rtk proxy python3 -m unittest discover -s agent/tests` and `rtk proxy swift test`
Expected: PASS.

- [ ] **Step 5: README**

Three additions: images sync up to 4 MiB and text wins when both are present; GPaste re-encodes images so the copy that lands on the PC is not byte-identical to the one copied on the Mac, and that is GPaste, not clipwire; and the existing secrets note extends to screenshots, which GPaste writes to its on-disk history exactly as it writes text — verified, `images-support` is `true` and `~/.local/share/gpaste/images` holds them.

- [ ] **Step 6: Commit**

```bash
git add Sources/clipwire/main.swift agent/clipwire-agent.py README.md Tests/clipwireTests agent/tests
git commit -m "Name both kinds in the reconciliation log, and document image behaviour"
```

---

## Self-Review

**Spec coverage.** Observation path → Tasks 1-3. Caps and protocol version → Task 4. Image codec → Task 5. Kind in clip-state and store → Task 6. Canonical read → Tasks 7-8. `_last_seen` as `(kind, hash)` → Task 9. Read-back hashing → Task 10. Send-branch verification → Task 11. End-to-end wiring → Tasks 12-13. Logging, timeouts and README → Task 14. The v3.1 fallback is a decision point, not work. The acceptance checklist is the spec's and is manual by nature.

**Deliberately unchanged.** The freshness formula and `fixtures/freshness.json` — Task 6 asserts they do not move. The frame envelope. `hello` and its mismatch handling. `_last_seen`'s connect-time seed. The echo guard's consume-on-first-change rule, which Task 9 touches and must not weaken.

**Not covered by any automated test, by nature.** GPaste's re-encode behaviour itself — Task 10 tests the agent's *response* to a re-encode using scripted bytes, which is the right unit boundary, but only the acceptance test proves the real re-encode is absorbed. Same for the TIFF→PNG conversion against a real screenshot, and for every acceptance item.

**Ordering risk worth naming.** Tasks 4-6 change the wire and the on-disk store before Tasks 12-13 make images work. Between them the branch is coherent but the two sides speak protocol 3 with no image path — that is fine for tests and would be broken in production. Do not deploy from a mid-plan commit.
