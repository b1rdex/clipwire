# agent/tests/test_lock_gate.py
"""LockGate (v3.5 spec §3.1/§3.3): lock closes instantly, unlock opens only
after LockedHint=false has been HELD for UNLOCK_HOLD_SECONDS of wall time on
the subscriber's clock, a fresh unlocked start pays no debounce tax."""
import os
import unittest
from unittest import mock

from agent_under_test import (
    LockGate,
    UNLOCK_HOLD_SECONDS,
    WaylandClipboard,
)
import clipwire_agent


class _Session:
    """Scripted (locked, since) state plus the clock the gate reads."""
    def __init__(self):
        self.t = 1000.0
        self.locked = False
        self.since = None
    def now(self):
        return self.t
    def state(self):
        return self.locked, self.since
    def flip(self, locked):
        self.locked = locked
        self.since = self.t


class TestLockGate(unittest.TestCase):
    def setUp(self):
        self.session = _Session()
        self.gate = LockGate(state=self.session.state, now=self.session.now)

    def test_fresh_unlocked_start_is_open_immediately(self):
        self.assertTrue(self.gate.open())

    def test_fail_open_is_open_immediately(self):
        self.session.locked = None
        self.assertTrue(self.gate.open())

    def test_lock_closes_on_the_first_sample(self):
        self.assertTrue(self.gate.open())
        self.session.flip(True)
        self.assertFalse(self.gate.open())

    def test_unlock_opens_only_after_the_hold(self):
        self.session.flip(True)
        self.assertFalse(self.gate.open())
        self.session.t += 90.0
        self.session.flip(False)
        self.assertFalse(self.gate.open())                    # 0s held
        self.session.t += UNLOCK_HOLD_SECONDS / 2
        self.assertFalse(self.gate.open())                    # under the hold
        self.session.t += UNLOCK_HOLD_SECONDS
        self.assertTrue(self.gate.open())                     # held long enough

    def test_bounce_restarts_the_hold(self):
        self.session.flip(True)
        self.assertFalse(self.gate.open())
        self.session.t += 10.0
        self.session.flip(False)
        self.assertFalse(self.gate.open())
        self.session.t += 0.5
        self.session.flip(True)                               # bounce
        self.assertFalse(self.gate.open())
        self.session.t += 5.0
        self.session.flip(False)
        self.session.t += 0.5
        self.assertFalse(self.gate.open())                    # new hold, not yet
        self.session.t += UNLOCK_HOLD_SECONDS
        self.assertTrue(self.gate.open())

    def test_stretch_duration_is_banked_once_and_measured_on_stamps(self):
        """stretch = unlock stamp minus lock stamp -- the debounce hold is NOT
        part of the locked duration."""
        self.session.flip(True)
        self.gate.open()
        self.session.t += 90.0
        self.session.flip(False)
        self.session.t += UNLOCK_HOLD_SECONDS
        self.assertTrue(self.gate.open())
        self.assertEqual(self.gate.stretch_just_ended(), 90.0)
        self.assertIsNone(self.gate.stretch_just_ended())

    def test_lock_edge_logs_once(self):
        logged = []
        self.session.flip(True)
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)):
            self.gate.open(); self.gate.open(); self.gate.open()
        self.assertEqual(len([m for m in logged if "session locked" in m]), 1)


class TestClipboardGate(unittest.TestCase):
    def test_socket_and_gate_must_both_agree(self):
        gate = mock.Mock()
        gate.open.return_value = False
        clipboard = WaylandClipboard(lock_gate=gate)
        with mock.patch("clipwire_agent.wayland_socket_path", return_value="/nonexistent"):
            self.assertFalse(clipboard.ready())
        with mock.patch("os.path.exists", return_value=True):
            self.assertFalse(clipboard.ready())
            gate.open.return_value = True
            self.assertTrue(clipboard.ready())

    def test_no_gate_means_todays_behaviour(self):
        clipboard = WaylandClipboard()
        with mock.patch("os.path.exists", return_value=True):
            self.assertTrue(clipboard.ready())


class TestClipboardLockStretchDelegation(unittest.TestCase):
    """WaylandClipboard.lock_stretch_ended() (Task 6 consumes this via a
    getattr fallback, so its shape here is the frozen contract)."""

    def test_delegates_to_the_gate(self):
        gate = mock.Mock()
        gate.stretch_just_ended.return_value = 42.0
        clipboard = WaylandClipboard(lock_gate=gate)
        self.assertEqual(clipboard.lock_stretch_ended(), 42.0)
        gate.stretch_just_ended.assert_called_once_with()

    def test_none_without_a_gate(self):
        clipboard = WaylandClipboard()
        self.assertIsNone(clipboard.lock_stretch_ended())


class TestMainWiring(unittest.TestCase):
    """B2 (review blocker, repeated in Task 5's brief): LockMonitor +
    LockGate are constructed ONLY in the real-clipboard branch, and
    monitor.stop() runs when run() returns and nowhere else. LockMonitor
    itself is always mocked here -- these tests must never spawn a real
    `gdbus monitor` subprocess or its reader thread."""

    def _no_fake_clipboard_env(self):
        """mock.patch.dict(os.environ) saves/restores the whole dict; the
        pop() inside makes the branch deterministic regardless of what the
        ambient shell (or another test) left behind."""
        patch = mock.patch.dict(os.environ)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("CLIPWIRE_FAKE_CLIPBOARD", None)

    def test_fake_clipboard_constructs_no_monitor(self):
        with mock.patch.dict(os.environ, {"CLIPWIRE_FAKE_CLIPBOARD": "never-ready"}), \
             mock.patch("clipwire_agent.LockMonitor") as monitor_cls, \
             mock.patch("clipwire_agent.Agent.run", return_value=0):
            self.assertEqual(clipwire_agent.main([]), 0)
        monitor_cls.assert_not_called()

    def test_real_clipboard_starts_then_stops_the_monitor_around_run(self):
        self._no_fake_clipboard_env()
        calls = []
        monitor = mock.Mock()
        monitor.start.side_effect = lambda: calls.append("start")
        monitor.stop.side_effect = lambda: calls.append("stop")

        def fake_run(self):
            calls.append("run")
            return 0

        with mock.patch("clipwire_agent.LockMonitor", return_value=monitor) as monitor_cls, \
             mock.patch("clipwire_agent.Agent.run", fake_run):
            self.assertEqual(clipwire_agent.main([]), 0)
        monitor_cls.assert_called_once_with()
        self.assertEqual(calls, ["start", "run", "stop"])

    def test_monitor_stops_even_when_run_raises_frame_error(self):
        self._no_fake_clipboard_env()
        calls = []
        monitor = mock.Mock()
        monitor.start.side_effect = lambda: calls.append("start")
        monitor.stop.side_effect = lambda: calls.append("stop")

        def fake_run(self):
            calls.append("run")
            raise clipwire_agent.FrameError("boom")

        with mock.patch("clipwire_agent.LockMonitor", return_value=monitor), \
             mock.patch("clipwire_agent.Agent.run", fake_run), \
             mock.patch("clipwire_agent.log"):
            self.assertEqual(clipwire_agent.main([]), 2)
        self.assertEqual(calls, ["start", "run", "stop"])


if __name__ == "__main__":
    unittest.main()
