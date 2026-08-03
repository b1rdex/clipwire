# agent/tests/test_watcher_polling.py
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
    JOIN_TIMEOUT,
    FakeGPasteProcess,
    ScriptedReadClipboard,
)
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
        _local_change blocks on _observe_lock and its own clipboard read can
        take up to SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT = 13s on an
        image clipboard (--list-types, then the body)."""
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

    def test_an_unchanged_token_still_signals_when_on_idle_tick_says_so(self):
        """v3.2's no-change branch, and the whole reason it exists.

        A clipboard that never changes again is not the same thing as
        nothing worth looking at. After this side applies an image from the
        peer, an expectation is armed waiting for GPaste to re-offer it --
        and on a machine with no GPaste that re-offer never comes and the
        selection never moves again. Without this branch nothing observes
        the clipboard a second time, _consume_image_reoffer never runs, and
        the disarm is a branch nothing reaches: the user's next image copy,
        whenever it happens, is absorbed as the re-offer instead. Under
        v3.2 that clip is not late, it is gone.

        The script never changes, so every signal here comes from the
        predicate rather than from the token."""
        clipboard = ScriptedReadClipboard([b"unmoving"])
        observations = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.005,
                                 on_idle_tick=lambda: True)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: observations.append(1))

        self.wait_until(lambda: len(observations) >= 2)
        watcher.stop()
        self.assertGreaterEqual(
            len(observations), 2,
            "a static clipboard must still be observed while the predicate asks for it",
        )

    def test_an_unchanged_token_signals_nothing_when_the_predicate_declines(self):
        """The control, and the one that keeps the branch from being 'poll the
        clipboard on every tick forever'. With no expectation armed the
        predicate says no and the loop behaves exactly as it did before v3.2
        -- which is what bounds the extra read to at most one detection
        budget's worth of ticks per applied image, and to none at all once
        the re-offer arrives.

        Deliberately paired with the test above rather than folded into it:
        a branch that signalled unconditionally would pass that one."""
        clipboard = ScriptedReadClipboard([b"unmoving"])
        observations = []
        asked = []
        watcher = PollingWatcher(
            clipboard, interval_seconds=0.002,
            on_idle_tick=lambda: asked.append(1) or False)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: observations.append(1))

        self.wait_until(lambda: len(asked) >= 3)
        watcher.stop()
        self.assertGreaterEqual(len(asked), 3, "the predicate must actually be asked")
        self.assertEqual(observations, [],
                         "an unchanged clipboard nobody is waiting on is not an observation")

    def test_a_real_change_still_reaches_the_tick_observer_when_the_predicate_is_wired(self):
        """Health accounting outranks absorption. The no-change branch must
        be an `elif` under the real-change branch and must not skip past
        on_tick: _observe_tick is the safety net's whole judgement, and a
        tick that returned early to service an expectation would leave a
        dead event source undiagnosed for another full cycle -- the safety
        net blinded by the very mechanism that proves it was needed."""
        ticks = []
        clipboard = ScriptedReadClipboard([b"a", b"b", b"b", b"b"])
        watcher = PollingWatcher(
            clipboard, interval_seconds=0.002,
            on_tick=lambda previous, current: ticks.append((previous, current)),
            on_idle_tick=lambda: True)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: None)

        self.wait_until(lambda: len(ticks) >= 3)
        watcher.stop()
        self.assertGreaterEqual(len(ticks), 3,
                                "every tick must still reach the verdict, changed or not")
        self.assertIn((b"a", b"b"), ticks,
                      "and the real change must still be reported as one")

    def test_stop_ends_the_poll_loop_promptly(self):
        clipboard = ScriptedReadClipboard([b"x"] * 10000)
        watcher = PollingWatcher(clipboard, interval_seconds=0.02)
        watcher.start(lambda: None)

        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertFalse(
            watcher._thread.is_alive(), "stop() must end the poll loop promptly"
        )


class ProbeOnlyClipboard:
    """read() is a trap. The poll loop must ask for the cheap change TOKEN
    and nothing else.

    The other doubles in this file alias probe to read, because their
    scripts are cheap in-memory values with no body behind them and every
    assertion about what the loop observed stays true either way. That
    aliasing is exactly why this one has to exist: an alias cannot tell the
    two calls apart, so nothing else in this suite would notice the loop
    reverting to read()."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0
        self.last = None

    def read(self):
        raise AssertionError("the poll loop must call probe(), never read()")

    def probe(self):
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        self.last = self._script[index]
        return self.last

    def take(self):
        return self.last


class TestPollingWatcherAsksForATokenNotTheContent(unittest.TestCase):
    """The spec's requirement, unimplemented until the final wave: "in
    degraded mode the image body must be checked off a change in `wl-paste
    --list-types` rather than the content itself".

    It is not scoped to degraded mode in practice, and that is what made it
    a defect rather than a tuning question: GPasteWatcher composes this
    same PollingWatcher as its safety net on EVERY connection, so a HEALTHY
    install with a screenshot on the clipboard forked two processes and
    piped up to MAX_IMAGE_BYTES every SAFETY_NET_POLL_SECONDS, held two
    4 MiB buffers resident as `previous` and `current`, and compared them
    each tick -- for the life of the connection. See
    WaylandClipboard.probe."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def test_the_loop_never_calls_read(self):
        """Both assertions are needed. The loop guards every tick with
        _handle_observer_error, and AssertionError is not in the fatal set,
        so a loop that called read() would not crash -- it would log and
        carry on observing nothing. So: the change must actually be
        reported (proving probe drove the loop) AND nothing may have been
        logged as a poll error (proving read was never reached)."""
        seen = threading.Event()
        clipboard = ProbeOnlyClipboard([b"a", b"b"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.002)
        self.addCleanup(watcher.stop)
        watcher.start(seen.set)

        self.assertTrue(seen.wait(JOIN_TIMEOUT),
                        "the change must be observed through probe(): %r" % self.log_lines)
        watcher.stop()
        self.assertEqual([line for line in self.log_lines if "poll error" in line], [])

    def test_the_composed_safety_net_never_calls_read_either(self):
        """The healthy path, which is where the cost actually lived: this
        poller is GPasteWatcher's safety net, running on every connection
        whether or not anything is wrong. A fix applied only to the
        standalone poller would have left the reported defect untouched."""
        clipboard = ProbeOnlyClipboard([b"a", b"b"])
        ticks = []
        process = FakeGPasteProcess()
        self.addCleanup(process.close)
        watcher = GPasteWatcher(clipboard, safety_net_interval_seconds=0.002,
                                degraded_interval_seconds=0.002)
        watcher._safety_net._on_tick = lambda previous, current: ticks.append((previous, current))
        self.addCleanup(watcher.stop)
        with mock.patch("subprocess.Popen", return_value=process):
            watcher.start(lambda: None)

        deadline = time.monotonic() + JOIN_TIMEOUT
        while len(ticks) < 2 and time.monotonic() < deadline:
            time.sleep(0.005)
        watcher.stop()

        self.assertGreaterEqual(len(ticks), 2,
                                "the safety net must keep ticking through probe(): %r"
                                % self.log_lines)
        self.assertEqual([line for line in self.log_lines if "poll error" in line], [])


