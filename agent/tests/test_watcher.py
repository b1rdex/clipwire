# agent/tests/test_watcher.py
import io
import os
import pathlib
import subprocess
import time
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    GPASTE_BUS_NAME,
    GPasteWatcher,
    MAX_PAYLOAD_BYTES,
    PollingWatcher,
    TYPE_CLIP,
    make_watcher,
    parse_gpaste_line,
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

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready)
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

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready)
        )

    def become_ready_without_a_real_watcher(self, agent):
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

    def test_write_clip_writes_and_arms_the_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(b"hello")
        self.assertEqual(agent.clipboard.written, [b"hello"])
        self.assertEqual(agent._last_written, b"hello")

    def test_immediate_clip_delivery_arms_the_suppression(self):
        agent = self.build(ready=True)
        self.become_ready_without_a_real_watcher(agent)
        agent.on_frame(TYPE_CLIP, b"now")
        self.assertEqual(agent.clipboard.written, [b"now"])
        self.assertEqual(agent._last_written, b"now")

    def test_pending_clip_delivery_arms_the_same_suppression(self):
        """If clipboard_became_ready() wrote pending_clip directly instead of
        through _write_clip, delivery would leave no suppression armed, and
        the very next poll tick would bounce our own delivered clip back to
        the peer as if the user had copied it."""
        agent = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, b"queued while pending")
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)
        self.assertEqual(agent.clipboard.written, [b"queued while pending"])
        self.assertEqual(agent._last_written, b"queued while pending")

    def test_matching_echo_is_suppressed_and_consumed(self):
        agent = self.build(ready=True)
        agent._write_clip(b"hello")
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
        agent._write_clip(b"hello")
        agent.clipboard.queue_read(b"something else")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, b"something else")])
        self.assertIsNone(
            agent._last_written,
            "the suppression must be consumed on a mismatch too, not only on a match",
        )

    def test_fresh_agent_sends_a_genuine_local_change(self):
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, b"typed by the user")])

    def test_deliberate_recopy_still_syncs_after_a_missed_echo(self):
        """Mirrors EchoGuardTests.testDeliberateRecopyStillSyncsAfterAMissedEcho
        on the Swift side, so the two implementations cannot drift on it."""
        agent = self.build(ready=True)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        our_write = b"our own write"
        something_else = b"a different clip the user made"

        agent._write_clip(our_write)
        agent.clipboard.queue_read(something_else)
        agent._local_change()  # the poll missed our echo, saw the user's clip instead

        agent.clipboard.queue_read(our_write)
        agent._local_change()  # a deliberate re-copy of our own text, later

        self.assertEqual(
            sent,
            [(TYPE_CLIP, something_else), (TYPE_CLIP, our_write)],
            "a later deliberate re-copy of our own text must still sync",
        )

    def test_deliberate_recopy_still_syncs_after_a_pending_delivery_misses_its_echo(self):
        """Same scenario, but the write that gets echoed-past is the
        pending-clip delivery, not an immediate one -- the path Task 8 never
        exercised."""
        agent = self.build(ready=False)
        delivered = b"queued while pending"
        agent.on_frame(TYPE_CLIP, delivered)
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)

        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        something_else = b"a different clip the user made"

        agent.clipboard.queue_read(something_else)
        agent._local_change()

        agent.clipboard.queue_read(delivered)
        agent._local_change()

        self.assertEqual(sent, [(TYPE_CLIP, something_else), (TYPE_CLIP, delivered)])

    def test_empty_read_does_not_consume_an_armed_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(b"hello")
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
        agent._write_clip(b"A")  # "A" arrived from the Mac
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
        agent._local_change()  # a genuine first sync of "A"
        self.assertEqual(sent, [(TYPE_CLIP, b"A")])

        agent.clipboard.queue_read(b"A")  # unchanged content, spurious signal
        agent._local_change()
        self.assertEqual(
            sent, [(TYPE_CLIP, b"A")],
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
        agent._local_change()  # "A" is now the known-synced value
        self.assertEqual(sent, [(TYPE_CLIP, b"A")])

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
            sent, [(TYPE_CLIP, b"A")],
            "a transient read() glitch that recovers to the same content must not resend it",
        )


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
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard())
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent._write_clip(b"A")
        agent.clipboard = RacyClipboard(agent, value_read=b"A", interleaved_write=b"B")

        agent._local_change()

        self.assertEqual(
            sent, [], "a write observed mid-read must not be echoed back as genuine"
        )
        self.assertEqual(
            agent._last_written, b"B",
            "the newer write's suppression must stay armed for its own echo",
        )


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
        ):
            with self.subTest(needle=needle):
                self.assertLess(
                    source.index(needle), guard_index,
                    "%r must be defined before the __main__ guard, or it "
                    "never executes when the agent is run for real" % needle,
                )


if __name__ == "__main__":
    unittest.main()
