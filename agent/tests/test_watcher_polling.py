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


class RecordingStop:
    """A drop-in for PollingWatcher._stop that records the timeout the poll
    loop asks to wait for, and then does not wait at all -- it returns
    immediately for `ticks` iterations and stops the loop after that.

    TWO JOBS, and both were learned the hard way here.

    It records because of a mutation nothing else in this suite caught:
    with the loop's wait reverted to `self._stop.wait(self.interval)`, the
    whole of spec 5.1's backoff is still computed and still stored, so every
    assertion about self._retry_interval goes on passing while the loop
    retries at full rate -- the exact failure the backoff exists to prevent,
    behind an attribute reporting it fixed. Asserting on a value is not the
    same as asserting it is USED.

    It does not sleep because a backoff test otherwise has to choose between
    intervals too small to distinguish and a suite that takes twenty seconds
    -- and the third option, patching SAFETY_NET_POLL_SECONDS down, is worse
    than both: the constant is module-global and daemon poll threads from
    earlier tests outlive the cleanup that stopped them, so the patch is
    visible to code no test in the file is looking at. Free-running instead,
    the whole compounding sequence up to the REAL cap is observable in one
    exact list with no clock involved.

    Deliberately not a threading.Event subclass: wrapping one means the three
    methods the watcher actually calls have to be named here, which is what
    makes this break loudly rather than silently if the loop ever starts
    using a fourth."""

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


class TestFailedReadDoesNotConsumeTheChange(unittest.TestCase):
    """Spec 5.1, and the sharpest regression risk in v3.3.

    Before the uuid tier, this loop's probe and the worker's read failed
    TOGETHER -- both are wl-paste, and a powered-off monitor makes wl-paste
    time out (spec 1.4) -- so a copy the hung read could not deliver was
    caught up by accident: the baseline collapsed to None, and any later
    probe that answered differed from it and re-signalled. The fast tier
    breaks that. Its uuid keeps answering with the screen off, so it
    signals the copy, advances its own baseline and is done with it
    forever, while the worker's read delivers nothing.

    So this loop becomes the only thing that can still catch that change
    up, and all three halves of the rule are tested separately below,
    because any two without the third are worse than none: the baseline
    that does not advance, the signal that fires anyway, and the backoff
    that paces the pair.

    `on_tick` is passed by every test here that exercises the rule and
    withheld by the one that pins the standalone path, because being
    non-None is the discriminator pump() uses -- see PollingWatcher's own
    docstring."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.001)

    def quiesce(self, watcher):
        """Stopped AND joined before anything reads self._retry_interval: it
        is written on the poll thread, so a test that sampled it while that
        thread was still running would be asserting on a value the loop was
        free to change underneath it."""
        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(watcher._thread.is_alive(),
                         "the poll thread must be joined before its state is read")

    def test_a_probe_that_never_answered_does_not_consume_the_baseline(self):
        """The first half. `previous` must still hold the last MEASURED
        token, so the change that arrives when the read recovers is still a
        change relative to what was last actually seen.

        Asserted through on_tick rather than through on_change because it
        is the only deterministic window onto `previous`: the observer is
        handed (before, current) on EVERY tick, changed or not, while
        signals to the worker collapse. Under the unconditional advance
        this replaces, the second hanging tick reports (None, None) -- the
        baseline eaten by a probe that answered nothing."""
        clipboard = ScriptedReadClipboard([b"a", None])
        ticks = []
        watcher = PollingWatcher(
            clipboard, interval_seconds=0.005,
            on_tick=lambda before, current: ticks.append((before, current)))
        self.addCleanup(watcher.stop)
        watcher.start(lambda: None)

        self.wait_until(lambda: len(ticks) >= 3)
        self.quiesce(watcher)

        self.assertGreaterEqual(len(ticks), 3,
                                "the poll must have run past the first failed probe")
        self.assertEqual(
            ticks[:3], [(b"a", None)] * 3,
            "every unresolved tick must still be judged against the last token "
            "actually measured; got %r" % (ticks[:3],))

    def test_a_hang_that_ends_signals_even_when_the_token_did_not_move(self):
        """The second half, and the one nothing else in the suite catches.

        The recovery signal cannot be conditioned on the token having MOVED
        across the hang, because the case that loses a change is exactly
        the one where it did not. probe()'s token for an image is the
        offered TYPE LIST, and its docstring discloses that two copies from
        the same source application inside GPaste's takeover window can
        present identical lists -- it expects them to alternate and says
        plainly that the expectation is unverified. On that branch the
        token recovers EQUAL to the pre-hang one while the body behind it
        is a different picture, one the fast tier already consumed and the
        hung read never delivered.

        That is why it is the run ENDING that signals, not the comparison.
        Before v3.3 the same catch-up happened by accident, because the
        baseline collapsed to None and any later answer differed from it;
        holding the last measured token without this makes the loop
        strictly worse than what shipped, in the release's headline
        scenario. Measured against the composed watcher, not argued.

        The script is that shape exactly: b"a", a hang, then b"a" again."""
        clipboard = ScriptedReadClipboard([b"a", None, None, b"a"])
        delivered = threading.Event()
        watcher = PollingWatcher(clipboard, interval_seconds=0.005,
                                 on_tick=lambda before, current: None)
        self.addCleanup(watcher.stop)
        watcher.start(delivered.set)

        self.assertTrue(
            delivered.wait(JOIN_TIMEOUT),
            "the first probe to answer after a hang must offer the worker an "
            "observation whether or not the token moved across it: %r"
            % self.log_lines)

    def test_an_unresolved_tick_asks_for_no_observation_at_all(self):
        """The other side of the same rule, and the reason the retry is not
        simply "signal on every unresolved tick". probe() and read() are the
        same wl-paste binary, so while it is not answering a read is
        guaranteed to find nothing: every signal raised here would buy one
        more timed-out fork and no information. The idle-tick predicate is
        skipped for the same reason -- an armed re-offer expectation cannot
        be resolved by a read that cannot run, and Agent._reoffer_is_overdue
        gives such an expectation up on its own clock regardless.

        So a hang is silent, and the tick's whole output is the backoff.
        `on_tick` still runs (asserted, because the safety net must keep
        judging through a hang), which is what makes this "quiet" rather
        than "skipped"."""
        clipboard = ScriptedReadClipboard([b"a", None])
        observations = []
        asked = []
        ticks = []
        watcher = PollingWatcher(
            clipboard, interval_seconds=0.005,
            on_tick=lambda before, current: ticks.append((before, current)),
            on_idle_tick=lambda: asked.append(1) or True)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: observations.append(1))

        self.wait_until(lambda: len(ticks) >= 3)
        self.quiesce(watcher)

        self.assertGreaterEqual(len(ticks), 3,
                                "the safety net must go on being handed every "
                                "tick through a hang, or this proves nothing")
        self.assertEqual(observations, [],
                         "a wl-paste that is not answering must not be asked to "
                         "read; got %d observations" % len(observations))
        self.assertEqual(asked, [],
                         "the idle-tick predicate must not be consulted on a "
                         "tick whose read could not run either")

    def free_running(self, script, ticks, interval=1.0, on_idle_tick=None):
        """A composed-tier poller driven through exactly `ticks` iterations
        with no clock: RecordingStop returns instead of waiting, so the
        production interval and the production cap are the real ones and the
        test still finishes instantly. Returns the watcher, joined."""
        clipboard = ScriptedReadClipboard(script)
        watcher = PollingWatcher(clipboard, interval_seconds=interval,
                                 on_tick=lambda before, current: None,
                                 on_idle_tick=on_idle_tick)
        watcher._stop = RecordingStop(ticks)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: None)
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(watcher._thread.is_alive(),
                         "the free-running loop must have finished its budget")
        return watcher

    def test_an_unresolved_observation_backs_off(self):
        """Spec 5.1: without this the fix costs twelve 3-second timeout
        forks a minute against today's two. This is also where spec 4.2's
        "the monitor is on" gate actually lives -- the timeout observed
        directly rather than proxied through PowerSaveMode, which this
        release measured only in its monitor-ON state and deliberately
        rejected.

        Read through the stored attribute, which is the shape the rest of
        this class asserts on; the test below reads the same run through
        what the loop did with it, and both are needed."""
        watcher = self.free_running([b"a", None], ticks=3)

        self.assertIsNotNone(watcher._retry_interval,
                             "an unresolved observation retried at full rate")
        self.assertEqual(
            watcher._retry_interval, 8.0,
            "three unresolved ticks from a 1s interval must compound to 8s, "
            "not settle on one slower fixed rate")
        self.assertLessEqual(watcher._retry_interval, SAFETY_NET_POLL_SECONDS)

    def test_a_watcher_that_starts_during_a_hang_backs_off_and_signals_nothing(self):
        """The one tick shape a reader is most likely to misread, and every
        other script here starts with a token that answers, so nothing else
        reaches it.

        THE METHOD NAME IS WRONG AND THE NAME IS WHAT TEST OUTPUT PRINTS.
        `..._and_signals_nothing` is contradicted by this test's own second
        assertion, `delivered.wait(...)`, which requires that it DID signal
        -- the recovery tick delivers, and that delivery is half of what is
        being pinned. What is true, and what the clause was reaching for, is
        that the HUNG ticks signal nothing: the backoff engages on ticks
        that deliver nothing at all. Not renamed here because a doc audit
        may not change an executable line, and a method name is one; carried
        as a finding instead. Read the name as
        `..._backs_off_while_the_hang_lasts`.

        `previous` is None from the baseline probe onward, so `current !=
        previous` is FALSE on every hung tick -- the backoff engages on
        ticks that signal nothing at all, because it is computed before the
        comparison rather than inside it. Both halves are correct and
        neither is obvious: the probe really is timing out and forking it at
        full rate is pure cost, while no token was ever measured, so no
        change can have been consumed and there is nothing to catch up.

        Then the recovery still signals, and here `recovered` and the
        comparison agree -- which is exactly why this case cannot stand in
        for the equal-token one above, and why both are pinned."""
        delivered = threading.Event()
        clipboard = ScriptedReadClipboard([None, None, None, b"a"])
        watcher = PollingWatcher(clipboard, interval_seconds=1.0,
                                 on_tick=lambda before, current: None)
        watcher._stop = RecordingStop(4)
        self.addCleanup(watcher.stop)
        watcher.start(delivered.set)
        watcher._thread.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(
            watcher._stop.waits[:3], [1.0, 2.0, 4.0],
            "a hang that is already in progress at startup must back off "
            "even though it has no baseline to differ from; got %r"
            % (watcher._stop.waits[:3],))
        self.assertTrue(
            delivered.wait(JOIN_TIMEOUT),
            "and the first probe to answer must still be delivered: %r"
            % self.log_lines)

    def test_the_backoff_is_what_the_loop_actually_waits_on(self):
        """Storing the backed-off interval is not the same as pacing the
        loop with it, and every other assertion in this class reads the
        stored value. Reverting the wait to `self.interval` leaves all of
        them green while the loop retries a hanging probe at full rate --
        spec 5.1's fork storm, behind an attribute reporting it fixed. So
        this one asserts on the argument the loop passed, and nothing else.

        The whole sequence, exactly, against the REAL cap: the first wait
        precedes the first probe and so must still be the plain interval
        (which is what tells a backoff apart from a loop that merely starts
        slow), then it doubles, then it is HELD at SAFETY_NET_POLL_SECONDS
        rather than going on past it -- so however long a hang lasts, the
        slow tier never ends up rarer than the plain safety net it is."""
        watcher = self.free_running([b"a", None], ticks=7)

        self.assertEqual(
            watcher._stop.waits[:7],
            [1.0, 2.0, 4.0, 8.0, 16.0, SAFETY_NET_POLL_SECONDS,
             SAFETY_NET_POLL_SECONDS],
            "the loop must WAIT on the backed-off interval and stop doubling "
            "at the cap; got %r" % (watcher._stop.waits[:7],))

    def test_the_backoff_clears_on_a_probe_that_answers_without_a_change(self):
        """The reset belongs to "the probe answered", never to "the token
        moved" -- and the difference is not academic. Nothing can be copied
        while the screen is off, so a hang that recovers with the clipboard
        SETTLED is the common shape, not the corner one. Reset only inside
        the change branch and that recovery leaves the elevated interval in
        place until the next real copy: harmless at the healthy rate, and a
        permanent silent 30x regression in degraded mode, where
        DEGRADED_POLL_SECONDS is the connection's only sync path.

        The recovery here is deliberately the SETTLED one -- b"a" before the
        hang and b"a" after it -- so the reset cannot be reached through the
        change branch even by accident."""
        watcher = self.free_running([b"a", None, None, b"a"], ticks=5)

        self.assertEqual(
            watcher._stop.waits[:5], [1.0, 2.0, 4.0, 1.0, 1.0],
            "a probe that answered must hand the interval back even when the "
            "clipboard it answered about did not move; got %r"
            % (watcher._stop.waits[:5],))
        self.assertIsNone(
            watcher._retry_interval,
            "and the run's memory must be cleared with it, or the next tick to "
            "answer would report itself a recovery")

    def test_the_rule_is_active_on_the_watcher_production_actually_builds(self):
        """Every other test here hands `on_tick` to a bare PollingWatcher, so
        together they prove the rule works GIVEN it is switched on -- and
        nothing proves that GPasteWatcher's safety net is the poller that
        has it switched on. That gap is this project's shipped history, not
        a hypothetical: spec 2 cites "a fix sited in a watcher that is never
        constructed on the target machine", and the file's own
        test_the_composed_safety_net_never_calls_read_either exists because
        "a fix applied only to the standalone poller would have left the
        reported defect untouched".

        The wiring is otherwise protected only incidentally. Dropping
        `on_tick=self._observe_tick` from GPasteWatcher's PollingWatcher
        does go red in test_watcher_safety_net.py -- but for "no verdict was
        reached", not for "spec 5.1's rule was off". A restructure that kept
        a verdict observer while moving the slow tier out from under the
        rule would stay green everywhere else, and spec 4.2's divergence
        re-probe is exactly such a restructure.

        Both halves are asserted at the real composition, and they fail to
        different mutations: the backoff engaged (only true if the composed
        safety net satisfies the gate) and the change was delivered across a
        hang the token does not record (only true if `recovered` fires).

        The fast tier is pushed out of reach rather than stubbed, so the
        only thing that can signal the worker here is the safety net's own
        pump -- which is what makes the delivery assertion evidence about
        this rule rather than about the uuid tier. Its uuid baseline is left
        unseeded for the same reason: uuid_frozen stays False, so
        _observe_tick reaches no verdict and cannot change the interval
        underneath the wait sequence."""
        clipboard = ScriptedReadClipboard([b"a", None, None, b"a"])
        delivered = threading.Event()
        process = FakeGPasteProcess()
        self.addCleanup(process.close)
        watcher = GPasteWatcher(clipboard, safety_net_interval_seconds=1.0,
                                degraded_interval_seconds=1.0,
                                fast_interval_seconds=JOIN_TIMEOUT * 100)
        watcher._safety_net._stop = RecordingStop(5)
        self.addCleanup(watcher.stop)
        with mock.patch("subprocess.Popen", return_value=process):
            watcher.start(delivered.set)
        watcher._safety_net._thread.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(
            watcher._safety_net._stop.waits[:3], [1.0, 2.0, 4.0],
            "spec 5.1's backoff must be active on the poller GPasteWatcher "
            "actually composes, not only on one built by hand; got %r"
            % (watcher._safety_net._stop.waits[:3],))
        self.assertTrue(
            delivered.wait(JOIN_TIMEOUT),
            "and the change the hung read could not deliver must reach the "
            "worker there too, on a token equal across the hang: %r"
            % self.log_lines)

    def test_the_standalone_poller_keeps_todays_behaviour_exactly(self):
        """Spec 2's constraint: a machine with no GPaste is untouched.
        There is no fast tier there to consume a change behind this loop's
        back -- the probe and the read fail together, as they always did --
        and None also means "the selection is empty", which on that machine
        is the normal end state once a selection owner exits and nothing
        repopulates. Applying the rule there would retry an empty clipboard
        forever.

        Both halves are asserted, because the gate feeds both and a
        mutation could drop it from either: the baseline still collapses to
        None (proved by the recovery re-signalling on a token equal to the
        pre-hang one, which only a None baseline can differ from), and the
        backoff never engages.

        Paced, because this asserts WHICH values were observed -- see
        ScriptedReadClipboard."""
        clipboard = ScriptedReadClipboard([b"a", None, b"a"], paced=True)
        observed = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.005)
        self.addCleanup(watcher.stop)
        watcher.start(lambda: observed.append(clipboard.take()))

        self.wait_until(lambda: len(observed) >= 2)
        self.quiesce(watcher)

        self.assertEqual(
            observed, [None, b"a"],
            "the standalone path must keep advancing its baseline through a "
            "failed probe, so the recovery is a change against None")
        self.assertIsNone(
            watcher._retry_interval,
            "the standalone path must never back off: its probe and its read "
            "fail together, so there is no consumed change to retry for")



class TestSlowTierIdleGate(unittest.TestCase):
    """Spec 4.2's second gate, composed with the poll loop: "the user has
    been recently active via Mutter.IdleMonitor.GetIdletime (with nobody
    copying there is no divergence to find, so an idle blink is pure cost)".

    PLACED HERE because the gate is a `continue` in PollingWatcher.pump,
    which is this file's subject -- the same loop, one screenful above, that
    TestFailedReadDoesNotConsumeTheChange owns for spec 5.1, and beside the
    spec 2 test that pins the standalone path untouched. The GATE FUNCTION
    itself (_user_recently_active: parsing, the threshold, the three-valued
    rule) is a module-level gdbus reader and is tested in
    test_watcher_uuid_tier.py beside gpaste_history_uuid, which it mirrors.

    THE TRAP THIS CLASS EXISTS FOR is test_an_unmeasured_gate_still_probes
    below. A gate that failed CLOSED would disable the safety net on every
    machine where org.gnome.Mutter.IdleMonitor is absent, renamed or slow --
    and the safety net is the only thing that can see a dead tracker, which
    is the failure this entire release is about. The gate would then have
    caused, silently, the exact class of bug it was added to help find."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.001)

    def quiesce(self, watcher):
        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(watcher._thread.is_alive(),
                         "the poll thread must be joined before its state is read")

    def gated_watcher(self, idle_answers, script=(b"a", b"b", b"c")):
        """A composed-shape poller (on_tick wired, as GPasteWatcher's safety
        net is) gated by THE PRODUCTION PREDICATE -- a real GPasteWatcher's
        _slow_tier_should_probe -- fed raw three-valued idle readings.

        Composed rather than hand-written on purpose: `should_probe` takes a
        bool and the None-proceeds rule lives one layer up, in that method,
        so a test that passed None straight to the loop would be testing a
        contract production never uses and would read `not None` as "skip" --
        the exact inversion this class exists to forbid, passing.

        The GPasteWatcher is never started; only its predicate is borrowed.
        clipboard=None is safe there for the same reason TestDegradedBackoff
        gives: nothing starts, and the predicate never touches a clipboard.

        Returns (watcher, clipboard, observations, gate_calls); `observations`
        is every (previous, current) pair on_tick was handed, which is what a
        gated tick must produce NONE of."""
        replies = list(idle_answers)
        gate_calls = []

        def read_idle_gate():
            gate_calls.append(True)
            return replies[min(len(gate_calls) - 1, len(replies) - 1)]

        composer = GPasteWatcher(clipboard=None, read_idle_gate=read_idle_gate)
        self.addCleanup(composer.stop)
        clipboard = ProbeOnlyClipboard(script)
        observations = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.005,
                                 on_tick=lambda p, c: observations.append((p, c)),
                                 should_probe=composer._slow_tier_should_probe)
        self.addCleanup(watcher.stop)
        return watcher, clipboard, observations, gate_calls

    def test_an_unmeasured_gate_still_probes(self):
        """THE TRAP, pinned. _user_recently_active returns None when the call
        failed, and GPasteWatcher._slow_tier_should_probe turns that into
        True -- "not measured" PROCEEDS. Driven through the real loop with a
        gate that only ever answers "unavailable", so a machine with no
        IdleMonitor at all is what this test actually simulates.

        Mutating `is not False` to a truthiness test in
        _slow_tier_should_probe is what this catches, and there is no
        comparison in pump to mutate instead -- which is why the assertion is
        on OBSERVATIONS REACHED rather than on the predicate's return.

        TWO WATCHERS AND TWO ASSERTIONS, which the name covers only half of.
        The first is the predicate asked DIRECTLY, and it is what reds for the
        truthiness mutation. The second is a real poll loop gated by that same
        production predicate, and it is what proves the first is not merely
        correct-and-unreached -- the failure shape this whole release keeps
        finding. Either alone is weaker: a direct assertion cannot see a gate
        wired backwards into pump, and a loop assertion cannot say WHICH rule
        let the ticks through."""
        watcher = GPasteWatcher(clipboard=None, read_idle_gate=lambda: None)
        self.addCleanup(watcher.stop)
        self.assertTrue(
            watcher._slow_tier_should_probe(),
            "an unmeasured idle reading must PROCEED: a gate that failed "
            "closed would disable the safety net on every machine without "
            "org.gnome.Mutter.IdleMonitor")

        gated, clipboard, observations, _calls = self.gated_watcher([None])
        gated.start(lambda: None)
        self.wait_until(lambda: len(observations) >= 2)
        self.quiesce(gated)

        self.assertGreaterEqual(
            len(observations), 2,
            "an unmeasured gate suppressed the slow tier entirely")
        self.assertGreater(clipboard.calls, 1, "no probe was ever made")

    def test_an_active_user_probes(self):
        watcher, clipboard, observations, _ = self.gated_watcher([True])
        watcher.start(lambda: None)
        self.wait_until(lambda: len(observations) >= 2)
        self.quiesce(watcher)

        self.assertGreaterEqual(len(observations), 2)

    def test_an_idle_user_costs_no_fork_and_no_observation(self):
        """The saving the gate exists for, and BOTH halves are asserted
        because either alone would pass against a half-implemented gate. No
        probe (the fork, and before v3.3 the focus blink, is the whole cost),
        and no on_tick -- which is not an optimisation but a correctness
        rule: on_tick with an unchanged token would feed spec 6.2's backoff a
        settled token, and that backoff DOUBLES on exactly that. Two idle
        mechanisms compounding, one of them silent."""
        watcher, clipboard, observations, gate_calls = self.gated_watcher([False])
        watcher.start(lambda: None)
        self.wait_until(lambda: len(gate_calls) >= 5)
        self.quiesce(watcher)

        self.assertEqual(
            clipboard.calls, 1,
            "a gated-out tick must fork no wl-paste at all -- 1 is the "
            "baseline probe taken before the loop, which is not gated")
        self.assertEqual(
            observations, [],
            "a gated-out tick must reach no observer: handing on_tick an "
            "unchanged token would compound two idle mechanisms")

    def test_the_gate_does_not_advance_the_baseline_across_an_idle_stretch(self):
        """What a skipped tick PRESERVES, and it is why skipping is correct
        rather than merely cheap. `previous` is not advanced, so a divergence
        that happened during the idle stretch is still visible to the first
        tick that runs after it -- the evidence survives the gap instead of
        being consumed by a tick that observed nothing."""
        watcher, clipboard, observations, gate_calls = self.gated_watcher(
            [False, False, False, True], script=[b"a", b"z"])
        watcher.start(lambda: None)
        self.wait_until(lambda: observations)
        self.quiesce(watcher)

        self.assertEqual(
            observations[0], (b"a", b"z"),
            "the first ungated tick must compare against the pre-gap "
            "baseline, or an idle stretch swallows the divergence inside it")

    def test_the_standalone_poller_has_no_gate_to_consult(self):
        """Spec 2: the machine with no GPaste at all is untouched. Its
        clipboard may legitimately never move again, so gating its ticks on
        user activity would be the wrong trade there even if it were free --
        and make_watcher passes nothing, so there is nothing to consult."""
        clipboard = ProbeOnlyClipboard([b"a", b"b"])
        observed = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.005)
        self.addCleanup(watcher.stop)
        self.assertIsNone(
            watcher._should_probe,
            "the standalone poller must default to ungated")
        watcher.start(lambda: observed.append(clipboard.take()))

        self.wait_until(lambda: observed)
        self.quiesce(watcher)
        self.assertTrue(observed, "the ungated path stopped ticking")

    def test_the_gate_is_asked_before_the_probe_not_after_it(self):
        """Ordering, pinned by counts rather than by reading the source. A
        gate asked AFTER probe() would still skip on_tick and would pass
        every other test in this class while saving exactly nothing -- the
        fork is the cost the gate exists to avoid."""
        watcher, clipboard, _, gate_calls = self.gated_watcher([False])
        watcher.start(lambda: None)
        self.wait_until(lambda: len(gate_calls) >= 5)
        self.quiesce(watcher)

        self.assertGreaterEqual(len(gate_calls), 5,
                                "the gate must be asked on every tick")
        self.assertEqual(
            clipboard.calls, 1,
            "probe() ran on a gated tick: the gate is being asked after the "
            "fork it exists to prevent")


class TestHealthyProbeSuppression(unittest.TestCase):
    """v3.6's gate, composed with the poll loop the way TestSlowTierIdleGate
    is. While GPaste is trusted, every probe() is a wl-paste fork whose
    transient Wayland window blinks focus on GNOME (and raises the
    "wl-paste is ready" banner when mutter denies it focus) -- and the
    signal path plus the uuid tier already see everything GPaste sees. So a
    suppressed tick forks nothing and judges nothing, but keeps servicing
    the reoffer backstop: that is the one duty the slow loop still owes a
    healthy connection.

    A SECOND gate beside the idle gate, deliberately never merged with it:
    the idle gate's skip covers the whole tick INCLUDING the backstop (a
    forced read against an idle or dark session is what it exists to
    prevent), while this gate must leave the backstop alive. Merged, one of
    those two regressions is the price."""

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.001)

    def quiesce(self, watcher):
        watcher.stop()
        watcher._thread.join(timeout=JOIN_TIMEOUT)
        self.assertFalse(watcher._thread.is_alive(),
                         "the poll thread must be joined before its state is read")

    def suppressed_watcher(self, suppressed_answers, on_idle_tick=None,
                           should_probe=None, script=(b"a", b"z")):
        """A composed-shape poller whose probe_suppressed replays
        `suppressed_answers` and then repeats the last one, counting asks.
        Returns (watcher, clipboard, observations, asked)."""
        replies = list(suppressed_answers)
        asked = []

        def probe_suppressed():
            asked.append(True)
            return replies[min(len(asked) - 1, len(replies) - 1)]

        clipboard = ProbeOnlyClipboard(script)
        observations = []
        watcher = PollingWatcher(clipboard, interval_seconds=0.005,
                                 on_tick=lambda p, c: observations.append((p, c)),
                                 on_idle_tick=on_idle_tick,
                                 should_probe=should_probe,
                                 probe_suppressed=probe_suppressed)
        self.addCleanup(watcher.stop)
        return watcher, clipboard, observations, asked

    def test_a_suppressed_loop_forks_nothing_not_even_the_baseline(self):
        """BOTH zeros matter. Zero probes on the ticks is the 30-second
        blink this gate exists to remove; zero at the START is the baseline
        probe, which used to run unconditionally before the loop -- and the
        watcher is rebuilt on every reconnect, so that one fork landed at
        wake, the worst possible moment for a 3s hang and its banner."""
        watcher, clipboard, observations, asked = self.suppressed_watcher([True])
        watcher.start(lambda: None)
        self.wait_until(lambda: len(asked) >= 5)
        self.quiesce(watcher)

        self.assertGreaterEqual(len(asked), 5,
                                "the suppression must be re-asked on every tick")
        self.assertEqual(
            clipboard.calls, 0,
            "a suppressed loop forked wl-paste anyway -- 0 means suppressed "
            "ticks AND the pre-loop baseline, which is under the same gate")
        self.assertEqual(
            observations, [],
            "a suppressed tick must reach no observer: there is no token to "
            "judge, and a judged None would look like a failed read")

    def test_an_overdue_reoffer_still_fires_through_a_suppressed_tick(self):
        """The one duty a suppressed tick keeps. The backstop's forced read
        is the image path -- rare, and visible by necessity -- but the
        DECISION to force it must survive the suppression, or an image
        write whose re-offer signal never came stays unresolved forever."""
        fired = threading.Event()
        watcher, clipboard, _, _ = self.suppressed_watcher(
            [True], on_idle_tick=lambda: True)
        watcher.start(fired.set)
        self.wait_until(fired.is_set)
        self.quiesce(watcher)

        self.assertTrue(
            fired.is_set(),
            "an overdue reoffer must still signal through a suppressed tick")
        self.assertEqual(
            clipboard.calls, 0,
            "the backstop DECISION must cost no fork; the forced read it "
            "provokes runs on the observer, not here")

    def test_the_idle_gate_still_outranks_the_suppressed_backstop(self):
        """Ordering between the two gates, pinned. The idle gate's skip
        covers the backstop today because a forced read against an idle or
        dark session hangs -- the suppression must not open that hole by
        servicing the backstop on a tick the idle gate already refused."""
        backstop_asked = []
        gate_calls = []

        def idle_gate():
            gate_calls.append(True)
            return False

        watcher, clipboard, observations, _ = self.suppressed_watcher(
            [True], on_idle_tick=lambda: backstop_asked.append(True) or True,
            should_probe=idle_gate)
        watcher.start(lambda: None)
        self.wait_until(lambda: len(gate_calls) >= 5)
        self.quiesce(watcher)

        self.assertEqual(
            backstop_asked, [],
            "an idle-gated tick serviced the backstop: the forced read it "
            "triggers is exactly the idle/dark fork the idle gate prevents")
        self.assertEqual(clipboard.calls, 0,
                         "no fork on a tick both gates refused")

    def test_a_lifted_suppression_resumes_probing_and_the_first_probe_signals(self):
        """The recovery direction, and its one deliberate cost. The
        suppressed stretch skipped the baseline, so the first probe after
        the lift compares against None and MUST signal: one extra full
        read, absorbed upstream by the content dedup -- never a swallowed
        change. A lift that waited for a second probe to establish a
        baseline would eat the very divergence that lifted it."""
        fired = threading.Event()
        watcher, clipboard, observations, _ = self.suppressed_watcher(
            [True, True, True, False], script=[b"z"])
        watcher.start(fired.set)
        self.wait_until(fired.is_set)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 1,
            "lifting the suppression must bring the forks back: the poll is "
            "the only detector left once GPaste cannot be trusted")
        self.assertTrue(observations, "the first probe must reach the observer")
        self.assertEqual(
            observations[0], (None, b"z"),
            "the first post-lift tick judges against the None baseline and "
            "signals -- one extra read, never a swallowed change")

    def test_the_standalone_poller_has_no_suppression_to_consult(self):
        """Spec 2's machine, untouched again: with no GPaste there is no
        healthy tier to trust -- the poll IS the sync, and make_watcher's
        fallback branch passes nothing."""
        clipboard = ProbeOnlyClipboard([b"a", b"b"])
        watcher = PollingWatcher(clipboard, interval_seconds=0.005)
        self.addCleanup(watcher.stop)
        self.assertIsNone(
            watcher._probe_suppressed,
            "the standalone poller must default to unsuppressed")
        observed = []
        watcher.start(lambda: observed.append(clipboard.take()))
        self.wait_until(lambda: observed)
        self.quiesce(watcher)
        self.assertTrue(observed, "the unsuppressed path stopped observing")
