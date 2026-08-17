# agent/tests/test_lock_monitor.py
"""LockMonitor (v3.5 spec §3.2): subscribe first, then read; re-resolve on
session churn; fail-open is logged once and answers None, never False-as-a-
default."""
import subprocess
import threading
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


class TestMonitor(unittest.TestCase):
    def _monitor(self, lines, resolved=PATH, hint=False):
        released = threading.Event()
        order = []
        def popen(argv, **kwargs):
            order.append("subscribe")
            return _ScriptedPump(lines, released)
        def run(argv, **kwargs):  # ListSessions/Get double via resolver patching below
            raise AssertionError("resolver is patched; run must not be called")
        monitor = LockMonitor(run=run, popen=popen)
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
