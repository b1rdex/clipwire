# agent/tests/test_watcher.py
import io
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    ClipStateError,
    GPASTE_BUS_NAME,
    GPasteWatcher,
    MAX_PAYLOAD_BYTES,
    PollingWatcher,
    TIMESTAMP_BYTES,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    decode_clip_payload,
    encode_clip_payload,
    encode_clip_state,
    load_clip_state,
    make_watcher,
    parse_gpaste_line,
    save_clip_state,
    sha256_hex,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to patch make_watcher the same way
# test_mainloop.py patches log(), and to locate the source file for the
# module-ordering check at the bottom of this file.
import clipwire_agent

AGENT = pathlib.Path(__file__).resolve().parents[1] / "clipwire-agent.py"
JOIN_TIMEOUT = 2  # generous relative to the millisecond-scale intervals below


class TestGPasteSignalParsing(unittest.TestCase):
    def test_accepts_the_real_captured_signal(self):
        """This is a verbatim line captured from the target machine.

        GPaste 45.3 reports target 'ALL' and a uint64 index — not 'CLIPBOARD'
        and not uint32. Filtering on 'CLIPBOARD' rejects every real signal.
        """
        line = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                "('REPLACE', 'ALL', uint64 0)")
        self.assertTrue(parse_gpaste_line(line))

    def test_accepts_any_target_including_ones_not_seen_yet(self):
        """Targets are not filtered: the content comparison is the real gate.

        GPaste's primary-to-history setting is off on the target machine, so
        primary selections emit nothing today — but that is a user-flippable
        setting, and a watcher that depends on it would break silently when it
        is flipped. Accepting every Update and letting the content comparison
        decide is immune to that.
        """
        for target in ("'ALL'", "'CLIPBOARD'", "'PRIMARY'"):
            line = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                    "('REPLACE', %s, uint64 0)" % target)
            self.assertTrue(parse_gpaste_line(line), target)

    def test_ignores_unrelated_signals(self):
        self.assertFalse(parse_gpaste_line(
            "/org/gnome/GPaste: org.gnome.GPaste2.ShowHistory ()"))
        self.assertFalse(parse_gpaste_line(""))
        self.assertFalse(parse_gpaste_line("Monitoring signals..."))


class TestGPasteWatcherAvailability(unittest.TestCase):
    """available() gates whether the agent ever leaves the polling fallback.
    Every case here patches subprocess.run, so no real gdbus, no real
    session bus, and therefore no dependency on what happens to be
    installed on the machine running the suite."""

    def test_true_when_introspection_succeeds(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with mock.patch("subprocess.run", return_value=completed):
            self.assertTrue(GPasteWatcher().available())

    def test_false_when_introspection_fails(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1)
        with mock.patch("subprocess.run", return_value=completed):
            self.assertFalse(GPasteWatcher().available())

    def test_false_when_gdbus_binary_is_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertFalse(GPasteWatcher().available())

    def test_false_when_the_probe_times_out(self):
        error = subprocess.TimeoutExpired(cmd="gdbus", timeout=3)
        with mock.patch("subprocess.run", side_effect=error):
            self.assertFalse(GPasteWatcher().available())

    def test_false_when_gdbus_is_not_executable(self):
        """A gdbus present but not executable (wrong permissions, an
        AppArmor denial) raises PermissionError at subprocess.run -- an
        OSError, but not a FileNotFoundError. Mirrors the same class of
        guard already fixed for wl-paste/wl-copy in test_clipboard.py;
        uncaught here, this crashes selftest() instead of just reporting
        GPaste unavailable."""
        with mock.patch("subprocess.run", side_effect=PermissionError("denied")):
            self.assertFalse(GPasteWatcher().available())

    def test_probes_the_bus_name_not_the_interface_name(self):
        """org.gnome.GPaste2 is the INTERFACE name, not the bus name.
        Probing it as --dest finds no owner on the target machine, which
        would make available() always False and silently pin the watcher on
        the polling fallback forever."""
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            GPasteWatcher().available()
        self.assertEqual(GPASTE_BUS_NAME, "org.gnome.GPaste")
        self.assertIn(GPASTE_BUS_NAME, captured["cmd"])
        self.assertNotIn("org.gnome.GPaste2", captured["cmd"])


class FakeGPasteProcess:
    """Stands in for the subprocess.Popen object GPasteWatcher.start()
    creates. Backed by a real pipe, so the reader thread genuinely blocks
    waiting for a line the way it would against a real gdbus monitor
    process; terminate() closes the write end, which is what lets the
    thread's `for line in stdout` end through EOF -- exactly as it would
    once a killed real process's pipe closes."""

    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r")
        self._writer = os.fdopen(write_fd, "w")
        self.terminated = False

    def emit(self, line):
        self._writer.write(line + "\n")
        self._writer.flush()

    def terminate(self):
        self.terminated = True
        if not self._writer.closed:
            self._writer.close()

    def close(self):
        if not self.stdout.closed:
            self.stdout.close()
        if not self._writer.closed:
            self._writer.close()


class TestGPasteWatcherLifecycle(unittest.TestCase):
    """No real gdbus is ever spawned here -- subprocess.Popen is patched to
    return a fake process backed by a real pipe, so the pump thread's
    blocking read behaves like it would against a real one."""

    def start_watcher(self, fake_process, on_change):
        patcher = mock.patch("subprocess.Popen", return_value=fake_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(fake_process.close)
        watcher = GPasteWatcher()
        watcher.start(on_change)
        return watcher

    def test_a_matching_line_invokes_on_change(self):
        fake_process = FakeGPasteProcess()
        changes = []
        watcher = self.start_watcher(fake_process, lambda: changes.append(1))

        fake_process.emit(
            "/org/gnome/GPaste: org.gnome.GPaste2.Update "
            "('REPLACE', 'ALL', uint64 0)"
        )
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not changes and time.monotonic() < deadline:
            time.sleep(0.01)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertEqual(changes, [1])

    def test_an_unrelated_line_does_not_invoke_on_change(self):
        fake_process = FakeGPasteProcess()
        changes = []
        watcher = self.start_watcher(fake_process, lambda: changes.append(1))

        fake_process.emit("/org/gnome/GPaste: org.gnome.GPaste2.ShowHistory ()")
        fake_process.emit(
            "/org/gnome/GPaste: org.gnome.GPaste2.Update "
            "('REPLACE', 'ALL', uint64 0)"
        )
        deadline = time.monotonic() + JOIN_TIMEOUT
        while len(changes) < 1 and time.monotonic() < deadline:
            time.sleep(0.01)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertEqual(changes, [1], "only the Update line should have fired")

    def test_stop_terminates_the_subprocess_and_joins_the_reader_thread(self):
        fake_process = FakeGPasteProcess()
        watcher = self.start_watcher(fake_process, lambda: None)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertTrue(fake_process.terminated)
        self.assertFalse(
            watcher._thread.is_alive(),
            "a daemon thread that keeps reading a dead pipe is a leak",
        )


class ScriptedReadClipboard:
    """read() replays a fixed script, then repeats its last value forever --
    so a poll tick that lands after the test stops watching cannot raise
    IndexError. `last` records the most recent value returned, so a test
    callback can observe what the watcher just saw without threading the
    value through on_change() itself (the real interface takes none)."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self.last = None

    def read(self):
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        self.last = self._script[index]
        return self.last


class TestPollingWatcher(unittest.TestCase):
    def test_available_is_always_true(self):
        self.assertTrue(PollingWatcher(clipboard=None, interval_seconds=1).available())

    def test_fires_only_on_an_actual_change(self):
        clipboard = ScriptedReadClipboard([b"a", b"a", b"a", b"b", b"b", b"c"])
        changes = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.01)
        watcher.start(lambda: changes.append(clipboard.last))

        deadline = time.monotonic() + JOIN_TIMEOUT
        while len(changes) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(changes, [b"b", b"c"])

    def test_stop_ends_the_poll_loop_promptly(self):
        clipboard = ScriptedReadClipboard([b"x"] * 10000)
        watcher = PollingWatcher(clipboard, interval_seconds=0.02)
        watcher.start(lambda: None)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertFalse(
            watcher._thread.is_alive(), "stop() must end the poll loop promptly"
        )


class TestMakeWatcher(unittest.TestCase):
    def test_uses_gpaste_when_available(self):
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=object())
        self.assertIsInstance(watcher, GPasteWatcher)

    def test_falls_back_to_polling_when_gpaste_unavailable(self):
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)
        self.assertIsInstance(watcher, PollingWatcher)
        self.assertEqual(watcher.interval, 2.5)


class QueueClipboard:
    """A clipboard double whose read() replays a queue of scripted values --
    lets a test dictate exactly what Agent._local_change observes on each
    call, independent of any subprocess or timing. write() records what the
    agent wrote locally."""

    def __init__(self, ready=True):
        self._ready = ready
        self._queue = []
        self.written = []

    def queue_read(self, value):
        self._queue.append(value)

    def ready(self):
        return self._ready

    def read(self):
        return self._queue.pop(0) if self._queue else None

    def write(self, data):
        self.written.append(data)


class SpyWatcher:
    """A watcher double for tests that only care whether Agent wired
    start()/stop() correctly -- it never spawns a thread or a subprocess."""

    def __init__(self):
        self.started_with = None
        self.start_count = 0
        self.stopped = False

    def start(self, on_change):
        self.started_with = on_change
        self.start_count += 1

    def stop(self):
        self.stopped = True


class TestWatcherLifecycleWiring(unittest.TestCase):
    """clipboard_became_ready()/clipboard_lost() must create and start a
    watcher exactly once per session, and stop it the moment the session
    goes away -- a gdbus monitor against a dead session is pointless.
    make_watcher is patched throughout so these tests never touch a real
    subprocess or thread."""

    def setUp(self):
        # clipboard_became_ready()'s new announce step persists through
        # save_clip_state/load_clip_state, which touch the real production
        # path when clip_state_path is None -- see TestEchoBookkeeping.setUp.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready),
            clip_state_path=self.clip_state_path,
        )

    def test_watcher_is_created_and_started_when_the_clipboard_becomes_ready(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy):
            agent.clipboard_became_ready()
        self.assertIs(agent._watcher, spy)
        # Bound methods are recreated on each attribute access, so `is` would
        # fail even when correctly wired; == compares __self__ and __func__.
        self.assertEqual(spy.started_with, agent._local_change)

    def test_watcher_is_stopped_and_cleared_when_the_clipboard_is_lost(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy):
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertTrue(spy.stopped)
        self.assertIsNone(agent._watcher)

    def test_watcher_is_not_recreated_while_already_running(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy) as factory:
            agent.clipboard_became_ready()
            agent.clipboard_became_ready()
        self.assertEqual(factory.call_count, 1)
        # factory.call_count alone would not catch a bug that re-called
        # start() on the existing watcher instead of skipping it entirely.
        self.assertEqual(spy.start_count, 1)

    def test_losing_a_clipboard_that_never_became_ready_does_not_raise(self):
        agent = self.build(ready=False)
        agent.clipboard_lost()  # must be a no-op, not an AttributeError
        self.assertIsNone(agent._watcher)


class TestEchoBookkeeping(unittest.TestCase):
    """_write_clip is the single funnel for local writes; _local_change is
    the single gate deciding whether an observed change is our own echo.
    Task 8's tests only exercise the immediate write path -- several of
    these cover the pending-clip path too, which is easy to forget."""

    def setUp(self):
        # _write_clip, _local_change and clipboard_became_ready's new
        # announce step all persist through save_clip_state/load_clip_state,
        # which default to the real production path
        # (~/.local/state/clipwire/clip-state.json) when clip_state_path is
        # None. Every Agent built in this class gets its own temp path so no
        # test in this file ever touches that real location.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready),
            clip_state_path=self.clip_state_path,
        )

    def become_ready_without_a_real_watcher(self, agent):
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

    def test_write_clip_writes_and_arms_the_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        self.assertEqual(agent.clipboard.written, [b"hello"])
        self.assertEqual(agent._last_written, b"hello")

    def test_immediate_clip_delivery_arms_the_suppression(self):
        agent = self.build(ready=True)
        self.become_ready_without_a_real_watcher(agent)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"now"))
        self.assertEqual(agent.clipboard.written, [b"now"])
        self.assertEqual(agent._last_written, b"now")

    def test_pending_clip_delivery_arms_the_same_suppression(self):
        """If clipboard_became_ready() wrote pending_clip directly instead of
        through _write_clip, delivery would leave no suppression armed, and
        the very next poll tick would bounce our own delivered clip back to
        the peer as if the user had copied it."""
        agent = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"queued while pending"))
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)
        self.assertEqual(agent.clipboard.written, [b"queued while pending"])
        self.assertEqual(agent._last_written, b"queued while pending")

    def test_matching_echo_is_suppressed_and_consumed(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(b"hello")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])
        self.assertIsNone(
            agent._last_written, "the suppression must be consumed even on a match"
        )

    def test_mismatched_echo_is_still_consumed(self):
        """Consume-on-mismatch, pinned directly. This was a real defect
        once (see EchoGuard's doc comment and its Swift mirror,
        shouldSend): a suppression that only clears on a MATCH stays
        armed forever once the watcher's one poll interval misses the
        write's own change event and observes something else first --
        silently swallowing the very next deliberate re-copy of the
        original text. The suppression must be consumed by the first
        observed change, whatever it is, not only by a matching one."""
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(b"something else")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1234.5):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1234.5, b"something else"))])
        self.assertIsNone(
            agent._last_written,
            "the suppression must be consumed on a mismatch too, not only on a match",
        )

    def test_fresh_agent_sends_a_genuine_local_change(self):
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1234.5):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1234.5, b"typed by the user"))])

    def test_deliberate_recopy_still_syncs_after_a_missed_echo(self):
        """Mirrors EchoGuardTests.testDeliberateRecopyStillSyncsAfterAMissedEcho
        on the Swift side, so the two implementations cannot drift on it."""
        agent = self.build(ready=True)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        our_write = b"our own write"
        something_else = b"a different clip the user made"

        agent._write_clip(encode_clip_payload(1.0, our_write))
        agent.clipboard.queue_read(something_else)
        with mock.patch("time.time", return_value=10.0):
            agent._local_change()  # the poll missed our echo, saw the user's clip instead

        agent.clipboard.queue_read(our_write)
        with mock.patch("time.time", return_value=20.0):
            agent._local_change()  # a deliberate re-copy of our own text, later

        self.assertEqual(
            sent,
            [(TYPE_CLIP, encode_clip_payload(10.0, something_else)),
             (TYPE_CLIP, encode_clip_payload(20.0, our_write))],
            "a later deliberate re-copy of our own text must still sync",
        )

    def test_deliberate_recopy_still_syncs_after_a_pending_delivery_misses_its_echo(self):
        """Same scenario, but the write that gets echoed-past is the
        pending-clip delivery, not an immediate one -- the path Task 8 never
        exercised."""
        agent = self.build(ready=False)
        delivered = b"queued while pending"
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, delivered))
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)

        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        something_else = b"a different clip the user made"

        agent.clipboard.queue_read(something_else)
        with mock.patch("time.time", return_value=10.0):
            agent._local_change()

        agent.clipboard.queue_read(delivered)
        with mock.patch("time.time", return_value=20.0):
            agent._local_change()

        self.assertEqual(
            sent,
            [(TYPE_CLIP, encode_clip_payload(10.0, something_else)),
             (TYPE_CLIP, encode_clip_payload(20.0, delivered))],
        )

    def test_empty_read_does_not_consume_an_armed_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(None)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])
        self.assertEqual(
            agent._last_written, b"hello",
            "an empty read must not consume an armed suppression",
        )

    def test_oversized_local_change_is_skipped_not_sent(self):
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"x" * (MAX_PAYLOAD_BYTES + 1))
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])

    def test_text_at_exactly_the_cap_is_skipped_because_the_encoded_frame_would_exceed_it(self):
        """Since this task, _local_change wraps the observed text in
        encode_clip_payload before it reaches the wire, adding an 8-byte
        timestamp prefix -- so text at exactly MAX_PAYLOAD_BYTES would
        encode to a frame 8 bytes OVER the cap. The pre-existing guard
        (len(text) > MAX_PAYLOAD_BYTES) cannot see this boundary: it only
        rejects text already over the cap, one byte too late for content
        exactly AT it. Mirrors
        PasteboardTests.testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt
        on the Swift side."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"x" * MAX_PAYLOAD_BYTES)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [], "text at exactly the cap would encode to a frame 8 bytes over it")

    def test_text_leaving_exact_room_for_the_timestamp_prefix_still_sends(self):
        """The other half of the boundary: content that leaves exact room
        for the 8-byte timestamp prefix must still be sent -- an
        over-trimmed fix would silently refuse to sync content the wire
        format actually supports."""
        agent = self.build(ready=True)
        text = b"x" * (MAX_PAYLOAD_BYTES - TIMESTAMP_BYTES)
        agent.clipboard.queue_read(text)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, text))])

    def test_local_change_stamps_the_send_with_the_moment_of_observation(self):
        """The timestamp half of the new send path: the frame must carry
        the moment _local_change observed the change, not some later
        moment. Mirrors
        AgentWiringTests.testAGenuineLocalChangeStoresItsHashAndObservationTimestamp
        on the Swift side, but pins it against the SENT FRAME here rather
        than the store (a separate test below pins the store)."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        before = time.time()
        agent._local_change()
        after = time.time()
        self.assertEqual(len(sent), 1)
        ts, text = decode_clip_payload(sent[0][1])
        self.assertEqual(text, b"typed by the user")
        self.assertTrue(before <= ts <= after, "expected %r to fall within [%r, %r]" % (ts, before, after))

    def test_local_change_persists_the_observed_hash_and_timestamp(self):
        """Mirrors AgentWiringTests.testAGenuineLocalChangeStoresItsHashAndObservationTimestamp:
        a genuine local change must hash the new content and persist it
        alongside the moment it was OBSERVED, so resolve_startup_state has
        something accurate to compare against on the next connection."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        agent.send = lambda t, p: None
        before = time.time()
        agent._local_change()
        after = time.time()

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored[0], sha256_hex(b"typed by the user"))
        self.assertTrue(before <= stored[1] <= after,
                        "expected %r to fall within [%r, %r]" % (stored[1], before, after))

    def test_local_change_stores_the_literal_known_hash_for_a_pinned_vector(self):
        """The outgoing-path twin of
        TestWriteClipDecodesTheWirePayload.test_stores_the_literal_known_hash_for_a_pinned_vector --
        the same call-site-hashing bug could hide on either side of the
        wire independently."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"hi")
        agent.send = lambda t, p: None
        agent._local_change()

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(
            stored[0],
            "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4",
        )

    def test_local_change_survives_a_real_disk_failure_when_persisting_clip_state(self):
        """A local disk failure must not prevent the genuine local change
        from being sent to the peer -- the peer did nothing wrong. Forces a
        real save failure rather than mocking it, mirroring
        TestWriteClipDecodesTheWirePayload's own version of this test."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable_path = os.path.join(blocker, "clip-state.json")
        with self.assertRaises(OSError):
            save_clip_state("aa", 1, path=unsaveable_path)

        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=True),
                      clip_state_path=unsaveable_path)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent._local_change()

        self.assertEqual(len(sent), 1, "a local disk failure must not prevent the send")
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"typed by the user")

    # MARK: - _last_seen: persistent memory of what was last synced
    #
    # Final-review Finding 1: _local_change() used to decide "the user
    # changed the clipboard" from *a signal fired* plus *content differs
    # from the one-shot echo value* (_last_written/expected). With no
    # memory of what was last synced, any signal that is not a real change
    # -- a non-change GPaste Update (e.g. a history deletion), or a
    # transient read() glitch in polling mode -- re-sends the current
    # content even though nothing actually changed, and can race a real
    # incoming write and clobber it. _last_seen is set in _write_clip
    # (content arriving from the peer) and after a successful send
    # (content leaving to the peer), and _local_change returns early
    # whenever the freshly read text already matches it.

    def test_non_change_signal_after_receiving_a_clip_produces_no_send(self):
        """Failure A from the final review, receive side: "A" arrives from
        the Mac (_write_clip). The watcher's own poll observes the echo of
        that write (consuming _last_written) -- and then a LATER,
        unrelated, non-change signal fires with the clipboard still
        reading "A". _last_written is already spent, so only _last_seen
        can recognize this as not a genuine change."""
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"A"))  # "A" arrived from the Mac
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()  # the echo of our own write: consumed, no send
        self.assertEqual(sent, [])

        agent.clipboard.queue_read(b"A")  # a later, non-change signal
        agent._local_change()
        self.assertEqual(
            sent, [],
            "a non-change signal after the echo is already consumed must not "
            "resend content already known to be in sync -- Failure A from the "
            "final review",
        )

    def test_non_change_signal_after_a_send_produces_no_send(self):
        """Failure A from the final review, send side: the PC itself sent
        "A" earlier (a genuine local change). A later non-change signal
        fires with the clipboard still reading "A" and nothing armed in
        _last_written (never armed by an outgoing send in the first
        place) -- only _last_seen, set after that earlier successful
        send, can recognize this as not a genuine change."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()  # a genuine first sync of "A"
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))])

        agent.clipboard.queue_read(b"A")  # unchanged content, spurious signal
        agent._local_change()
        self.assertEqual(
            sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))],
            "a non-change signal must not resend content already known to be in sync",
        )

    def test_transient_read_glitch_does_not_cause_a_duplicate_send(self):
        """Failure B from the final review: PollingWatcher's own
        previous/current tracking is fooled by a transient read() that
        returns None (e.g. a wl-paste timeout), which makes it treat the
        next successful read of the SAME content as a change and fire
        on_change() -- potentially on more than one tick. Without
        _last_seen, _local_change has no way to recognize that content as
        already synced and resends it every time it fires."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()  # "A" is now the known-synced value
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))])

        # PollingWatcher's read() returned None on a transient timeout, so it
        # treats this tick's previous(A)->current(None) transition as a
        # change and fires on_change() -- but _local_change's OWN read()
        # call recovers immediately and sees "A" again, not None.
        agent.clipboard.queue_read(b"A")
        agent._local_change()
        # The very next tick, the same recovery repeats.
        agent.clipboard.queue_read(b"A")
        agent._local_change()

        self.assertEqual(
            sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))],
            "a transient read() glitch that recovers to the same content must not resend it",
        )

    def test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send(self):
        """Second-round final review, Finding 1: _last_seen starts None, and
        this agent lives exactly one connection (sshd spawns a fresh
        process per SSH connection). Without a seed, ANY reconnect -- a
        Mac sleep/wake or a network blip, not only a PC reboot, since
        those also spawn a brand-new agent while the Wayland session is
        already up -- lets the first spurious signal (GPasteWatcher's pump
        has no content baseline of its own, unlike PollingWatcher) send
        whatever the PC's clipboard already held, and the Mac applies it
        unconditionally: destroying a copy made on the Mac while the
        channel was down. clipboard_became_ready() must seed _last_seen
        from the clipboard before the watcher can observe anything."""
        agent = self.build(ready=True)
        # Two reads happen inside clipboard_became_ready() now: the seed
        # read below, and a second read from the new announce-clip-state
        # step's own resolve_current_clip_state call. QueueClipboard.read()
        # pops one value per call, so both must be queued or the second
        # (the announce's) would see an empty queue and read as None --
        # not a bug in the code under test, just this queue-based double
        # needing to reflect the extra call.
        agent.clipboard.queue_read(b"already on the pc")  # the seed read
        agent.clipboard.queue_read(b"already on the pc")  # the announce step's own read
        self.become_ready_without_a_real_watcher(agent)
        self.assertEqual(agent._last_seen, b"already on the pc")

        agent.clipboard.queue_read(b"already on the pc")  # the spurious signal's read
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(
            sent, [],
            "a spurious signal right after connect must not resend content the PC "
            "already held before anything synced",
        )


class OrderRecordingClipboard:
    """write() snapshots agent._last_written at the exact moment it is
    called, so a test can pin the ORDER of arm-then-write, not merely that
    both happened. A version of _write_clip that armed the suppression
    AFTER writing would still make an occurrence-only assertion
    (clipboard.written == [...] and agent._last_written == ...) pass, since
    both would still be true by the time the test looks -- only checking
    what was armed AT WRITE TIME can tell the two orderings apart. Mirrors
    HandleFrameTests.swift's RecordingPasteboard.onWrite callback."""

    def __init__(self):
        self.agent = None  # set after construction, once the real agent exists
        self.written = []
        self.armed_at_write_time = []

    def ready(self):
        return True

    def read(self):
        return None

    def write(self, data):
        self.written.append(data)
        self.armed_at_write_time.append(self.agent._last_written if self.agent else None)


class TestWriteClipDecodesTheWirePayload(unittest.TestCase):
    """_write_clip's contract changed with this task: its argument is now
    the wire-format [ts][text] payload encode_clip_payload produces, not
    bare text -- so it must decode before touching the clipboard, arm the
    suppression with the TEXT (not the ts-prefixed payload -- Contract 2 in
    HandleFrameTests.swift), and persist the PEER's timestamp, never now."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def test_arms_suppression_strictly_before_writing_to_the_clipboard(self):
        """The ordering contract, pinned as a SEQUENCE: reversed, the
        watcher's next poll would observe the new content before the
        suppression exists and bounce our own applied clip back out."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(1.0, b"hello"))

        self.assertEqual(clipboard.written, [b"hello"])
        self.assertEqual(
            clipboard.armed_at_write_time, [b"hello"],
            "the suppression must already be armed with the decoded text at "
            "the moment write() runs -- arm-after-write would leave this None",
        )

    def test_arms_suppression_with_plain_text_not_the_timestamp_prefixed_payload(self):
        """Distinct from the ordering test above: this pins WHAT is armed.
        _local_change's own clipboard.read() returns plain text, never a
        timestamp prefix -- arming with the raw wire payload instead would
        make the suppression never match a later poll's read, silently
        disabling echo suppression entirely."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        wire_payload = encode_clip_payload(1.0, b"hello")

        agent._write_clip(wire_payload)

        self.assertEqual(agent._last_written, b"hello")
        self.assertNotEqual(agent._last_written, wire_payload)

    def test_stores_the_peers_timestamp_not_now(self):
        """The assertion the persistent store's whole design exists to make
        possible: stamping applied content with `now` instead of the peer's
        ts would make it look freshly copied here and win the next
        reconciliation against the machine it actually came from. 424242.0
        is picked far from wall-clock time specifically so "came from the
        frame" and "came from now" cannot be confused by coincidence."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        peers_ts = 424242.0

        agent._write_clip(encode_clip_payload(peers_ts, b"peer's clip"))

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored, (sha256_hex(b"peer's clip"), peers_ts),
                         "must store the PEER's ts, never now")

    def test_stores_the_literal_known_hash_for_a_pinned_vector(self):
        """Closes a gap a self-referential assertion cannot: comparing the
        stored hash against sha256_hex(b"hi") itself would still pass even
        if _write_clip hashed the wrong bytes (the ts-prefixed wire
        payload, or a str re-decoded/re-encoded differently), as long as it
        did so consistently with sha256_hex's own behavior. This pins the
        LITERAL, independently-verified digest instead -- see
        fixtures/hashes.json, read by both suites."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)

        agent._write_clip(encode_clip_payload(1.0, b"hi"))

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(
            stored,
            ("8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4", 1.0),
        )

    def test_a_malformed_too_short_payload_touches_neither_suppression_nor_the_clipboard(self):
        """Mirrors Sources/clipwire/main.swift's handleFrame .clip case:
        `guard let decoded = try? ClipPayload.decode(...) else { return }`.
        A payload shorter than the 8-byte timestamp prefix cannot decode at
        all; _write_clip must swallow that quietly, exactly as an
        undecodable or empty clip frame already did before this task."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(b"\x00\x01\x02")  # 3 bytes: shorter than TIMESTAMP_BYTES

        self.assertEqual(clipboard.written, [])
        self.assertIsNone(agent._last_written)
        self.assertIsNone(load_clip_state(path=self.clip_state_path),
                          "a payload that never decoded must not be persisted")

    def test_a_decodable_but_empty_text_payload_touches_neither_suppression_nor_the_clipboard(self):
        """Distinct failure mode from the too-short case above: this payload
        DECODES fine (a valid 8-byte ts prefix, no text) but carries empty
        text. Mirrors
        HandleFrameTests.testDecodableButEmptyTextClipTouchesNeitherSuppressionNorThePasteboard."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(5.0, b""))

        self.assertEqual(clipboard.written, [])
        self.assertIsNone(agent._last_written)

    def test_survives_a_real_disk_failure_when_persisting_clip_state(self):
        """Self-review: a local disk failure is not the peer's fault, and
        must not silently disable applying the clip. Forces a REAL save
        failure (a plain file occupying the directory the store needs to
        create), not a mock -- a mock would only prove a try/except exists
        syntactically, not that the clipboard write survives an actual
        failure. Mirrors HandleFrameTests.testAnnounceClipStateStillSendsWhenTheStoreCannotBeSaved."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable_path = os.path.join(blocker, "clip-state.json")
        # Confirm the setup actually forces a failure, or this test proves nothing.
        with self.assertRaises(OSError):
            save_clip_state("aa", 1, path=unsaveable_path)

        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=unsaveable_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(1.0, b"still write this"))

        self.assertEqual(clipboard.written, [b"still write this"],
                         "a local disk failure must not prevent the clipboard write")
        self.assertEqual(agent._last_written, b"still write this",
                         "the suppression must still be armed despite the disk failure")


class RacyClipboard:
    """A clipboard double that simulates a write landing on the main thread
    WHILE a read() is in flight -- deterministically, via a side effect
    inside read() itself, rather than by timing two real threads. This is
    the exact shape of the real race: a wl-paste round trip can take up to
    SUBPROCESS_TIMEOUT=3 seconds, and a new frame can arrive and be
    written locally (on the main thread) at any point during that
    window."""

    def __init__(self, agent, value_read, interleaved_write):
        self._agent = agent
        self._value_read = value_read
        self._interleaved_write = interleaved_write

    def read(self):
        self._agent._write_clip(self._interleaved_write)  # lands mid-flight
        return self._value_read  # what the fork had already captured

    def write(self, data):
        pass

    def ready(self):
        return True


class TestLocalChangeRaceSafety(unittest.TestCase):
    """_write_clip() always runs on the main thread (driven by run()'s
    single-threaded loop); _local_change() always runs on the watcher's
    background thread. clipboard.read() -- a wl-paste round trip -- can
    take up to SUBPROCESS_TIMEOUT=3s, so a new _write_clip() can land on
    the main thread at any point during that window, not just cleanly
    before or after it."""

    def setUp(self):
        # _write_clip persists through save_clip_state, which touches the
        # real production path when clip_state_path is None -- see
        # TestEchoBookkeeping.setUp.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def test_a_write_that_lands_during_the_read_is_not_echoed(self):
        """Sequence pinned here:
        1. Mac sends clip A -> _write_clip(A) arms the suppression for A.
        2. The watcher's read() begins; the underlying fork has already
           captured A, but control has not yet returned to Python.
        3. Before it returns, Mac sends clip B -> _write_clip(B) re-arms
           the suppression for B, on the main thread.
        4. read() finally returns A -- stale by the time _local_change
           gets to compare it against whatever is now armed.

        A version that snapshots _last_written only *after* read() returns
        treats A as a genuine change relative to the now-current expected
        value B, and sends A back to the Mac as if the user had copied it
        -- our own echo. It must send nothing, and must leave B's
        suppression armed so B's own echo (or a later genuine change) is
        still judged correctly."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent._write_clip(encode_clip_payload(1.0, b"A"))
        agent.clipboard = RacyClipboard(
            agent, value_read=b"A", interleaved_write=encode_clip_payload(2.0, b"B"))

        agent._local_change()

        self.assertEqual(
            sent, [], "a write observed mid-read must not be echoed back as genuine"
        )
        self.assertEqual(
            agent._last_written, b"B",
            "the newer write's suppression must stay armed for its own echo",
        )


class TestIncomingClipState(unittest.TestCase):
    """Agent._on_clip_state: resolves an incoming TYPE_CLIP_STATE frame
    against what we hold, per resolve_freshness, and sends only when we
    win. Mirrors HandleFrameTests.swift's "Contract 5" section
    (resolving a peer's clip-state announcement)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, clipboard=None):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(),
            clipboard=clipboard if clipboard is not None else QueueClipboard(ready=True),
            clip_state_path=self.clip_state_path,
        )

    def test_losing_clip_state_with_peer_fresher_produces_no_send(self):
        """resolve_freshness's waitForPeer outcome: the peer is fresher, so
        we wait. Conflating this with doNothing would be harmless here, but
        the point of a resend would be to CLOBBER a fresher peer -- exactly
        the defect this whole design exists to prevent."""
        save_clip_state("aa", 5, path=self.clip_state_path)
        agent = self.build()
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state("bb", 9))  # peer fresher

        self.assertEqual(sent, [], "the peer is fresher -- we wait, we do not resend")

    def test_agreeing_clip_state_with_equal_hashes_produces_no_send(self):
        """resolve_freshness's doNothing outcome via equal hashes: "hashes
        equal" must mean "we agree", not "resend" -- conflating it with
        sendMine would ping-pong the same content back and forth forever."""
        save_clip_state("aa", 5, path=self.clip_state_path)
        agent = self.build()
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state("aa", 999))  # same hash

        self.assertEqual(sent, [], "hashes equal means we agree, not resend")

    def test_winning_clip_state_sends_exactly_one_clip_frame_carrying_our_stored_timestamp(self):
        """resolve_freshness's sendMine outcome: a peer with no clipboard at
        all (also the fix for v1's documented loss of Mac copies made while
        the PC was off). The resulting clip must carry OUR stored ts, not
        now -- resending with now would perpetually refresh its age and let
        it win every future reconciliation regardless of what happens next."""
        save_clip_state("aa", 777, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"current clip text")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))  # peer empty

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], TYPE_CLIP)
        ts, text = decode_clip_payload(sent[0][1])
        self.assertEqual(ts, 777, "must carry OUR stored ts, not now")
        self.assertEqual(text, b"current clip text")

    def test_winning_clip_state_with_content_at_exactly_the_cap_produces_no_send(self):
        """Unlike _local_change's own send path, this branch reads the live
        clipboard independently and, without this bound, winning a
        reconciliation over content at or beyond the cap would build a
        frame that exceeds MAX_PAYLOAD_BYTES once wrapped -- the peer's
        decode_frame rejects that as oversized and drops the whole channel."""
        save_clip_state("aa", 777, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"x" * MAX_PAYLOAD_BYTES)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))

        self.assertEqual(sent, [], "content of exactly the cap would encode to a frame 8 bytes over it")

    def test_winning_clip_state_with_content_leaving_exact_room_for_the_timestamp_prefix_still_sends(self):
        save_clip_state("aa", 777, path=self.clip_state_path)
        text = b"x" * (MAX_PAYLOAD_BYTES - TIMESTAMP_BYTES)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(text)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))

        self.assertEqual(len(sent), 1)
        self.assertEqual(decode_clip_payload(sent[0][1])[1], text)

    def test_winning_clip_state_with_oversized_content_is_logged_with_its_size(self):
        """A user whose large paste wins a reconciliation but can't actually
        be sent has nothing to look at otherwise -- matches the existing
        "skipping a clip of N bytes: over the frame cap" line used for
        _local_change's own cap."""
        save_clip_state("aa", 777, path=self.clip_state_path)
        oversized = MAX_PAYLOAD_BYTES
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"x" * oversized)
        agent = self.build(clipboard=clipboard)
        agent.send = lambda t, p: None

        original_log = clipwire_agent.log
        log_lines = []
        clipwire_agent.log = log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))

        self.assertTrue(
            any("skipping a clip of %d bytes" % oversized in line for line in log_lines),
            "expected the skip to be logged with its size; got: %r" % log_lines,
        )

    def test_clip_state_fallback_when_store_is_empty_still_resolves_from_the_live_clipboard(self):
        """If the store's own load returns nothing (a disk failure on an
        earlier save, never expected in ordinary operation), _on_clip_state
        must still resolve a real state from the live clipboard rather than
        a bare None-hash placeholder. A bare None there would make BOTH
        sides resolve waitForPeer against each other's (correctly
        announced) state and silently lose the clip -- exactly v1's bug,
        reintroduced through the fallback path instead of the main one."""
        # self.clip_state_path is never written to -- load_clip_state() returns None.
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"what we actually hold")  # the fallback resolution's own read
        clipboard.queue_read(b"what we actually hold")  # the sendMine branch's own read
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))  # peer also empty

        self.assertEqual(len(sent), 1,
                         "an empty store must not silently resolve to waitForPeer against a "
                         "peer that also holds nothing to compare against -- that is a silent loss")
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"what we actually hold")

    def test_a_malformed_clip_state_raises_a_clip_state_error_not_a_crash(self):
        """The agent-level twin of test_freshness.py's
        test_decode_rejects_an_oversized_integer_timestamp: _on_clip_state
        must not locally swallow a decode failure. It mirrors _on_hello's
        existing bare `raise FrameError(...)` for a malformed/mismatched
        hello -- so a malformed clip-state closes the connection via
        main()'s `except FrameError`, exactly like a malformed hello does,
        rather than silently continuing (Swift's peer cannot self-close
        and so swallows this; this agent, as the child process sshd spawns,
        can and already does for hello) or crashing with an uncaught
        OverflowError."""
        agent = self.build()
        oversized = b'{"sha256": "aa", "ts": 1' + b"0" * 400 + b"}"

        with self.assertRaises(ClipStateError):
            agent.on_frame(TYPE_CLIP_STATE, oversized)


class TestModuleDefinitionOrder(unittest.TestCase):
    """The file ends with `if __name__ == "__main__": sys.exit(main(...))`.
    main() is only CALLED on that line, so any def/class placed AFTER it in
    the file has not executed yet at that point -- referencing it from
    inside Agent.clipboard_became_ready() would raise NameError the first
    time the clipboard becomes ready for real.

    No behavioural test catches a regression here: agent_under_test.py
    loads the module with exec_module under the name "clipwire_agent", so
    the guard's `if` is False there and the whole file runs regardless of
    definition order; the subprocess tests in test_mainloop.py do run the
    file as __main__, but all of them use
    CLIPWIRE_FAKE_CLIPBOARD=never-ready, so clipboard_became_ready() -- and
    therefore this defect -- is never reached. Pin the ordering directly
    against the source instead."""

    def test_watcher_definitions_precede_the_main_guard(self):
        source = AGENT.read_text()
        guard_index = source.index('if __name__ == "__main__"')
        for needle in (
            "def make_watcher",
            "class GPasteWatcher",
            "class PollingWatcher",
            "def parse_gpaste_line",
            "TIMESTAMP_BYTES = 8",
            "class ClipPayloadError",
            "def encode_clip_payload",
            "def decode_clip_payload",
            "TYPE_CLIP_STATE = 0x02",
            "class ClipStateError",
            "def encode_clip_state",
            "def decode_clip_state",
            "def resolve_freshness",
            'SEND_MINE = "sendMine"',
            'WAIT_FOR_PEER = "waitForPeer"',
            'DO_NOTHING = "doNothing"',
            "def _xdg_dir",
            "def clip_state_path",
            "def load_clip_state",
            "def save_clip_state",
            "def resolve_startup_state",
            "def sha256_hex",
            "def resolve_current_clip_state",
            "def announce_clip_state",
        ):
            with self.subTest(needle=needle):
                self.assertLess(
                    source.index(needle), guard_index,
                    "%r must be defined before the __main__ guard, or it "
                    "never executes when the agent is run for real" % needle,
                )


if __name__ == "__main__":
    unittest.main()
