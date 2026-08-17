# agent/tests/test_lock_monitor.py
"""LockMonitor (v3.5 spec §3.2): subscribe first, then read; re-resolve on
session churn; fail-open is logged once and answers None, never False-as-a-
default."""
import subprocess
import threading
import time
import unittest
from unittest import mock

from agent_under_test import (
    LockMonitor,
    parse_logind_monitor_line,
)
import clipwire_agent

PATH = "/org/freedesktop/login1/session/_32"
LOCKED_LINE = (PATH + ": org.freedesktop.DBus.Properties.PropertiesChanged "
               "('org.freedesktop.login1.Session', {'LockedHint': <true>}, @as [])")
UNLOCKED_LINE = LOCKED_LINE.replace("<true>", "<false>")
NEW_LINE = "/org/freedesktop/login1: org.freedesktop.login1.Manager.SessionNew ('249', ...)"

JOIN_TIMEOUT = 2  # generous relative to the millisecond-scale waits below


class TestParse(unittest.TestCase):
    def test_locked(self):
        self.assertEqual(parse_logind_monitor_line(LOCKED_LINE, PATH), "locked")

    def test_unlocked(self):
        self.assertEqual(parse_logind_monitor_line(UNLOCKED_LINE, PATH), "unlocked")

    def test_other_sessions_lockedhint_is_ignored(self):
        other = LOCKED_LINE.replace("_32", "_99")
        self.assertIsNone(parse_logind_monitor_line(other, PATH))

    def test_session_churn_asks_for_reresolve(self):
        self.assertEqual(parse_logind_monitor_line(NEW_LINE, PATH), "resolve")

    def test_noise_is_none(self):
        self.assertIsNone(parse_logind_monitor_line("/foo: bar.Baz ()", PATH))

    def test_a_session_path_that_is_a_prefix_of_another_does_not_false_positive(self):
        """Review finding: logind's bus_label_escape can make one session's
        path a strict prefix of another's -- session "1" -> .../_31,
        session "12" -> .../_312 -- and "_31" IS a substring of "_312", so a
        bare `session_path in line` lets a _312 PropertiesChanged line flip
        a monitor watching _31. The existing _32/_99 test cannot catch this
        (neither is a prefix of the other); this pins the prefix case
        specifically."""
        short_path = "/org/freedesktop/login1/session/_31"
        long_path = "/org/freedesktop/login1/session/_312"
        line_for_the_other_session = (
            long_path + ": org.freedesktop.DBus.Properties.PropertiesChanged "
            "('org.freedesktop.login1.Session', {'LockedHint': <true>}, @as [])")
        self.assertIsNone(
            parse_logind_monitor_line(line_for_the_other_session, short_path))


class _ScriptedPump:
    """Stands in for the gdbus monitor process: .stdout is an iterator the
    reader thread consumes; terminate() ends it."""
    def __init__(self, lines, released):
        self._released = released
        def gen():
            released.wait(5)
            yield from lines
        self.stdout = gen()
    def terminate(self):
        self._released.set()


def _unreachable_run(argv, **kwargs):
    """The `run` double for every test below: resolve_graphical_session and
    session_locked_hint are always patched at the module level instead, so
    LockMonitor must never fall through to calling `run` itself -- it only
    ever threads `run=self._run` INTO those two functions."""
    raise AssertionError("resolver is patched; run must not be called")


class _RecordingStop:
    """Drop-in for LockMonitor._stop: records the delay the respawn loop
    asks to wait for and returns as though it had already elapsed, rather
    than actually sleeping. Same idiom and the same reason as
    test_watcher_polling.py's RecordingStop: patching the backoff SECONDS
    constants down would prove the number computed, never that it is the
    number the loop actually paced itself on. Stops the loop once more than
    `ticks` waits have been recorded."""
    def __init__(self, ticks):
        self._event = threading.Event()
        self._ticks = ticks
        self.waits = []
    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self._event.is_set() or len(self.waits) > self._ticks
    def set(self):
        self._event.set()
    def is_set(self):
        return self._event.is_set()


class _FakeClock:
    """Stands in for time.monotonic: a plain float the test moves by hand,
    so state()'s `since` can be pinned against a known value instead of
    merely "some real number came out". Callable with no arguments, like
    time.monotonic itself -- that is the only shape LockMonitor asks of
    `now`."""
    def __init__(self, start=1000.0):
        self.t = start
    def __call__(self):
        return self.t
    def tick(self, by):
        self.t += by
        return self.t


class TestMonitor(unittest.TestCase):
    def _monitor(self, lines, resolved=PATH, hint=False, now=time.monotonic):
        released = threading.Event()
        order = []
        def popen(argv, **kwargs):
            order.append("subscribe")
            return _ScriptedPump(lines, released)
        def run(argv, **kwargs):  # ListSessions/Get double via resolver patching below
            raise AssertionError("resolver is patched; run must not be called")
        monitor = LockMonitor(run=run, popen=popen, now=now)
        with mock.patch("clipwire_agent.resolve_graphical_session",
                        return_value=resolved), \
             mock.patch("clipwire_agent.session_locked_hint",
                        side_effect=lambda *a, **k: order.append("get") or hint):
            monitor.start()
        return monitor, order, released

    def test_subscribes_before_the_initial_get(self):
        monitor, order, released = self._monitor([], hint=False)
        self.addCleanup(monitor.stop)
        self.assertEqual(order[:2], ["subscribe", "get"])
        self.assertIs(monitor.locked(), False)

    def test_a_locked_line_flips_the_cache(self):
        monitor, order, released = self._monitor([LOCKED_LINE], hint=False)
        self.addCleanup(monitor.stop)
        released.set()
        deadline = threading.Event()
        for _ in range(50):
            if monitor.locked() is True:
                break
            deadline.wait(0.02)
        self.assertIs(monitor.locked(), True)

    def test_failed_resolution_answers_none_and_logs_once(self):
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)):
            monitor, order, released = self._monitor([], resolved=None)
            self.addCleanup(monitor.stop)
            self.assertIsNone(monitor.locked())
        gate_lines = [m for m in logged if "lock gate inactive" in m]
        self.assertEqual(len(gate_lines), 1)

    def test_state_stamps_the_bootstrap_transition_with_the_injected_clock(self):
        """state()/`now` are the interface Task 5 freezes state() from --
        pin them directly rather than leaving them exercised only as a
        side effect of locked() assertions elsewhere in this file."""
        clock = _FakeClock(1000.0)
        monitor, order, released = self._monitor([], hint=True, now=clock)
        self.addCleanup(monitor.stop)
        self.assertEqual(monitor.state(), (True, 1000.0))

    def test_state_stamps_a_flip_with_the_clocks_value_at_that_moment(self):
        clock = _FakeClock(1000.0)
        monitor, order, released = self._monitor([LOCKED_LINE], hint=False, now=clock)
        self.addCleanup(monitor.stop)
        self.assertEqual(monitor.state(), (False, 1000.0))
        clock.tick(42.0)  # advance BEFORE the reader thread processes the flip
        released.set()
        deadline = threading.Event()
        for _ in range(50):
            if monitor.locked() is True:
                break
            deadline.wait(0.02)
        self.assertEqual(monitor.state(), (True, 1042.0))

    def test_state_does_not_restamp_on_a_repeated_same_value(self):
        """The debounce and stretch duration Task 5's LockGate computes are
        both measured from `since` -- if a duplicate line (or a duplicate
        _set_locked call from any path) moved it, every hold/stretch
        calculation downstream would be wrong. Calls _set_locked directly:
        this pins ITS OWN guard deterministically, independent of pump/
        thread timing already covered elsewhere in this file."""
        clock = _FakeClock(1000.0)
        monitor, order, released = self._monitor([], hint=True, now=clock)
        self.addCleanup(monitor.stop)
        self.assertEqual(monitor.state(), (True, 1000.0))
        clock.tick(500.0)
        monitor._set_locked(True)
        self.assertEqual(monitor.state(), (True, 1000.0),
                         "a repeated same value must not move `since`")

    def test_a_second_separate_fail_open_stretch_logs_again(self):
        """spec §6: 'a relogin can pass through a two-candidate moment more
        than once, and the second entry must not be mute' -- a resolver
        that fails, then succeeds, then fails again a second time must log
        TWICE, not once; a persistent failure (the test above) logs once,
        so this is the other half of the same rule."""
        released = threading.Event()
        logged = []
        resolved_queue = [None, PATH, None]
        def resolve(run=None):
            return resolved_queue.pop(0)
        def popen(argv, **kwargs):
            return _ScriptedPump([NEW_LINE], released)
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   side_effect=resolve)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", return_value=False)
        log_patch = mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m))
        resolve_patch.start()
        hint_patch.start()
        log_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        self.addCleanup(log_patch.stop)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        self.addCleanup(monitor.stop)
        monitor.start()
        self.assertIsNone(monitor.locked())
        released.set()
        deadline = threading.Event()
        for _ in range(50):
            if monitor.locked() is False:
                break
            deadline.wait(0.02)
        self.assertIs(monitor.locked(), False)
        # End the pump before driving a third resolve directly -- isolates
        # the re-arm assertion from the pump's own EOF/backoff timing
        # rather than scripting a second SessionNew line to force it.
        monitor.stop()
        monitor._reresolve()
        self.assertIsNone(monitor.locked())
        gate_lines = [m for m in logged if "lock gate inactive" in m]
        self.assertEqual(len(gate_lines), 2)

    def test_a_resolved_sessions_unreadable_hint_logs_once_and_rearms(self):
        """spec §6 final bullet: the resolver's "no unique graphical
        session" line is not the only fail-open door -- a LockedHint Get
        that fails on a session that DID resolve is a second, separate one
        (test_failed_resolution_answers_none_and_logs_once pins the first;
        these are two different doors and each logs its own transition).
        Get failing twice running logs once; a real answer in between
        re-arms it, so a THIRD failure logs again rather than staying mute
        forever the way a once-per-lifetime line would."""
        logged = []
        hint_queue = [None, None, True, None]
        def hint(path, run=None):
            return hint_queue.pop(0)
        released = threading.Event()
        def popen(argv, **kwargs):
            return _ScriptedPump([], released)
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   return_value=PATH)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", side_effect=hint)
        log_patch = mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m))
        resolve_patch.start()
        hint_patch.start()
        log_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        self.addCleanup(log_patch.stop)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        self.addCleanup(monitor.stop)
        monitor.start()                      # hint_queue[0] -> None: unreadable
        self.assertIsNone(monitor.locked())
        # End the pump before driving _reresolve directly -- same reasoning
        # as test_a_second_separate_fail_open_stretch_logs_again: isolates
        # this from the pump's own EOF/backoff timing.
        monitor.stop()
        monitor._reresolve()                 # hint_queue[1] -> None: still unreadable, mute
        self.assertIsNone(monitor.locked())
        monitor._reresolve()                 # hint_queue[2] -> True: answers, re-arms
        self.assertIs(monitor.locked(), True)
        monitor._reresolve()                 # hint_queue[3] -> None: unreadable again
        self.assertIsNone(monitor.locked())
        unreadable_lines = [m for m in logged if "lock state unreadable" in m]
        self.assertEqual(len(unreadable_lines), 2)
        # The resolver's own door must stay silent throughout: this session
        # resolved every time, only its Get ever failed.
        self.assertEqual([m for m in logged if "no unique graphical session" in m], [])

    def test_a_parsed_transition_rearms_the_unreadable_hint_door_too(self):
        """The re-arm must not be scoped to Gets alone: a PropertiesChanged
        line parsing on the pump thread is also "a later read or parse"
        succeeding (spec §6), and _set_locked is the one funnel both a Get
        and a parsed line go through. Without this, an unreadable Get
        followed by a real LOCKED_LINE and then another unreadable Get
        would wrongly stay mute on the second failure."""
        logged = []
        hint_queue = [None, None]
        def hint(path, run=None):
            return hint_queue.pop(0)
        with mock.patch("clipwire_agent.resolve_graphical_session", return_value=PATH), \
             mock.patch("clipwire_agent.session_locked_hint", side_effect=hint), \
             mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)):
            monitor = LockMonitor(run=_unreachable_run,
                                  popen=lambda argv, **k: _ScriptedPump([], threading.Event()))
            self.addCleanup(monitor.stop)
            monitor.start()                  # hint_queue[0] -> None: unreadable, logged
            self.assertIsNone(monitor.locked())
            monitor.stop()
            monitor._set_locked(True)        # stands in for a parsed LOCKED_LINE
            monitor._reresolve()             # hint_queue[1] -> None: unreadable AGAIN
            self.assertIsNone(monitor.locked())
        unreadable_lines = [m for m in logged if "lock state unreadable" in m]
        self.assertEqual(len(unreadable_lines), 2)

    def test_stop_terminates_the_subprocess_and_ends_the_reader_thread(self):
        monitor, order, released = self._monitor([])
        monitor.stop()
        monitor._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(
            monitor._thread.is_alive(),
            "a daemon thread that keeps reading a dead pipe is a leak",
        )

    def test_the_pump_is_spawned_with_the_pdeathsig_preexec(self):
        """Style of test_watcher_gpaste_signals.py's own pdeathsig pin --
        also pins the transport shape wholesale (spec §3.2 Pump bullet):
        --system, no --object-path, and deliberately NO clipboard_env(),
        since this is the system bus, not the session one GPaste uses."""
        released = threading.Event()
        calls = []
        def popen(argv, **kwargs):
            calls.append((argv, kwargs))
            return _ScriptedPump([], released)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        with mock.patch("clipwire_agent.resolve_graphical_session", return_value=PATH), \
             mock.patch("clipwire_agent.session_locked_hint", return_value=False):
            monitor.start()
        self.addCleanup(monitor.stop)
        argv, kwargs = calls[0]
        self.assertEqual(
            argv, ["gdbus", "monitor", "--system", "--dest", clipwire_agent.LOGIND_BUS_NAME])
        self.assertEqual(kwargs.get("stdout"), subprocess.PIPE)
        self.assertEqual(kwargs.get("stderr"), subprocess.DEVNULL)
        self.assertTrue(kwargs.get("text"))
        self.assertNotIn("env", kwargs, "the system bus needs no clipboard_env() override")
        self.assertIs(kwargs.get("preexec_fn"), clipwire_agent._pdeathsig_preexec)

    def test_session_churn_reresolves_and_swaps_the_watched_path(self):
        """spec §3.2 step 3: a SessionNew/SessionRemoved line re-runs the
        resolver AND the initial Get, and the cache ends up answering for
        the NEW session, not the old one."""
        new_path = "/org/freedesktop/login1/session/_99"
        released = threading.Event()
        resolve_calls = []
        hint_calls = []
        resolved_queue = [PATH, new_path]
        hint_queue = [False, True]
        def resolve(run=None):
            path = resolved_queue.pop(0)
            resolve_calls.append(path)
            return path
        def hint(path, run=None):
            hint_calls.append(path)
            return hint_queue.pop(0)
        def popen(argv, **kwargs):
            return _ScriptedPump([NEW_LINE], released)
        # .start()/.stop() rather than a `with` block, and addCleanup in
        # THIS order: LIFO means monitor.stop() (which ends the reader
        # thread) runs before the patches are removed. A `with` scoped
        # around just monitor.start() would restore the REAL
        # resolve_graphical_session while the reader thread is still
        # asynchronously re-resolving off of NEW_LINE, and the real one
        # calls the forbidden `run` double.
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   side_effect=resolve)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", side_effect=hint)
        resolve_patch.start()
        hint_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        self.addCleanup(monitor.stop)
        monitor.start()
        self.assertIs(monitor.locked(), False)
        released.set()
        deadline = threading.Event()
        for _ in range(50):
            if monitor.locked() is True:
                break
            deadline.wait(0.02)
        self.assertIs(monitor.locked(), True)
        self.assertEqual(resolve_calls, [PATH, new_path])
        self.assertEqual(hint_calls, [PATH, new_path])

    def test_respawn_backoff_doubles_caps_and_resets_on_a_parsed_line(self):
        """spec §3.2.3: pump death self-heals by re-spawning and
        re-resolving, backoff 1s doubling to a 30s cap, reset the moment a
        line parses to something real again -- and that must be the value
        the loop actually waits on, not merely a value it computes (see
        _RecordingStop)."""
        immediate = threading.Event()
        immediate.set()
        popen_calls = []
        def popen(argv, **kwargs):
            popen_calls.append(argv)
            lines = [LOCKED_LINE] if len(popen_calls) == 7 else []
            return _ScriptedPump(lines, immediate)
        # LIFO addCleanup ordering, same reasoning as the churn test above:
        # monitor.stop() must run before the patches are removed, in case
        # the join below ever raced a respawn (it shouldn't -- _RecordingStop
        # never blocks -- but the ordering costs nothing and removes the
        # assumption).
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   return_value=PATH)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", return_value=False)
        resolve_patch.start()
        hint_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        stop = _RecordingStop(6)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        monitor._stop = stop
        self.addCleanup(monitor.stop)
        monitor.start()
        monitor._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(monitor._thread.is_alive(),
                         "the free-running respawn loop must have finished its budget")
        self.assertEqual(stop.waits, [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 1.0])
        self.assertEqual(len(popen_calls), 7)

    def test_an_unexpected_exception_falls_open_and_keeps_retrying(self):
        """Review finding: this thread is the unlock's SOLE observer
        (unlike GPasteWatcher's pump), so an unanticipated exception --
        reproduced here as a respawn's Popen raising FileNotFoundError --
        must not kill it and freeze the cache at its last value, including
        True. Falls open (locked() -> None) and keeps retrying via the
        SAME backoff/respawn loop rather than parking: a standing failure
        (this test never lets it succeed again) just keeps re-raising and
        re-falling-open behind the existing backoff, proven here by the
        thread staying alive and popen being called repeatedly, not by it
        ever recovering."""
        immediate = threading.Event()
        immediate.set()
        calls = []
        def popen(argv, **kwargs):
            calls.append(argv)
            if len(calls) >= 2:
                raise FileNotFoundError("gdbus")
            return _ScriptedPump([], immediate)
        logged = []
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   return_value=PATH)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", return_value=True)
        log_patch = mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m))
        resolve_patch.start()
        hint_patch.start()
        log_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        self.addCleanup(log_patch.stop)
        stop = _RecordingStop(3)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        monitor._stop = stop
        self.addCleanup(monitor.stop)
        monitor.start()
        # No assertion on locked() here: with every pump immediate-EOF and
        # _RecordingStop never blocking, the reader thread can race through
        # its whole fall-open-and-retry sequence before this thread's next
        # line runs -- the initial True is real (the same synchronous Get
        # every other test in this file pins) but not OBSERVABLE without a
        # synchronization point, so this test proves its claim only from
        # the settled state after join(), below.
        monitor._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(monitor._thread.is_alive(),
                         "an unexpected exception must not kill the sole observer")
        self.assertIsNone(monitor.locked(),
                          "must fall open, not freeze at the last value (here, True)")
        self.assertEqual(len(calls), 4, "the loop must keep retrying (respawning), not park")
        exception_lines = [m for m in logged if "FileNotFoundError" in m]
        self.assertEqual(len(exception_lines), 3,
                         "one log line naming the exception class per failed attempt")

    def test_a_popen_that_cannot_start_falls_open_without_raising(self):
        """F2 (final-review finding): start()'s OWN self._process =
        self._spawn() runs on the CALLER's thread -- main(), before
        send_hello -- with no reader thread yet built to catch anything,
        unlike the already-covered respawn inside _pump() (test_an_
        unexpected_exception_falls_open_and_keeps_retrying), which is
        already wrapped. A missing/unstartable gdbus (FileNotFoundError
        here, the shape Popen actually raises for a binary that is not on
        PATH) must not raise out of start(): that would kill the agent
        before the protocol even begins, and the Mac would reconnect
        forever against a crashing peer -- violating spec §3.2's "a
        machine where none of this works behaves exactly as today". Falls
        open (cache stays at __init__'s None, never reads the hint) and
        starts no reader thread -- there is nothing for it to read."""
        logged = []
        def popen(argv, **kwargs):
            raise FileNotFoundError("gdbus")
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   return_value=PATH)
        hint_patch = mock.patch("clipwire_agent.session_locked_hint", return_value=True)
        log_patch = mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m))
        resolve_patch.start()
        hint_patch.start()
        log_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        self.addCleanup(log_patch.stop)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        self.addCleanup(monitor.stop)
        monitor.start()   # must not raise
        self.assertIsNone(monitor.locked(),
                          "must fall open, not read the (unreachable) hint")
        self.assertIsNone(monitor._thread, "no reader thread when there is nothing to read")
        exception_lines = [m for m in logged if "FileNotFoundError" in m]
        self.assertEqual(len(exception_lines), 1)

    def test_subprocess_calls_never_run_with_the_cache_lock_held(self):
        """threading.Lock is non-reentrant, so a resolver/hint reader that
        can itself acquire _lock (non-blocking) proves the calling thread
        was not holding it. Pins the leaf-lock rule at BOTH call sites:
        start()'s initial resolve+Get on the caller's thread, and the
        re-resolve a SessionNew line triggers on the reader thread."""
        released = threading.Event()
        acquired = []
        def probe_and(value):
            def fn(*a, **k):
                ok = monitor._lock.acquire(blocking=False)
                acquired.append(ok)
                if ok:
                    monitor._lock.release()
                return value
            return fn
        def popen(argv, **kwargs):
            return _ScriptedPump([NEW_LINE], released)
        # LIFO addCleanup ordering, same reasoning as the churn test above.
        resolve_patch = mock.patch("clipwire_agent.resolve_graphical_session",
                                   side_effect=probe_and(PATH))
        hint_patch = mock.patch("clipwire_agent.session_locked_hint",
                                side_effect=probe_and(False))
        resolve_patch.start()
        hint_patch.start()
        self.addCleanup(resolve_patch.stop)
        self.addCleanup(hint_patch.stop)
        monitor = LockMonitor(run=_unreachable_run, popen=popen)
        self.addCleanup(monitor.stop)
        monitor.start()
        released.set()
        deadline = threading.Event()
        for _ in range(50):
            if len(acquired) >= 4:
                break
            deadline.wait(0.02)
        self.assertEqual(len(acquired), 4, "expected 2 resolves + 2 hints "
                         "(the initial one, then the SessionNew re-resolve)")
        self.assertTrue(all(acquired), "a subprocess call ran with _lock held")


if __name__ == "__main__":
    unittest.main()
