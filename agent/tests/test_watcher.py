# agent/tests/test_watcher.py
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
    MAX_PAYLOAD_BYTES,
    PollingWatcher,
    SAFETY_NET_POLL_SECONDS,
    TIMESTAMP_BYTES,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    decode_clip_payload,
    decode_clip_state,
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
# A verbatim line captured from the target machine -- see
# TestGPasteSignalParsing.test_accepts_the_real_captured_signal.
GPASTE_UPDATE_LINE = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                      "('REPLACE', 'ALL', uint64 0)")

# 64 lowercase hex characters: the only shape decode_clip_state accepts,
# and the only shape hashlib.sha256(...).hexdigest() -- hence the wire --
# ever produces. Obviously fake, but well-formed, so these tests exercise
# the same path a real digest does instead of one the protocol forbids.
# See _is_sha256_hex on why that validation exists at all. HASH_A sorts
# below HASH_B, which resolve_freshness's hash tie-break depends on.
HASH_A = "aa" * 32
HASH_B = "bb" * 32


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
        trip can take up to SUBPROCESS_TIMEOUT=3s; a pump that calls it sits
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


class TestPdeathsigPreexec(unittest.TestCase):
    def test_load_libc_is_none_when_libc_is_unavailable(self):
        """The macOS path: CDLL("libc.so.6") raises OSError there (no such
        library), and _load_libc() must come back with None rather than let
        it propagate -- this is what lets the whole module still import
        cleanly on the machine this suite runs on. Mocked rather than relying
        on the real macOS behavior, so this also pins the same contract on
        Linux, where CDLL would otherwise succeed for real."""
        with mock.patch.object(clipwire_agent.ctypes, "CDLL", side_effect=OSError):
            self.assertIsNone(clipwire_agent._load_libc())

    def test_it_does_not_resolve_libc_at_call_time(self):
        """The whole point of _LIBC: nothing inside preexec_fn may import,
        allocate, or take a lock, because preexec_fn runs in a forked child of
        a threaded process. _load_libc() calling import ctypes is fine at
        module load and would be a hazard here -- so this pins that
        _pdeathsig_preexec never calls it, not merely that _LIBC is read."""
        with mock.patch.object(clipwire_agent, "_load_libc") as loader, \
             mock.patch.object(clipwire_agent, "_LIBC", None):
            clipwire_agent._pdeathsig_preexec()
        loader.assert_not_called()

    def test_it_requests_sigterm_when_the_parent_dies(self):
        calls = []

        class FakeLibc:
            def prctl(self, option, sig, *rest):
                calls.append((option, sig))
                return 0

        with mock.patch.object(clipwire_agent, "_LIBC", FakeLibc()), \
             mock.patch.object(clipwire_agent.os, "getppid", return_value=42):
            clipwire_agent._pdeathsig_preexec()

        self.assertEqual(calls, [(clipwire_agent.PR_SET_PDEATHSIG, signal.SIGTERM)])

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

        with mock.patch.object(clipwire_agent, "_LIBC", FakeLibc()), \
             mock.patch.object(clipwire_agent.os, "getppid", return_value=1), \
             mock.patch.object(clipwire_agent.os, "_exit") as exit_call:
            clipwire_agent._pdeathsig_preexec()
        exit_call.assert_called_once_with(0)

    def test_it_is_a_no_op_where_prctl_is_unavailable(self):
        with mock.patch.object(clipwire_agent, "_LIBC", None):
            clipwire_agent._pdeathsig_preexec()   # must not raise


class ScriptedReadClipboard:
    """read() replays a fixed script, then repeats its last value forever --
    so a poll tick that lands after the test stops watching cannot raise
    IndexError. `last` records the most recent value returned, so a test
    callback can observe what the watcher just saw without threading the
    value through on_change() itself (the real interface takes none).

    `paced` is what makes that observation DETERMINISTIC, and every test
    asserting on which values were seen needs it. Since the watchers stopped
    calling the handler on their reader threads, a change is signalled and the
    handler runs later, on the worker: an unpaced script can advance `last`
    between the two, and two changes inside one worker turnaround coalesce into
    a single observation. Both are correct in production -- the real handler
    re-reads current state -- and both make "which values were observed" depend
    on the scheduler.

    Paced, read() refuses to hand out a NEW value until the previous change has
    been taken through take(). That is the same "block inside read() until the
    other thread has caught up" handshake SignallingClipboard uses below, and
    it removes the race structurally rather than making it unlikely. Bounded by
    JOIN_TIMEOUT so a change nobody observes fails an assertion instead of
    hanging the suite."""

    def __init__(self, script, paced=False):
        self._script = list(script)
        self.calls = 0
        self.last = None
        self._paced = paced
        # Set means "nothing is waiting to be observed", so the baseline read
        # is free.
        self._taken = threading.Event()
        self._taken.set()

    def take(self):
        """For a test's on_change: the value the watcher just observed, and the
        release that lets a paced script move on. Safe to call on an unpaced
        clipboard, so one callback shape works for both."""
        value = self.last
        self._taken.set()
        return value

    def read(self):
        if self._paced:
            self._taken.wait(JOIN_TIMEOUT)
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        value = self._script[index]
        # calls > 1 because the poll's FIRST read is its baseline, which it
        # never reports: pending an observation on it would park the next read
        # for the whole timeout.
        if self._paced and self.calls > 1 and value != self.last:
            self._taken.clear()
        self.last = value
        return self.last


class TestPollingWatcher(unittest.TestCase):
    """A standalone poller -- what make_watcher returns when GPaste is
    unavailable -- owns the event AND the worker, so the same "no handler call
    on a reader thread" rule holds with nothing composed above it."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_available_is_always_true(self):
        self.assertTrue(PollingWatcher(clipboard=None, interval_seconds=1).available())

    def test_a_slow_handler_does_not_stop_the_poll_loop_reading(self):
        """The poll loop is a reader thread like the gdbus pump, and it carries
        more: it is the only caller of _observe_tick, so a loop parked inside
        Agent._local_change is a safety net that has stopped judging anything.
        _local_change blocks on _observe_lock and its own wl-paste round trip
        can take up to SUBPROCESS_TIMEOUT=3s."""
        released = threading.Event()
        self.addCleanup(released.set)
        entered = threading.Event()

        def slow_handler():
            entered.set()
            released.wait(JOIN_TIMEOUT)

        clipboard = ScriptedReadClipboard([b"a", b"b"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.002)
        self.addCleanup(watcher.stop)
        watcher.start(slow_handler)

        self.assertTrue(
            entered.wait(JOIN_TIMEOUT),
            "the handler must actually be in flight, or this test proves nothing",
        )
        at_entry = clipboard.calls
        self.wait_until(lambda: clipboard.calls >= at_entry + 3)
        observed = clipboard.calls - at_entry

        released.set()
        watcher.stop()
        self.assertGreaterEqual(
            observed, 3,
            "the poll must keep reading while the handler is busy; got %d more "
            "reads" % observed,
        )

    def test_a_raising_handler_does_not_kill_the_poll_loop(self):
        """One bad observation is disposable. A poll loop that dies with the
        handler takes the ONLY remaining clipboard observer with it -- for a
        standalone poller there is no signal path left to fall back to."""
        seen = []
        lock = threading.Lock()

        def handler():
            with lock:
                seen.append(1)
                first = len(seen) == 1
            if first:
                raise RuntimeError("first observation explodes")

        clipboard = ScriptedReadClipboard([b"a", b"b", b"c", b"d"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.005)
        self.addCleanup(watcher.stop)
        watcher.start(handler)

        self.wait_until(lambda: len(seen) >= 2)
        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertGreaterEqual(
            len(seen), 2, "the poller must survive a raising handler"
        )
        self.assertTrue(
            any("observer error" in line for line in self.log_lines),
            "the disposable error must be logged: %r" % self.log_lines,
        )

    def test_a_raising_on_tick_does_not_kill_the_poll_loop(self):
        """The other half, and the one with no second chance: on_tick is
        GPasteWatcher._observe_tick, the safety net's whole judgement. An
        exception there killed the loop and left NOTHING watching -- neither
        syncing nor able to diagnose that it had stopped."""
        ticks = []

        def on_tick(previous, current):
            ticks.append((previous, current))
            if len(ticks) == 1:
                raise RuntimeError("the first verdict explodes")

        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.005, on_tick=on_tick)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: None)

        self.wait_until(lambda: len(ticks) >= 3)
        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertGreaterEqual(
            len(ticks), 3, "the poll loop must keep ticking after a bad verdict"
        )

    def test_stop_wakes_a_standalone_pollers_own_worker(self):
        """Same leak as GPasteWatcher's: Agent.clipboard_lost drops its
        reference the moment stop() returns, so a worker parked in
        _event.wait() can never be reached again. Bounded rather than hanging:
        the join times out and the assertion fails."""
        clipboard = ScriptedReadClipboard([b"a"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.005)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: None)
        self.assertIsNotNone(
            watcher._worker, "a standalone poller must own a worker of its own"
        )

        watcher.stop()
        watcher._worker.join(timeout=JOIN_TIMEOUT)

        self.assertFalse(
            watcher._worker.is_alive(),
            "stop() must wake a worker parked in _event.wait(), not only set a flag",
        )

    def test_fires_only_on_an_actual_change(self):
        # Paced: this asserts WHICH values were observed, and the handler no
        # longer runs on the thread that read them -- see ScriptedReadClipboard.
        clipboard = ScriptedReadClipboard(
            [b"a", b"a", b"a", b"b", b"b", b"c"], paced=True)
        changes = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.01)
        watcher.start(lambda: changes.append(clipboard.take()))

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


class SignallingClipboard:
    """A clipboard whose read() DRIVES the signal source: on the tick that
    first sees new content it emits a real GPaste Update line and blocks until
    the pump has delivered it, only then returning the changed value. So "the
    signal for this very change has already been delivered" is a fact by the
    time the safety net compares, not a timing hope -- the same
    deterministic-side-effect-inside-read() trick RacyClipboard uses further
    down this file, instead of racing two real threads and hoping.

    Deliberately waits on the CALLBACK having run rather than on the watcher's
    private signal counter, so the test is evidence about observable behaviour
    rather than about an attribute.

    What it does NOT prove: that the pump counts a signal BEFORE dispatching
    it. In a count-after-dispatch variant the increment and the dispatch are
    adjacent bytecodes on the pump thread while the released poll thread still
    has to be rescheduled, so this test would normally pass anyway. That
    ordering is argued in the pump's own comment and is deliberately left
    untested rather than pinned by a test that would pass most of the time."""

    def __init__(self, before, after):
        self.process = None       # set by start_watcher, before anything reads
        self.delivered = threading.Event()
        self.reports = 0
        self._before = before
        self._after = after
        self.calls = 0
        self.last = None

    def observe(self):
        """The on_change both paths share: counts reports and releases the
        read() waiting for the signal to have been delivered."""
        self.reports += 1
        self.delivered.set()

    def read(self):
        self.calls += 1
        if self.calls == 1:
            self.last = self._before   # the poll loop's own baseline
            return self.last
        if not self.delivered.is_set():
            self.process.emit(GPASTE_UPDATE_LINE)
            self.delivered.wait(JOIN_TIMEOUT)
        self.last = self._after
        return self.last


class TestGPasteSafetyNet(unittest.TestCase):
    """GPasteWatcher.available() probes the bus NAME, but GPaste tracks the
    clipboard through a gnome-shell extension: a GNOME upgrade can leave the
    daemon running and the bus answering while the extension is disabled, so
    Update never fires, PC->Mac sync is silently dead, and every probe still
    reports health. The safety net is the only thing keyed on events being
    ABSENT rather than on the bus being unreachable.

    No real gdbus is ever spawned -- subprocess.Popen is patched, exactly as in
    TestGPasteWatcherLifecycle -- and both intervals are constructor
    parameters so these tests run at millisecond scale instead of the
    production 30 seconds."""

    SWITCH_MARKER = "not reporting clipboard changes"

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)
        self.changes = []

    def switch_log_lines(self):
        return [line for line in self.log_lines if self.SWITCH_MARKER in line]

    def start_watcher(self, clipboard, on_change=None,
                      safety_net_interval_seconds=0.01,
                      degraded_interval_seconds=0.01,
                      degraded=False, on_degrade=None):
        fake_process = FakeGPasteProcess()
        # A clipboard that drives the signal source from inside its own read()
        # must hold the process BEFORE anything reads: PollingWatcher takes
        # its baseline read the instant start() is called.
        if hasattr(clipboard, "process"):
            clipboard.process = fake_process
        patcher = mock.patch("subprocess.Popen", return_value=fake_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(fake_process.close)
        watcher = GPasteWatcher(
            clipboard,
            safety_net_interval_seconds=safety_net_interval_seconds,
            degraded_interval_seconds=degraded_interval_seconds,
            degraded=degraded,
            on_degrade=on_degrade,
        )
        # Stopped in cleanup as well as in the tests themselves, so a failing
        # assertion cannot leave a poll thread running against a torn-down
        # fixture.
        self.addCleanup(watcher.stop)
        # take() rather than `last`: it is the release half of a paced script's
        # handshake, and a plain read of `last` on an unpaced one.
        watcher.start(on_change or (lambda: self.changes.append(clipboard.take())))
        return watcher, fake_process

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.001)

    def quiesce(self, watcher):
        watcher.stop()
        watcher._safety_net._thread.join(timeout=JOIN_TIMEOUT)
        watcher._thread.join(timeout=JOIN_TIMEOUT)

    def test_changes_the_signal_path_missed_are_reported_and_the_switch_logged_once(self):
        """The whole point of the task: a signal source that produces nothing
        while the clipboard content moves. Two changes across two ticks, so
        this pins both halves at once -- every missed change is still reported
        through the same on_change a signal would use, and the switch is
        logged exactly ONCE however many further changes the poll goes on to
        catch (the latch, mirroring Agent._clip_state_sent's shape)."""
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"], paced=True)
        watcher, _ = self.start_watcher(clipboard)

        self.wait_until(lambda: len(self.changes) >= 2)
        self.quiesce(watcher)

        self.assertEqual(
            self.changes, [b"b", b"c"],
            "both changes the dead signal path missed must be reported",
        )
        self.assertEqual(
            len(self.switch_log_lines()), 1,
            "the switch must be logged exactly once, not once per missed "
            "change; got: %r" % self.log_lines,
        )

    def test_one_diverging_tick_does_not_degrade(self):
        """The hysteresis, stated as the thing that must NOT happen.

        v2 degraded from a SINGLE tick, and that ruling was safe only because
        the poll loop called the handler synchronously: _local_change always
        forks wl-paste, so milliseconds always passed between the content read
        and the verdict -- enough for a signal already in flight to be counted.
        Decoupling the loop from the handler deleted that grace period, so one
        diverging tick is no longer evidence: a copy landing in the last few
        milliseconds before the read gets judged missed while its Update is
        still in flight, and a healthy machine is degraded to 1-second polling
        for the rest of the connection. That is the very defect this release
        exists to fix.

        The script changes exactly once and then repeats, so there can never be
        a second consecutive divergence. Against a one-tick verdict this goes
        red on the assertion below."""
        clipboard = ScriptedReadClipboard([b"a", b"b"])
        watcher, _ = self.start_watcher(clipboard)

        # The baseline read, the diverging tick, and three settled ticks after
        # it -- far past the point a one-tick verdict would have fired.
        self.wait_until(lambda: clipboard.calls >= 5)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 5,
            "the poll must have run well past the divergence, or this test "
            "proves nothing",
        )
        self.assertEqual(
            self.switch_log_lines(), [],
            "one diverging tick must ARM the verdict, not fire it; got: %r"
            % self.log_lines,
        )

    def test_the_verdict_lands_on_the_second_consecutive_diverging_tick(self):
        """Not merely "it degrades eventually" -- that passes against a
        one-tick verdict too and would pin nothing. This pins WHICH tick fires
        it.

        on_degrade runs synchronously inside _observe_tick, on the poll thread,
        and clipboard.calls is incremented only by that same thread, so the
        read count at the moment of the verdict is exact with no sleep and no
        waiting: one baseline read plus two ticks. A one-tick verdict records
        2."""
        reads_at_verdict = []
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(
            clipboard, on_degrade=lambda: reads_at_verdict.append(clipboard.calls))

        self.wait_until(lambda: reads_at_verdict)
        self.quiesce(watcher)

        self.assertEqual(
            reads_at_verdict, [3],
            "the verdict must land on the SECOND consecutive diverging tick -- "
            "one baseline read plus two ticks",
        )

    def test_a_healthy_signal_source_plus_a_content_change_does_not_log_the_switch(self):
        """THE trap in this task. When GPaste is healthy the user copies
        something, the signal fires and is handled -- and then the safety net's
        next tick ALSO sees content differing from its own baseline, because it
        keeps one. An implementation that concludes "the event source is dead"
        from a content difference alone logs the switch and degrades EVERY
        healthy installation to polling on the user's first copy: worse than
        the bug the safety net fixes, and entirely invisible to a test that
        only ever drives a dead source. The discriminator is the count of
        accepted signals, not the content difference."""
        clipboard = SignallingClipboard(before=b"a", after=b"b")
        watcher, _ = self.start_watcher(clipboard, on_change=clipboard.observe)

        # calls >= 3: the baseline read, the tick that sees the change (having
        # first driven the signal to completion), and one further tick.
        self.wait_until(lambda: clipboard.calls >= 3)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 3,
            "the safety net must actually have compared across a content "
            "change, or this test proves nothing",
        )
        self.assertGreaterEqual(
            clipboard.reports, 1, "the signal path must actually have fired"
        )
        self.assertEqual(
            self.switch_log_lines(), [],
            "a healthy signal source must not be declared dead just because "
            "the safety net also noticed the change it reported",
        )

    def test_a_failed_read_and_its_recovery_are_not_read_as_a_missed_change(self):
        """WaylandClipboard.read() returns None for a failed wl-paste -- it
        keeps a dedicated one-shot log line for exactly that timeout -- and
        also for a genuinely empty selection. No selection change is involved
        either way, so the signal counter is GUARANTEED not to have moved:
        judging a None transition as a missed change needs no race at all to
        fire, and one flaky wl-paste on a healthy, idle system would
        permanently degrade the connection while blaming the gnome-shell
        extension for it. Both the failure (a->None) and the recovery
        (None->a) look like changes to the comparison."""
        clipboard = ScriptedReadClipboard([b"a", None, b"a", b"a"])
        watcher, _ = self.start_watcher(clipboard)

        self.wait_until(lambda: clipboard.calls >= 5)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 5, "the poll must actually have run past the recovery"
        )
        self.assertEqual(
            self.switch_log_lines(), [],
            "a failed read and its recovery are not evidence that the event "
            "source missed anything",
        )

    def test_the_pump_counts_only_accepted_update_lines(self):
        """The discriminator's own input. Counting every line gdbus prints
        (it opens with a "Monitoring signals..." banner, and GPaste emits
        other signals on the same object) would make a dead source look alive
        on unrelated traffic; not counting at all makes every healthy
        installation look dead."""
        clipboard = ScriptedReadClipboard([b"unchanged"])
        watcher, fake_process = self.start_watcher(
            clipboard, safety_net_interval_seconds=JOIN_TIMEOUT * 100)

        fake_process.emit("Monitoring signals...")
        fake_process.emit("/org/gnome/GPaste: org.gnome.GPaste2.ShowHistory ()")
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: len(self.changes) >= 1)
        self.quiesce(watcher)

        self.assertEqual(
            watcher._signals, 1,
            "only the Update line is a signal; the banner and ShowHistory are not",
        )

    def test_production_defaults_are_a_thirty_second_budget_and_a_one_second_degraded_poll(self):
        """The interval ruling, pinned without timing anything: 30 seconds is
        a DETECTION budget, an acceptable worst case for noticing a broken
        subscription and an unusable one for actually syncing. Leaving it as
        the operating interval after the switch would keep PC->Mac half a
        minute behind while reporting itself as working."""
        watcher = GPasteWatcher(clipboard=None)  # never started
        self.assertEqual(watcher._safety_net.interval, SAFETY_NET_POLL_SECONDS)
        self.assertEqual(SAFETY_NET_POLL_SECONDS, 30.0)
        self.assertEqual(watcher._degraded_interval, DEGRADED_POLL_SECONDS)
        self.assertEqual(DEGRADED_POLL_SECONDS, 1.0)

    def test_the_switch_polls_at_the_degraded_interval_not_the_detection_budget(self):
        """The same ruling as a live transition rather than a default: the
        interval changes, the mechanism does not."""
        # Three values, not two: the verdict now needs two CONSECUTIVE
        # diverging ticks, so a dead source must be given two changes to miss.
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=0.01, degraded_interval_seconds=0.05)

        self.wait_until(lambda: self.switch_log_lines())
        self.quiesce(watcher)

        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")
        self.assertEqual(
            watcher._safety_net.interval, 0.05,
            "after switching, the poll must run at the degraded interval, not "
            "stay on the detection budget",
        )

    def test_after_switching_the_loop_actually_ticks_at_the_degraded_rate(self):
        """Assigning the field is not the requirement -- polling at the new
        rate is. An implementation that captured `interval` into a local before
        the loop, or restarted nothing, would pass every other assertion here
        while leaving PC->Mac sync a full detection budget behind and
        reporting itself as working, which is exactly the failure the interval
        ruling names. So this counts TICKS in a window shorter than the budget:
        at the degraded rate three of them need ~15ms, while a loop still on
        the budget cannot deliver even one, since Event.wait does not return
        early."""
        budget, degraded, window = 0.03, 0.002, 0.02
        # Three values: two consecutive diverging ticks are needed to switch.
        # Deliberately NOT paced -- this counts ticks in a 20ms window and a
        # blocking read would starve the count.
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=budget,
            degraded_interval_seconds=degraded)

        self.wait_until(lambda: self.switch_log_lines())
        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")

        # Every tick reads, change or not, so the script needs nothing more.
        at_switch = clipboard.calls
        deadline = time.monotonic() + window
        while clipboard.calls < at_switch + 3 and time.monotonic() < deadline:
            time.sleep(0.001)
        observed = clipboard.calls - at_switch
        self.quiesce(watcher)

        self.assertGreaterEqual(
            observed, 3,
            "expected at least 3 ticks within %ss of the switch (the degraded "
            "rate needs ~%ss for them); a loop still on the %ss budget delivers "
            "none. Got %d" % (window, 3 * degraded, budget, observed),
        )

    def test_the_gdbus_child_survives_the_switch_and_recovered_signals_still_report(self):
        """Deliberately NOT terminating the subscription on the switch: if the
        extension is re-enabled the signals resume and still funnel through the
        same on_change, which dedupes by content on the Agent side. Tearing
        that child down mid-connection is the SIGPIPE class of bug that already
        killed this project's Mac agent once, at exactly the moment the PC
        rebooted."""
        # Three values for the two consecutive diverging ticks the verdict
        # now needs, and paced so both are observed before the count below is
        # taken -- otherwise a poll-driven change still in flight could satisfy
        # the assertion that only the emitted SIGNAL is supposed to.
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"], paced=True)
        watcher, fake_process = self.start_watcher(clipboard)

        self.wait_until(lambda: self.switch_log_lines())
        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")
        self.assertFalse(
            fake_process.terminated,
            "the subscription must be left alive to recover on its own",
        )

        # The script is exhausted and repeats its last value forever, so the
        # poll cannot add a change here: only the emitted signal can. Both of
        # its changes must have been OBSERVED first, or the count below could
        # be satisfied by one of them landing late.
        self.wait_until(lambda: len(self.changes) >= 2)
        self.assertEqual(len(self.changes), 2, "both poll-driven changes must be in")
        reported = len(self.changes)
        fake_process.emit(GPASTE_UPDATE_LINE)
        self.wait_until(lambda: len(self.changes) > reported)
        self.quiesce(watcher)

        self.assertGreater(
            len(self.changes), reported,
            "a recovered signal must still reach the same on_change after the switch",
        )

    def test_the_safety_net_signals_the_same_event_instead_of_calling_the_handler(self):
        """One observation path, now structural rather than argued.

        This used to assert that PollingWatcher.start received the very
        callback object the signal path got -- true, and the strongest
        statement available while the safety net still CALLED it. It no longer
        does: the composed poller signals this watcher's own event, so the
        handler is called from exactly one place in the process, by exactly one
        thread. Exactly one place still decides what a local change means --
        one observation, one one-shot echo suppression, one _last_seen -- and a
        second copy of that decision remains the shape of the echo bug this
        project has already fixed two races in.

        The composed poller owning a worker of its own would be that second
        copy, and would also put two threads back inside _local_change."""
        clipboard = ScriptedReadClipboard([b"a"])
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=JOIN_TIMEOUT * 100)

        self.assertIs(
            watcher._safety_net._event, watcher._event,
            "the safety net must signal the watcher's own event, not one of its own",
        )
        self.assertIsNone(
            watcher._safety_net._worker,
            "exactly one worker per watcher tree: the composed poller must not "
            "start a second one",
        )
        self.assertIsNotNone(watcher._worker, "the watcher owns the only worker")

    def test_the_verdict_still_arrives_after_the_handler_has_raised(self):
        """The behaviour the wiring exists for, and the one a future refactor
        will break while preserving the wiring.

        The poll loop is the only caller of _observe_tick. While it called
        on_change itself, one exception from Agent._local_change killed it --
        and with it the only thing that can ever diagnose a dead event source
        or switch to the degraded interval. Silent failure of the detector of
        silent failure: the watcher would report itself healthy forever."""
        calls = []
        lock = threading.Lock()

        def on_change():
            with lock:
                calls.append(1)
                first = len(calls) == 1
            if first:
                raise RuntimeError("first observation explodes")

        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(clipboard, on_change=on_change)

        # BOTH facts, not just the verdict: the two now happen on different
        # threads and in no fixed order. _on_tick runs on the poll thread
        # immediately after the signal, so the verdict can be logged before the
        # worker has been scheduled at all -- waiting on the verdict alone
        # quiesces a watcher whose handler has not run yet, and the premise
        # below then fails on a correct implementation.
        self.wait_until(lambda: calls and self.switch_log_lines())
        self.quiesce(watcher)

        self.assertGreaterEqual(
            len(calls), 1, "the handler must actually have raised, or this "
            "test proves nothing",
        )
        self.assertEqual(
            len(self.switch_log_lines()), 1,
            "the safety net must still reach its verdict after the handler "
            "blew up; got: %r" % self.log_lines,
        )

    def test_a_slow_handler_does_not_stop_the_safety_net_ticking(self):
        """The other hazard the split removes from this thread. A poll loop
        parked inside _local_change -- which blocks on _observe_lock and can
        spend SUBPROCESS_TIMEOUT=3s in one wl-paste -- is a safety net that has
        stopped observing for as long as the handler runs."""
        released = threading.Event()
        self.addCleanup(released.set)
        entered = threading.Event()

        def slow_handler():
            entered.set()
            released.wait(JOIN_TIMEOUT)

        clipboard = ScriptedReadClipboard([b"a", b"b"])
        watcher, _ = self.start_watcher(
            clipboard, on_change=slow_handler, safety_net_interval_seconds=0.002)

        self.assertTrue(
            entered.wait(JOIN_TIMEOUT),
            "the handler must actually be in flight, or this test proves nothing",
        )
        at_entry = clipboard.calls
        self.wait_until(lambda: clipboard.calls >= at_entry + 3)
        observed = clipboard.calls - at_entry

        released.set()
        self.quiesce(watcher)
        self.assertGreaterEqual(
            observed, 3,
            "the safety net must keep polling while the handler is busy; got "
            "%d more reads" % observed,
        )

    def test_a_watcher_built_already_degraded_polls_fast_and_stays_quiet(self):
        """One half of the flap fix: `clipboard_lost` discards the watcher and
        `clipboard_became_ready` builds a fresh one, so the verdict has to be
        handed back IN. A rebuilt watcher must come up on the degraded interval
        and must not log the diagnosis a second time on the same connection."""
        clipboard = ScriptedReadClipboard([b"a", b"b"], paced=True)
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=0.20,
            degraded_interval_seconds=0.01, degraded=True)

        self.assertEqual(
            watcher._safety_net.interval, 0.01,
            "a watcher built already degraded must poll at the degraded interval "
            "from its first tick, not re-arm the detection budget",
        )

        self.wait_until(lambda: self.changes)
        self.quiesce(watcher)

        self.assertEqual(self.changes, [b"b"], "the change must still be reported")
        self.assertEqual(
            self.switch_log_lines(), [],
            "the diagnosis must not be logged a second time on one connection",
        )

    def test_the_diagnosis_is_reported_outward_exactly_once(self):
        """The other half: an Agent-level latch is only connection-scoped if
        the watcher actually tells it. Two missed changes, one notification."""
        notified = []
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"], paced=True)
        watcher, _ = self.start_watcher(
            clipboard, on_degrade=lambda: notified.append(1))

        self.wait_until(lambda: len(self.changes) >= 2)
        self.quiesce(watcher)

        self.assertEqual(len(self.changes), 2, "both missed changes must be reported")
        self.assertEqual(
            notified, [1],
            "the diagnosis must be reported outward exactly once, like the log line",
        )

    def test_stop_ends_the_safety_net_poll_too(self):
        """A leaked poll thread keeps forking wl-paste against a session that
        has gone away -- and Agent.clipboard_lost() drops its reference to the
        watcher, so nothing can ever stop it afterwards."""
        clipboard = ScriptedReadClipboard([b"a"])
        watcher, _ = self.start_watcher(clipboard, safety_net_interval_seconds=0.02)

        watcher.stop()
        watcher._safety_net._thread.join(timeout=JOIN_TIMEOUT)

        self.assertFalse(
            watcher._safety_net._thread.is_alive(),
            "stop() must end the safety-net poll, not only the gdbus pump",
        )


class TestMakeWatcher(unittest.TestCase):
    def test_uses_gpaste_when_available(self):
        clipboard = object()
        on_degrade = object()
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=clipboard, on_degrade=on_degrade)
        self.assertIsInstance(watcher, GPasteWatcher)
        self.assertIs(
            watcher.clipboard, clipboard,
            "the clipboard must reach the watcher, or its safety net polls nothing",
        )
        # The other half of the same forwarding contract, and the one this
        # class never checked: `degraded` (the verdict handed IN) is pinned
        # by the two tests below, but `on_degrade` -- the callback that
        # carries the verdict back OUT to Agent._note_event_source_degraded
        # -- was not asserted anywhere. It is the single production link
        # that makes the degraded latch connection-scoped rather than
        # watcher-scoped: without it, clipboard_lost/clipboard_became_ready
        # discards the watcher that reached the verdict, the rebuilt one
        # starts undiagnosed, and a mid-connection Wayland flap puts
        # PC-to-Mac sync back on the 30-second detection budget on an
        # installation already known to be broken. Verified by mutation:
        # hardcoding `on_degrade=None` at the forwarding site below left the
        # entire suite green before this assertion existed.
        self.assertIs(
            watcher._on_degrade, on_degrade,
            "the degraded verdict must be able to travel back out to the Agent, "
            "or the latch dies with the watcher that reached it",
        )

    def test_falls_back_to_polling_when_gpaste_unavailable(self):
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)
        self.assertIsInstance(watcher, PollingWatcher)
        self.assertEqual(watcher.interval, 2.5)

    def test_the_polling_fallback_defaults_to_the_interval_degraded_mode_uses(self):
        """One constant, two modes: a machine that never had GPaste and a
        connection whose GPaste went silent must poll at the same rate, or the
        two silently drift the next time one of them is tuned."""
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object())
        self.assertEqual(watcher.interval, DEGRADED_POLL_SECONDS)

    def test_the_fallback_interval_is_the_one_knob_for_both_degraded_modes(self):
        """The previous test only pins the never-had-GPaste branch, and pins the
        default value at that -- so it cannot see the GPaste branch quietly
        keeping its own hardcoded rate. `fallback_interval_seconds` means "how
        fast we poll when signals cannot be relied on", and there are two ways
        to arrive there: GPaste was never available, or its event source was
        diagnosed silent. Tuning the knob has to move both, or make_watcher ends
        up logging one value while polling another."""
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)
        self.assertEqual(
            watcher._degraded_interval, 2.5,
            "the GPaste watcher's degraded rate must come from the same knob",
        )

        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            rebuilt = make_watcher(clipboard=object(), fallback_interval_seconds=2.5,
                                   degraded=True)
        self.assertEqual(
            rebuilt._safety_net.interval, 2.5,
            "and a watcher rebuilt already degraded must come up polling at it",
        )


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


class FlapRecordingWatcher:
    """Records the degraded-latch wiring make_watcher was handed, and can fire
    the diagnosis on demand -- standing in for a real safety-net verdict
    without any thread or subprocess."""

    def __init__(self, degraded=False, on_degrade=None):
        self.degraded = degraded
        self._on_degrade = on_degrade
        self.started_with = None
        self.stopped = False

    def start(self, on_change):
        self.started_with = on_change

    def stop(self):
        self.stopped = True

    def diagnose(self):
        """What GPasteWatcher._observe_tick does once it concludes the event
        source is dead."""
        self.degraded = True
        self._on_degrade()


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

    def test_a_wayland_flap_does_not_re_arm_the_detection_budget(self):
        """clipboard_lost() discards the watcher and clipboard_became_ready()
        builds a fresh one, so a dead-event-source verdict living only on the
        watcher resets on any mid-connection Wayland flap (a logout/login with
        the SSH channel still up): the switch line logs a second time and
        PC->Mac sync drops back to 30-second latency for another full detection
        cycle on an installation already diagnosed.

        This agent is one process per SSH connection (see _clip_state_sent), so
        an Agent-level flag is connection-scoped by construction -- which is
        exactly what "for the rest of the connection" means. And re-enabling
        the gnome-shell extension, the one thing that actually fixes a dead
        source, does not tear down the Wayland session, so a flap is no
        evidence whatsoever that the source recovered."""
        agent = self.build(ready=True)
        built = []

        def factory(clipboard, **kwargs):
            watcher = FlapRecordingWatcher(**kwargs)
            built.append(watcher)
            return watcher

        with mock.patch.object(clipwire_agent, "make_watcher", factory):
            agent.clipboard_became_ready()
            self.assertFalse(
                built[0].degraded, "a fresh connection starts on the detection budget"
            )
            built[0].diagnose()          # the safety net's verdict lands
            agent.clipboard_lost()
            agent.clipboard_became_ready()

        self.assertEqual(len(built), 2, "the flap must have rebuilt the watcher")
        self.assertTrue(
            built[1].degraded,
            "the watcher rebuilt after a flap must start already degraded: the "
            "diagnosis has to outlive the watcher the flap discarded",
        )
        self.assertEqual(
            built[1].started_with, agent._local_change,
            "and it must still be wired to the one observation funnel",
        )


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
            save_clip_state(HASH_A, 1, path=unsaveable_path)

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
            save_clip_state(HASH_A, 1, path=unsaveable_path)

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


class GatedReadClipboard:
    """read() blocks on a gate the test controls, so one _local_change can be
    held inside its clipboard round trip while a second one runs on another
    thread. That is not hypothetical: since the safety-net poll was added, the
    gdbus pump thread and the poll thread BOTH call _local_change, and a
    wl-paste round trip can take up to SUBPROCESS_TIMEOUT=3s."""

    def __init__(self, text, gate):
        self._text = text
        self._gate = gate
        self._lock = threading.Lock()
        self.reads = 0

    def ready(self):
        return True

    def read(self):
        with self._lock:
            self.reads += 1
        self._gate.wait(JOIN_TIMEOUT)
        return self._text

    def write(self, data):
        pass


class TestLocalChangeRaceSafety(unittest.TestCase):
    """_write_clip() always runs on the main thread (driven by run()'s
    single-threaded loop); _local_change() only ever runs on watcher
    threads -- plural since the safety-net poll was added, which is what the
    last test in this class is about. clipboard.read() -- a wl-paste round
    trip -- can take up to SUBPROCESS_TIMEOUT=3s, so a new _write_clip() can
    land on the main thread at any point during that window, not just cleanly
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

    def test_two_watcher_threads_observing_one_copy_send_a_single_clip(self):
        """Before the safety net there was exactly ONE caller of
        _local_change -- the gdbus pump or the fallback poll, never both. Now
        both threads are live at once, and this method structurally cannot
        dedupe against a sibling: it snapshots _last_seen BEFORE its clipboard
        read and only advances it AFTER the send, so two observations of one
        copy that overlap inside that window both find `stale` false, both find
        the text different from a stale `last_seen`, and both send a TYPE_CLIP
        frame -- two frames for one copy, with different observed_at values and
        two competing clip-state writes behind them.

        Serialization must BLOCK, not skip: PollingWatcher's `previous` has
        already advanced past the change it is reporting, so a skipped
        observation is a clip lost until the next change rather than deferred.
        Hence the assertion is one send, not zero."""
        gate = threading.Event()
        self.addCleanup(gate.set)  # never leave the two threads parked
        clipboard = GatedReadClipboard(b"copied once", gate)
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        sent = []
        send_lock = threading.Lock()

        def record(frame_type, payload):
            with send_lock:
                sent.append((frame_type, payload))

        agent.send = record

        pump = threading.Thread(target=agent._local_change)
        poll = threading.Thread(target=agent._local_change)
        pump.start()
        deadline = time.monotonic() + JOIN_TIMEOUT
        while clipboard.reads < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(clipboard.reads, 1, "the first observation must be in flight")

        poll.start()
        # Bounded, and only ever consumed in full when the two observations ARE
        # serialized: unserialized, the second thread reaches its own read
        # within microseconds and this returns immediately.
        deadline = time.monotonic() + 0.02
        while clipboard.reads < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        reads_while_first_in_flight = clipboard.reads

        gate.set()
        pump.join(timeout=JOIN_TIMEOUT)
        poll.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(
            reads_while_first_in_flight, 1,
            "the second observation must not enter its own clipboard read while "
            "the first is still in flight",
        )
        self.assertEqual(
            len(sent), 1,
            "one copy must produce exactly one clip frame, whichever thread "
            "observes it; got %r" % (sent,),
        )
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"copied once")


class TestIncomingClipState(unittest.TestCase):
    """Agent._on_clip_state: resolves an incoming TYPE_CLIP_STATE frame
    against what we hold, per resolve_freshness, and sends only when we
    win. Mirrors HandleFrameTests.swift's "Contract 5" section
    (resolving a peer's clip-state announcement).

    Fix round 1, Finding 1: _on_clip_state only resolves a peer's
    announcement immediately when _clip_state_sent is already True (our own
    side has reconciled and announced at least once this connection) --
    otherwise it stashes the peer state for clipboard_became_ready to
    resolve once that reconciliation has happened, since the store can
    still be stale before it. build() below defaults to simulating that
    precondition directly (already_reconciled=True) so the tests in this
    class, which are about resolve_freshness's OUTCOMES once resolution
    actually happens, are not all forced to drive a full
    clipboard_became_ready() just to reach that state. The stash itself,
    and clipboard_became_ready resolving it, are pinned separately below
    (test_a_clip_state_arriving_before_our_own_reconciliation_is_stashed_not_resolved_immediately
    and
    test_clipboard_became_ready_resolves_a_stashed_peer_clip_state_after_reconciling);
    the full real-dispatch-ordering reproduction lives in
    test_mainloop.py::TestClipStateOrderingAcrossRealDispatch, since only
    driving the REAL run() loop can prove the ordering bug this precondition
    exists to close (calling handlers by hand in a chosen order is exactly
    what let it through undetected the first time)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, clipboard=None, already_reconciled=True):
        agent = Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(),
            clipboard=clipboard if clipboard is not None else QueueClipboard(ready=True),
            clip_state_path=self.clip_state_path,
        )
        agent._clip_state_sent = already_reconciled
        return agent

    def test_losing_clip_state_with_peer_fresher_produces_no_send(self):
        """resolve_freshness's waitForPeer outcome: the peer is fresher, so
        we wait. Conflating this with doNothing would be harmless here, but
        the point of a resend would be to CLOBBER a fresher peer -- exactly
        the defect this whole design exists to prevent."""
        save_clip_state(HASH_A, 5, path=self.clip_state_path)
        # Real content on the clipboard, or a wrongly-resolved SEND_MINE
        # would still send nothing (its first guard is a non-empty read)
        # and this assertion would hold for the wrong reason. Mirrors
        # HandleFrameTests.testLosingClipStateWithPeerFresherProducesNoSend.
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"something to wrongly send")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 9))  # peer fresher

        self.assertEqual(sent, [], "the peer is fresher -- we wait, we do not resend")

    def test_agreeing_clip_state_with_equal_hashes_produces_no_send(self):
        """resolve_freshness's doNothing outcome via equal hashes: "hashes
        equal" must mean "we agree", not "resend" -- conflating it with
        sendMine would ping-pong the same content back and forth forever."""
        save_clip_state(HASH_A, 5, path=self.clip_state_path)
        # See the test above: without real content a wrongly-resolved
        # SEND_MINE sends nothing anyway, and this would pass regardless.
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"something to wrongly send")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_A, 999))  # same hash

        self.assertEqual(sent, [], "hashes equal means we agree, not resend")

    def test_winning_clip_state_sends_exactly_one_clip_frame_carrying_our_stored_timestamp(self):
        """resolve_freshness's sendMine outcome: a peer with no clipboard at
        all (also the fix for v1's documented loss of Mac copies made while
        the PC was off). The resulting clip must carry OUR stored ts, not
        now -- resending with now would perpetually refresh its age and let
        it win every future reconciliation regardless of what happens next."""
        save_clip_state(HASH_A, 777, path=self.clip_state_path)
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

    def test_winning_clip_state_updates_last_seen_to_what_was_just_sent(self):
        """_local_change sets _last_seen = text after a successful send,
        deliberately: _last_seen is "what the peer already holds, for as
        long as neither side has genuinely changed it" (see its own doc
        comment in _local_change). Telling the peer "I hold X" through THIS
        path is no different -- after this send, the peer does (soon) hold
        X too, exactly as after a _local_change send. Without this update, a
        later spurious GPaste signal (a history deletion emits Update too,
        not only a real change) would see clipboard.read() == X but
        last_seen still stale or None, wrongly conclude a genuine local
        change happened, and resend X to the peer -- wastefully at best,
        and destructively if the Mac had meanwhile been changed to some Y:
        the Mac's handleFrame applies ANY incoming .clip frame
        unconditionally, so a stale resend of X arriving after the user's
        own Y would silently clobber it.

        This is the Swift reference's own .clipState case NOT doing this
        either -- but harmlessly there, since the Mac's PasteboardWatcher is
        changeCount-driven and never fires on a non-change. The PC's GPaste
        watcher does fire on non-changes (that is the entire reason
        _last_seen exists on this side at all), so the omission that is
        inert on the Mac is a real defect here."""
        save_clip_state(HASH_A, 777, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"current clip text")
        agent = self.build(clipboard=clipboard)
        agent.send = lambda t, p: None

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))  # peer empty -> sendMine

        self.assertEqual(
            agent._last_seen, b"current clip text",
            "_last_seen must advance to what was just sent, exactly as "
            "_local_change's own send path already does",
        )

    def test_winning_clip_state_prefers_a_just_applied_clip_over_a_racy_reread(self):
        """The same class of bug clipboard_became_ready's announce step was
        fixed for, one door over: a TYPE_CLIP frame applied via _write_clip
        (the immediate _on_clip path, once READY) followed closely by a
        TYPE_CLIP_STATE frame that wins the reconciliation -- both
        plausible on the same connection, e.g. the Mac's watcher pushing a
        fresh local change around the time of its own one-time clip-state
        announcement. _write_clip already wrote to and correctly persisted
        state for that applied clip; if _on_clip_state instead re-reads the
        clipboard fresh to find the TEXT to send, that read can race
        wl-copy's asynchronous, detached write (see _write_clip's own
        comment) and see stale content -- sending WRONG text stamped with
        the CORRECT mine[1] timestamp, which looks like a valid, fresh
        reconciliation response to the peer.

        _last_seen already holds the applied clip's exact text (_write_clip
        sets it before spawning wl-copy) -- and it is verified against
        mine's hash before being trusted, so a stale/unrelated _last_seen
        (e.g. clipboard_became_ready's own connect-time seed) still falls
        back to a live read exactly as before.

        QueueClipboard is the right double here, unmodified: its write()
        already never affects what a subsequently-queued read() returns --
        precisely the "write and read are decoupled in time" shape of the
        real asynchronous wl-copy, achieved here simply by not queuing the
        applied text as a read value.

        already_reconciled=False, unlike every other test in this class:
        this is the one test that drives the REAL clipboard_became_ready()
        transition (not a simulated shortcut) before the clip-state frame
        arrives -- it is the "clip-state after readiness" half of the
        ordering space, deliberately kept distinct from
        TestClipStateOrderingAcrossRealDispatch's "clip-state BEFORE
        readiness" reproduction in test_mainloop.py."""
        applied_text = b"the peer's own recently applied clip"
        applied_ts = 555.0
        clipboard = QueueClipboard(ready=True)
        agent = self.build(clipboard=clipboard, already_reconciled=False)

        # Get into READY phase first (an empty queue -> the connect-time
        # seed and the initial announce both read None, which is fine and
        # irrelevant to what this test actually checks).
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        agent.on_frame(TYPE_CLIP, encode_clip_payload(applied_ts, applied_text))
        self.assertEqual(
            load_clip_state(path=self.clip_state_path), (sha256_hex(applied_text), applied_ts),
            "test setup must actually apply and persist the clip, or this test proves nothing",
        )

        # Queued for _on_clip_state's OWN read, if it takes the (buggy)
        # fresh-read path -- stale content that predates the clip just
        # applied above, modeling wl-copy not yet having taken over.
        clipboard.queue_read(b"stale content predating this connection")

        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))  # peer empty -> sendMine

        self.assertEqual(len(sent), 1)
        ts, text = decode_clip_payload(sent[0][1])
        self.assertEqual(ts, applied_ts)
        self.assertEqual(
            text, applied_text,
            "must send the just-applied clip's own known text, not a stale "
            "clipboard.read() racing wl-copy's asynchronous write",
        )

    def test_winning_clip_state_with_content_at_exactly_the_cap_produces_no_send(self):
        """Unlike _local_change's own send path, this branch reads the live
        clipboard independently and, without this bound, winning a
        reconciliation over content at or beyond the cap would build a
        frame that exceeds MAX_PAYLOAD_BYTES once wrapped -- the peer's
        decode_frame rejects that as oversized and drops the whole channel."""
        save_clip_state(HASH_A, 777, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"x" * MAX_PAYLOAD_BYTES)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0))

        self.assertEqual(sent, [], "content of exactly the cap would encode to a frame 8 bytes over it")

    def test_winning_clip_state_with_content_leaving_exact_room_for_the_timestamp_prefix_still_sends(self):
        save_clip_state(HASH_A, 777, path=self.clip_state_path)
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
        save_clip_state(HASH_A, 777, path=self.clip_state_path)
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
        oversized = ('{"sha256": "%s", "ts": 1' % HASH_A).encode() + b"0" * 400 + b"}"

        with self.assertRaises(ClipStateError):
            agent.on_frame(TYPE_CLIP_STATE, oversized)

    # MARK: - Fix round 1, Finding 1: stash-before-reconciliation
    #
    # Fast, direct pins of the stash mechanism itself, complementing
    # test_mainloop.py::TestClipStateOrderingAcrossRealDispatch's slower but
    # higher-fidelity end-to-end reproduction (which drives the REAL run()
    # loop, per Finding 3 -- calling handlers by hand in a chosen order,
    # as these two tests do, is exactly the blind spot that let the bug
    # through the first time, so it cannot be the ONLY coverage).

    def test_a_clip_state_arriving_before_our_own_reconciliation_is_stashed_not_resolved_immediately(self):
        """not self._clip_state_sent -- our own side has not yet reconciled
        and announced this connection -- must stash the peer's state rather
        than resolve it against a store that can still be stale (content
        predating this process, the ordinary case: ANY reconnect, not only
        a reboot). Resolving here anyway is exactly how a peer's
        announcement that happens to still match the stale value would be
        judged doNothing and never reconsidered -- v1's silent loss.

        Our own local store's content ("aa", 5) is deliberately irrelevant
        here and never even loaded: this pins that the STASHED value is the
        peer's own decoded announcement ("bb", 42) -- not our local state,
        and not silently dropped -- which is a distinct assertion from
        "nothing was sent"."""
        save_clip_state(HASH_A, 5, path=self.clip_state_path)
        agent = self.build(already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 42))

        self.assertEqual(sent, [], "must not resolve before our own side has reconciled")
        self.assertEqual(
            agent._pending_peer_clip_state, (HASH_B, 42.0),
            "the peer's own decoded state must be stashed, not silently dropped",
        )

    def test_clipboard_became_ready_resolves_a_stashed_peer_clip_state_after_reconciling(self):
        """The other half: once clipboard_became_ready has reconciled (and
        announced) our own side, a clip-state stashed before that point
        must actually be resolved -- against the NOW-current store, not a
        stale one. This is the direct-call twin of
        TestClipStateOrderingAcrossRealDispatch's full run()-loop
        reproduction in test_mainloop.py; both exist because neither alone
        is enough evidence (see this class's own docstring)."""
        clipboard = QueueClipboard(ready=True)
        # Two reads happen inside clipboard_became_ready(): the connect-time
        # seed, and a second read from announce_clip_state's own
        # resolve_current_clip_state call -- see
        # TestEchoBookkeeping.test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send
        # for the same double-read shape.
        clipboard.queue_read(b"B")  # the seed read
        clipboard.queue_read(b"B")  # the announce step's own read
        agent = self.build(clipboard=clipboard, already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # Stale on disk: both sides last synced on "A" a long time ago.
        save_clip_state(sha256_hex(b"A"), 100.0, path=self.clip_state_path)
        # The peer's own announcement, also still describing "A" -- it
        # has not changed either.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(sha256_hex(b"A"), 100.0))
        self.assertEqual(sent, [], "must not resolve yet -- stashed")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        self.assertIsNone(
            agent._pending_peer_clip_state, "the stash must be cleared once resolved"
        )
        clip_frames = [f for f in sent if f[0] == TYPE_CLIP]
        self.assertEqual(
            len(clip_frames), 1,
            "the reconciled store now says B, fresher than the peer's stale "
            "A announcement -- B must be sent, not silently dropped",
        )
        self.assertEqual(decode_clip_payload(clip_frames[0][1])[1], b"B")

    def test_a_stashed_clip_state_resolves_against_the_pair_just_computed(self):
        """_resolve_clip_state re-loaded the store even when reached from
        clipboard_became_ready, which had JUST computed the authoritative
        pair one line earlier and announced it to this very peer.

        That only diverges when the store cannot be read back -- every
        save_clip_state call site swallows its failure, so an unwritable
        state directory is silent and this is the shape it takes. The
        fallback then re-derives from a FRESH clipboard read and stamps
        time.time(), so the value we reconcile with is not the value we
        just announced to the peer: an age we invented, inflated past the
        one on the wire, able to win a comparison it should have lost.

        Pinned two ways: the clipboard must not be read a third time at
        all (the seed and the announce step are the only two legitimate
        reads), and the clip we send after winning must carry the exact
        timestamp we announced. The queued third read is what a
        re-derivation would consume, and it returns DIFFERENT content so a
        re-derivation cannot accidentally agree."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable = os.path.join(blocker, "clip-state.json")
        with self.assertRaises(OSError):
            save_clip_state(HASH_A, 1, path=unsaveable)

        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"A")          # the connect-time seed
        clipboard.queue_read(b"A")          # announce_clip_state's own read
        clipboard.queue_read(b"DIFFERENT")  # only a re-derivation consumes this
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                      clipboard=clipboard, clip_state_path=unsaveable)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # The peer holds something else and is OLDER, so we win and send.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1.0))
        self.assertEqual(sent, [], "must not resolve before our own side has reconciled")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        self.assertEqual(
            len(clipboard._queue), 1,
            "the store's own pair was already in hand -- re-reading the clipboard "
            "to rebuild it is what invents a timestamp nobody announced",
        )
        announced = [f for f in sent if f[0] == TYPE_CLIP_STATE]
        clips = [f for f in sent if f[0] == TYPE_CLIP]
        self.assertEqual(len(announced), 1)
        self.assertEqual(len(clips), 1, "we are fresher than the peer, so we send")
        self.assertEqual(decode_clip_payload(clips[0][1])[1], b"A")
        self.assertEqual(
            decode_clip_payload(clips[0][1])[0], decode_clip_state(announced[0][1])[1],
            "the clip we send must carry the timestamp we just announced to this "
            "same peer, not one re-derived from a later clock reading",
        )

    def test_every_reconciliation_outcome_is_logged(self):
        """No reconciliation decision was logged at all, on either side.
        Acceptance item 2 -- "the PC's copy must win, and the conflict must
        appear in the log" -- is unpassable without this, and the design's
        one accepted trade-off ("the side whose agent was born more recently
        wins") is justified on the grounds of being visible in the log
        rather than mysterious, which was never implemented.

        The decision word itself is the shared vocabulary: SEND_MINE /
        WAIT_FOR_PEER / DO_NOTHING are the exact strings Swift's
        FreshnessDecision raw values use, so the two sides' lines are
        byte-identical for free -- the same convention the frame-cap and
        skew lines already follow. Both sides' lines land in the SAME file
        in production: Channel.attempt pipes this agent's stderr into the
        Mac's log with a `remote: ` prefix."""
        cases = [
            # (stored ts, peer state, expected decision)
            (5, (HASH_B, 9), "waitForPeer"),   # peer fresher
            (5, (HASH_A, 999), "doNothing"),   # same hash
            (777, (None, 0), "sendMine"),      # peer has nothing
        ]
        for stored_ts, peer, expected in cases:
            with self.subTest(expected):
                save_clip_state(HASH_A, stored_ts, path=self.clip_state_path)
                clipboard = QueueClipboard(ready=True)
                clipboard.queue_read(b"whatever we hold")
                agent = self.build(clipboard=clipboard)
                agent.send = lambda t, p: None

                original_log = clipwire_agent.log
                log_lines = []
                clipwire_agent.log = log_lines.append
                try:
                    agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(*peer))
                finally:
                    clipwire_agent.log = original_log

                self.assertIn("reconciled with the peer: %s" % expected, log_lines)

    def test_an_applied_pending_clip_supersedes_the_peers_stashed_announcement(self):
        """The reboot flow (acceptance item 5), where the stash is stale by
        construction. The Mac announces its clip-state, then copies again
        and sends the newer clip -- both while this side is still
        PHASE_PENDING, so the announcement is stashed and the clip queued.
        clipboard_became_ready then APPLIES the queued clip and only
        afterwards drains the stash, so it resolves the Mac's superseded
        announcement (ts 1000) against the clip it just applied (ts 3000),
        gets SEND_MINE, and sends the Mac its own clip straight back.

        A clip frame from a peer is strictly NEWER information than that
        same peer's earlier announcement -- the announcement describes what
        the peer held before it sent the clip -- so once the clip has been
        applied there is nothing left in the stash worth resolving.

        The harm is bounded (noteWrittenLocally is armed before the write,
        EchoGuard suppresses, content converges), which is exactly why it
        needs a test: nothing about the end state is wrong, so only the
        redundant frame itself is observable."""
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"whatever the PC held")  # the connect-time seed
        agent = self.build(clipboard=clipboard, already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # The Mac's announcement, describing what IT held at the time.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(sha256_hex(b"the mac's older clip"), 1000.0))
        # ... then the Mac copies something else and sends it. Still pending
        # here, so it is queued rather than applied.
        agent.on_frame(TYPE_CLIP, encode_clip_payload(3000.0, b"the mac's newer clip"))
        self.assertEqual(sent, [], "nothing may go out before the clipboard is ready")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        self.assertEqual(
            [f for f in sent if f[0] == TYPE_CLIP], [],
            "the Mac's own clip must not be sent back to the Mac: its earlier "
            "announcement was superseded by the very clip we just applied",
        )
        self.assertEqual(
            len([f for f in sent if f[0] == TYPE_CLIP_STATE]), 1,
            "our own one-shot announcement must still go out",
        )
        self.assertIsNone(
            agent._pending_peer_clip_state,
            "a superseded stash must be dropped, not left for a later drain",
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
            "import ctypes",
            "import signal",
            "PR_SET_PDEATHSIG = 1",
            "def _load_libc",
            "_LIBC = _load_libc()",
            "def _pdeathsig_preexec",
            "import traceback",
            "def _handle_observer_error",
            "def _start_observer",
            "class GPasteWatcher",
            "class PollingWatcher",
            "def parse_gpaste_line",
            "SAFETY_NET_POLL_SECONDS = 30.0",
            "DEGRADED_POLL_SECONDS = 1.0",
            "TIMESTAMP_BYTES = 8",
            "class ClipPayloadError",
            "def encode_clip_payload",
            "def decode_clip_payload",
            "TYPE_CLIP_STATE = 0x02",
            "class ClipStateError",
            "def encode_clip_state",
            "def _is_sha256_hex",
            "def decode_clip_state",
            "def resolve_freshness",
            'SEND_MINE = "sendMine"',
            'WAIT_FOR_PEER = "waitForPeer"',
            'DO_NOTHING = "doNothing"',
            "def _xdg_dir",
            "def clip_state_path",
            "def load_clip_state",
            "_clip_state_write_lock = threading.Lock()",
            "def save_clip_state",
            "def resolve_startup_state",
            "def sha256_hex",
            "def resolve_current_clip_state",
            "def announce_clip_state",
            "SKEW_WARN_SECONDS = 5.0",
            "def skew_log_line",
        ):
            with self.subTest(needle=needle):
                self.assertLess(
                    source.index(needle), guard_index,
                    "%r must be defined before the __main__ guard, or it "
                    "never executes when the agent is run for real" % needle,
                )


if __name__ == "__main__":
    unittest.main()
