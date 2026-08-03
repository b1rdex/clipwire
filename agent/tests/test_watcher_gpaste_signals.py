# agent/tests/test_watcher_gpaste_signals.py
#
# A shard of test_watcher.py, split out in v3.2.1. Shared constants and test
# doubles are imported from the anchor rather than copied, so there is one
# definition of each. See docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import io
import os
import pathlib
import signal
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    ClipStateError,
    DEGRADED_POLL_SECONDS,
    GPASTE_BUS_NAME,
    GPasteWatcher,
    KIND_IMAGE,
    KIND_TEXT,
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    PollingWatcher,
    SAFETY_NET_POLL_SECONDS,
    TIMESTAMP_BYTES,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_IMAGE_CLIP,
    decode_clip_payload,
    decode_clip_state,
    decode_image_payload,
    encode_clip_payload,
    encode_clip_state,
    load_clip_state,
    make_watcher,
    parse_gpaste_line,
    save_clip_state,
    sha256_hex,
)
import clipwire_agent
from test_watcher import (
    GPASTE_UPDATE_LINE,
    JOIN_TIMEOUT,
    FakeGPasteProcess,
    ScriptedReadClipboard,
)
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
    installed on the machine running the suite.

    clipboard=None throughout: available() is a pure gdbus probe and never
    touches the clipboard, and none of these watchers is ever started, so the
    safety net that does use it never runs."""

    def test_true_when_introspection_succeeds(self):
        completed = subprocess.CompletedProcess(args=[], returncode=0)
        with mock.patch("subprocess.run", return_value=completed):
            self.assertTrue(GPasteWatcher(clipboard=None).available())

    def test_false_when_introspection_fails(self):
        completed = subprocess.CompletedProcess(args=[], returncode=1)
        with mock.patch("subprocess.run", return_value=completed):
            self.assertFalse(GPasteWatcher(clipboard=None).available())

    def test_false_when_gdbus_binary_is_missing(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertFalse(GPasteWatcher(clipboard=None).available())

    def test_false_when_the_probe_times_out(self):
        error = subprocess.TimeoutExpired(cmd="gdbus", timeout=3)
        with mock.patch("subprocess.run", side_effect=error):
            self.assertFalse(GPasteWatcher(clipboard=None).available())

    def test_false_when_gdbus_is_not_executable(self):
        """A gdbus present but not executable (wrong permissions, an
        AppArmor denial) raises PermissionError at subprocess.run -- an
        OSError, but not a FileNotFoundError. Mirrors the same class of
        guard already fixed for wl-paste/wl-copy in test_clipboard.py;
        uncaught here, this crashes selftest() instead of just reporting
        GPaste unavailable."""
        with mock.patch("subprocess.run", side_effect=PermissionError("denied")):
            self.assertFalse(GPasteWatcher(clipboard=None).available())

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
            GPasteWatcher(clipboard=None).available()
        self.assertEqual(GPASTE_BUS_NAME, "org.gnome.GPaste")
        self.assertIn(GPASTE_BUS_NAME, captured["cmd"])
        self.assertNotIn("org.gnome.GPaste2", captured["cmd"])


class TestGPasteWatcherLifecycle(unittest.TestCase):
    """No real gdbus is ever spawned here -- subprocess.Popen is patched to
    return a fake process backed by a real pipe, so the pump thread's
    blocking read behaves like it would against a real one.

    These tests are about the SIGNAL path only, so the safety net gets an
    interval far longer than the suite's own JOIN_TIMEOUT: it reads its
    baseline once and is then stopped mid-wait, and can never fire a change of
    its own into the assertions below. TestGPasteSafetyNet drives it instead."""

    def start_watcher(self, fake_process, on_change):
        patcher = mock.patch("subprocess.Popen", return_value=fake_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(fake_process.close)
        watcher = GPasteWatcher(
            ScriptedReadClipboard([b"unchanged"]),
            safety_net_interval_seconds=JOIN_TIMEOUT * 100,
        )
        self.addCleanup(watcher.stop)
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

    def test_the_gdbus_child_is_spawned_with_the_pdeathsig_preexec(self):
        fake_process = FakeGPasteProcess()
        self.addCleanup(fake_process.close)
        with mock.patch.object(clipwire_agent.subprocess, "Popen") as popen:
            popen.return_value = fake_process
            watcher = clipwire_agent.GPasteWatcher(clipboard=ScriptedReadClipboard([b"a"]))
            watcher.start(lambda: None)
        self.addCleanup(watcher.stop)
        self.assertIs(
            popen.call_args.kwargs.get("preexec_fn"), clipwire_agent._pdeathsig_preexec
        )


class TestPumpNeverCallsTheHandler(unittest.TestCase):
    """The thread that reads gdbus and the thread that runs the handler are
    deliberately separate.

    Until v3 the pump called on_change -- Agent._local_change -- itself, which
    made it able to BLOCK on Agent._observe_lock and able to DIE of any
    exception the handler raised, silently and for the rest of the connection,
    while the gdbus child stayed alive and kept printing lines nobody counted.
    Both were candidate causes of a production false positive in which the
    safety net declared a healthy event source dead and degraded to a
    1-second poll; neither was ever proven, and this split removes both by
    construction rather than by diagnosis.

    The pump's only job is now `_signals += 1; _event.set()`, and _signals is
    the discriminator the safety net judges the event source by -- see
    _observe_tick. A pump that can stall or die makes a live source look dead;
    a worker that dies takes the sync with it while the counter keeps climbing,
    which is why worker_alive() exists.

    There is deliberately NO queue: the GPaste Update payload carries nothing
    this agent uses -- the handler reads clipboard STATE -- so signals arriving
    while the handler runs collapse into one set() and one re-read afterwards.
    Coalescing is the right semantics for a clipboard, and the tests below emit
    their second line only after the first observation has been consumed rather
    than assuming two lines must produce two calls."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def start_watcher(self, on_change):
        """As TestGPasteWatcherLifecycle's: a fake process backed by a real
        pipe, and a safety-net interval far past JOIN_TIMEOUT so the poll takes
        its baseline read once and can never fire a change of its own into
        these assertions. Only the signal path is under test here."""
        fake_process = FakeGPasteProcess()
        self.addCleanup(fake_process.close)
        patcher = mock.patch("subprocess.Popen", return_value=fake_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        watcher = GPasteWatcher(
            ScriptedReadClipboard([b"unchanged"]),
            safety_net_interval_seconds=JOIN_TIMEOUT * 100,
        )
        self.addCleanup(watcher.stop)
        watcher.start(on_change)
        return watcher, fake_process

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_a_slow_handler_does_not_stop_the_pump_counting(self):
        """The pump must keep reading lines while the handler is busy.

        Agent._local_change blocks on _observe_lock and its own wl-paste round
        trip can take up to SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT = 13s
    on an image clipboard; a pump that calls it sits
        inside that window instead of counting, so the second and third signals
        are never counted and a safety-net tick landing there sees an unmoved
        counter on a perfectly healthy source."""
        released = threading.Event()
        self.addCleanup(released.set)   # never leave the worker parked
        entered = threading.Event()

        def slow_handler():
            entered.set()
            released.wait(JOIN_TIMEOUT)

        watcher, fake_process = self.start_watcher(slow_handler)
        for _ in range(3):
            fake_process.emit(GPASTE_UPDATE_LINE)

        self.assertTrue(
            entered.wait(JOIN_TIMEOUT),
            "the handler must actually be in flight, or this test proves nothing",
        )
        self.wait_until(lambda: watcher._signals >= 3)
        self.assertEqual(
            watcher._signals, 3,
            "the pump must count every line while the handler is busy",
        )

        released.set()
        watcher.stop()

    def test_a_signal_arriving_during_an_observation_earns_its_own_re_read(self):
        """The lost wakeup this Event is one misplaced clear() away from.

        Coalescing is only correct if the collapsed signals produce a re-read
        AFTERWARDS: a signal landing while the handler runs must still leave the
        event armed. Clearing it after the handler instead of before wipes
        exactly the change that arrived while we were looking at the previous
        one, and nothing else will ever report it -- the pump has already
        counted the line, so the safety net still calls the source healthy."""
        entered_first = threading.Event()
        release_first = threading.Event()
        self.addCleanup(release_first.set)
        calls = []
        lock = threading.Lock()

        def handler():
            with lock:
                calls.append(1)
                first = len(calls) == 1
            if first:
                entered_first.set()
                release_first.wait(JOIN_TIMEOUT)

        watcher, fake_process = self.start_watcher(handler)
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.assertTrue(
            entered_first.wait(JOIN_TIMEOUT),
            "the first observation must be in flight, or the second signal is "
            "not landing where this test needs it to",
        )

        # Strictly INSIDE the first observation.
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: watcher._signals >= 2)
        self.assertEqual(
            watcher._signals, 2, "the pump must have taken the second line already"
        )

        release_first.set()
        self.wait_until(lambda: len(calls) >= 2)
        self.assertEqual(
            len(calls), 2,
            "a signal that arrived during an observation must produce one more",
        )
        watcher.stop()

    def test_a_raising_handler_does_not_kill_the_observer(self):
        """One bad observation is disposable -- the next one re-reads the
        clipboard anyway. Against a worker with no guard the thread dies and
        every later signal is silently lost for the rest of the connection.

        The second line is emitted only after the first observation has been
        counted, because coalescing makes "two lines, two calls" a race rather
        than a contract."""
        seen = []
        lock = threading.Lock()

        def handler():
            with lock:
                seen.append(1)
                first = len(seen) == 1
            if first:
                # NOT a member of the fatal set below: a ValueError here would
                # reach os._exit(0) unmocked and end the test RUNNER with a
                # success status -- a truncated suite that reads as green.
                raise RuntimeError("first observation explodes")

        watcher, fake_process = self.start_watcher(handler)
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: len(seen) >= 1)
        self.assertEqual(len(seen), 1, "the first observation must have happened")

        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: len(seen) >= 2)

        self.assertEqual(len(seen), 2, "the observer must survive a raising handler")
        self.assertTrue(watcher.worker_alive())
        watcher.stop()

    def test_a_disposable_handler_error_is_logged_with_its_traceback(self):
        """A thread that dies quietly is the exact defect this split removes;
        a thread that swallows quietly is the same defect one debugging session
        later. The traceback is the whole diagnostic value: the log line is all
        anyone gets from the PC."""
        def handler():
            raise RuntimeError("first observation explodes")

        watcher, fake_process = self.start_watcher(handler)
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: any("observer error" in line for line in self.log_lines))
        watcher.stop()

        logged = "\n".join(self.log_lines)
        self.assertIn("observer error", logged)
        self.assertIn(
            "Traceback (most recent call last)", logged,
            "log the traceback, not just the exception: %r" % self.log_lines,
        )
        self.assertIn("first observation explodes", logged)

    def _assert_takes_the_agent_down(self, error):
        def handler():
            raise error

        with mock.patch.object(clipwire_agent.os, "_exit") as exit_call:
            watcher, fake_process = self.start_watcher(handler)
            fake_process.emit(GPASTE_UPDATE_LINE)
            self.wait_until(lambda: exit_call.called)
            try:
                self.assertTrue(
                    exit_call.called,
                    "%s must take the agent down" % type(error).__name__,
                )
                self.assertEqual(
                    exit_call.call_args, mock.call(0),
                    "a dead channel is a clean exit, not a crash status",
                )
            finally:
                # Stopped and JOINED inside the patch, and in a finally rather
                # than after the assertions: once os._exit is the real one
                # again, a worker still in flight would end the test RUNNER
                # outright -- with status 0, so a truncated suite would read as
                # green. An AssertionError above escapes the `with` and
                # restores os._exit, so the failing path needs this at least as
                # much as the passing one. The None guard covers a watcher that
                # never reached start(): a TypeError here would replace the
                # real failure with a bogus one.
                watcher.stop()
                if watcher._worker is not None:
                    watcher._worker.join(timeout=JOIN_TIMEOUT)
            self.assertFalse(
                watcher._worker.is_alive(),
                "the worker must be gone before os._exit is unpatched",
            )

    def test_a_dead_channel_takes_the_agent_down(self):
        """Not a disposable observation. This agent is one process per
        connection and exiting IS how it reports a dead channel, mirroring
        run()'s rule for stdin EOF -- a worker that logged and carried on would
        leave a process syncing into a pipe nobody reads.

        Both members of the fatal set are pinned here. ValueError is not
        hypothetical: it is what a CLOSED stdout raises on write, and
        Agent.send writes straight to it."""
        for error in (BrokenPipeError("peer went away"),
                      ValueError("I/O operation on closed file")):
            with self.subTest(error=type(error).__name__):
                self._assert_takes_the_agent_down(error)

    def test_stop_wakes_a_worker_parked_on_the_event(self):
        """A worker parked in Event.wait() has nothing else to wake it, and
        Agent.clipboard_lost() drops its reference to the watcher the moment
        stop() returns -- so a thread left parked here can never be reached
        again, and every Wayland flap leaks another one.

        Bounded rather than hanging: the join times out and the assertion
        fails."""
        watcher, _ = self.start_watcher(lambda: None)
        self.assertTrue(
            watcher.worker_alive(), "the worker must be running to begin with"
        )

        watcher.stop()
        watcher._worker.join(timeout=JOIN_TIMEOUT)

        self.assertFalse(
            watcher._worker.is_alive(),
            "stop() must wake a worker parked in _event.wait(), not only set a flag",
        )
        self.assertFalse(watcher.worker_alive())


