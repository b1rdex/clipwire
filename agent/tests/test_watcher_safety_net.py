# agent/tests/test_watcher_safety_net.py
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
    ArmThenSignalClipboard,
    FakeGPasteProcess,
    ScriptedReadClipboard,
    SignallingClipboard,
)
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

    SWITCH_MARKER = "reported no clipboard change"

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
        # Task 6: _observe_tick's verdict now judges against the fast tier's
        # uuid (spec 4.0), not the signal counter these tests otherwise
        # exercise -- see _observe_tick's own predicate comment. Every test
        # in this class constructs its watcher with fast_interval_seconds
        # left at the production default (5s, or whatever
        # CLIPWIRE_FAST_TIER_SECONDS says), far longer than these
        # millisecond-scale tests run, so the real fast tier never ticks and
        # self._last_uuid would stay None for the test's whole life --
        # reading as "no verdict may be reached" (spec 4.0.1) and leaving
        # every _observe_tick verdict below permanently unreachable, not
        # merely delayed. Seeded by hand for the same reason TestSlowTierVerdict
        # in test_watcher_uuid_tier.py seeds it: this class is about the
        # WL-PASTE TOKEN's own arm/confirm/clear behaviour, not about the
        # uuid tier's own measured-vs-unmeasured distinction, which has its
        # own dedicated tests there.
        #
        # BOTH fields, not just self._last_uuid (fix round 1, a coordinator
        # review finding): _observe_tick's "frozen" is a DELTA against
        # self._uuid_at_last_tick, not a presence check, so seeding only
        # self._last_uuid left self._uuid_at_last_tick at its __init__
        # default of None -- meaning every test's FIRST _observe_tick call
        # still saw uuid_frozen False regardless, needing an extra warm-up
        # tick none of these scripts provide. Priming both to the SAME
        # value represents a connection already past its first slow-tier
        # tick, which is the steady state every test here means to exercise.
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"
        # Same reason as `process` above, and the same instant: a clipboard
        # that drives the SIGNAL COUNTER from inside its own read() must hold
        # the watcher before the baseline read.
        if hasattr(clipboard, "watcher"):
            clipboard.watcher = watcher
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

    def test_the_verdict_reports_evidence_and_does_not_assert_a_cause(self):
        """The production incident this task exists for: the line said `GPaste
        is not reporting clipboard changes (is the gnome-shell extension
        enabled?)` on a machine where the extension was enabled and active,
        the bus name was owned, and a direct probe caught three Update signals
        for three copies. The line asserted a cause it could not know and
        carried no evidence, which is why the mechanism was never established.

        No emitted gdbus line ever reaches this watcher, so `_signals` and
        `_signals_at_last_tick` are pinned at 0 by construction, and neither
        thread is ever stopped before the verdict fires -- so the values are
        exact, not just present.

        Three values, not two (Task 6): the verdict now needs the wl-paste
        token to diverge on TWO CONSECUTIVE ticks -- a settled second tick
        clears instead of confirming (spec 4.2) -- so a two-value script that
        changes once and then repeats would never reach the switch at all.
        None of the assertions below are about the script's shape, so
        lengthening it changes nothing they check."""
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(clipboard)

        self.wait_until(lambda: self.switch_log_lines())
        self.quiesce(watcher)

        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")
        line = self.switch_log_lines()[0]
        self.assertIn("signals=0", line)
        self.assertIn("signals_at_last_tick=0", line)
        self.assertIn("pump_alive=True", line)
        self.assertIn("worker_alive=True", line)
        self.assertNotIn(
            "is the gnome-shell extension enabled?", line,
            "the verdict must not assert a cause it cannot know",
        )

    def test_the_verdict_lands_on_the_tick_after_the_arming_one(self):
        """The rule, pinned by WHICH tick fires it rather than by "it degrades
        eventually" -- which passes against every version of this rule the
        project has had and pins nothing.

        One diverging tick ARMS. The next tick CONFIRMS only if it ALSO
        diverges -- Task 6's settled-clears rule (spec 4.2), the EXACT
        REVERSE of the rule this docstring argued for before Task 6. That
        argument is not wrong, it is answering a different question: it is
        correct for the fast tier's input (a sparse, genuinely-dead source),
        and its own comment now says so -- see _observe_tick's predicate
        comment. On THIS tier's input a settled second tick is GPaste's own
        takeover settling back down, a known benign class, not evidence the
        source is fine; a genuinely dead tracker under an active user keeps
        diverging instead, which is what the script below now does. A
        two-value script that changes once and then settles would arm and
        then CLEAR under the new rule, forever, on a source Task 6 exists to
        stop misdiagnosing -- so it can no longer pin "lands on tick 3", only
        pin "never lands", which is a different test.

        on_degrade runs synchronously inside _observe_tick, on the poll thread,
        and clipboard.calls is incremented only by that same thread, so the read
        count at the moment of the verdict is exact with no sleep and no
        waiting: one baseline read plus two ticks, the second one diverging
        too."""
        reads_at_verdict = []
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(
            clipboard, on_degrade=lambda: reads_at_verdict.append(clipboard.calls))

        self.wait_until(lambda: reads_at_verdict)
        self.quiesce(watcher)

        self.assertEqual(
            reads_at_verdict, [3],
            "the verdict must land on the tick AFTER the arming one -- one "
            "baseline read plus two ticks, the second one diverging too",
        )

    def test_a_signal_arriving_after_the_arming_tick_clears_the_run(self):
        """Named for the PRE-TASK-6 mechanism, and that mechanism no longer
        applies: signals are not part of this tier's predicate at all now
        (spec 4.0 says so explicitly -- do not "strengthen" the predicate
        with them; see _observe_tick's own comment). This still passes, but
        for a DIFFERENT reason: ArmThenSignalClipboard's third read repeats
        the SAME value ("b") as its second, a settled token, which clears
        under Task 6's rule regardless of whether a signal also arrived
        alongside it. The in-flight-signal assertion below is kept because it
        is still a true, real property of this double's own mechanics (see
        its docstring) -- not because it is still what clears the run.

        Kept as a regression check rather than deleted or renamed outright: a
        healthy source whose signal races with the poll must still not
        degrade, whatever the mechanism, and "diverge once, then repeat" is
        also the simplest possible instance of settled-clears --
        test_a_takeover_does_not_degrade in test_watcher_uuid_tier.py is the
        same rule under a more elaborate, image-shaped input.

        Deterministic by construction: the counter moves from inside the read
        belonging to the confirming tick, on the poll thread itself, so it is
        provably in place before that tick's comparison -- unaffected by
        Task 6, which is why this assertion is untouched."""
        clipboard = ArmThenSignalClipboard(before=b"a", after=b"b")
        watcher, _ = self.start_watcher(clipboard, on_change=lambda: None)

        # The baseline, the arming tick, the tick that settles (and clears
        # the run), and two more after it.
        self.wait_until(lambda: clipboard.calls >= 5)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 5,
            "the poll must have run past the tick that would have confirmed",
        )
        self.assertEqual(
            watcher._signals, 1, "the in-flight signal must have been counted"
        )
        self.assertEqual(
            self.switch_log_lines(), [],
            "a settled token arriving after the arming tick must clear the "
            "run, not leave it to be confirmed; got: %r" % self.log_lines,
        )

    def test_a_failed_read_clears_an_armed_run(self):
        """The third reset condition. A read that failed is not evidence about
        the event source either way -- WaylandClipboard.read() returns None for
        a timed-out wl-paste and for a genuinely empty selection alike -- so it
        cannot serve as the second half of a verdict. Without that, one flaky
        wl-paste landing on the tick after a change would confirm a verdict on
        a healthy machine, which is the same false positive by another route.

        The script arms on b, then fails, so the tick that would otherwise
        confirm has nothing to confirm with."""
        clipboard = ScriptedReadClipboard([b"a", b"b", None, b"b"])
        watcher, _ = self.start_watcher(clipboard)

        self.wait_until(lambda: clipboard.calls >= 6)
        self.quiesce(watcher)

        self.assertGreaterEqual(
            clipboard.calls, 6, "the poll must have run past the recovery"
        )
        self.assertEqual(
            self.switch_log_lines(), [],
            "a failed read must clear an armed run, not confirm it; got: %r"
            % self.log_lines,
        )

    def test_a_healthy_signal_source_plus_a_content_change_does_not_log_the_switch(self):
        """THE trap in the ORIGINAL task, before Task 6. When GPaste is
        healthy the user copies something, the signal fires and is handled --
        and then the safety net's next tick ALSO sees content differing from
        its own baseline, because it keeps one. An implementation that
        concludes "the event source is dead" from a content difference alone
        logs the switch and degrades EVERY healthy installation to polling on
        the user's first copy: worse than the bug the safety net fixes, and
        entirely invisible to a test that only ever drives a dead source.

        The discriminator named here used to be the count of accepted
        signals; Task 6 replaces it with the fast tier's uuid (spec 4.0),
        and signals are now deliberately absent from the predicate
        altogether -- see _observe_tick's own comment. This test still passes
        (via settled-clears: SignallingClipboard's third read repeats the
        SAME value as its second, same shape as
        test_a_signal_arriving_after_the_arming_tick_clears_the_run above),
        so it is kept as a regression check on the healthy-source trap this
        docstring names, not because "the count of accepted signals" is still
        the mechanism."""
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
        interval changes, the mechanism does not.

        Three values, not two (Task 6): a two-value script settles after one
        change and would never reach the switch under the new settled-clears
        rule -- see test_the_verdict_reports_evidence_and_does_not_assert_a_cause's
        identical note."""
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
        # Deliberately NOT paced: this counts ticks in a 20ms window and a
        # blocking read would starve the count.
        #
        # Three values, not two (Task 6): reaching the switch itself now
        # needs the wl-paste token to diverge on two CONSECUTIVE ticks
        # (settled-clears, spec 4.2), so the script must keep changing
        # through the second tick too, not settle after the first.
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"])
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=budget,
            degraded_interval_seconds=degraded)

        self.wait_until(lambda: self.switch_log_lines())
        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")

        # From here the script is exhausted and repeats "c" forever, so every
        # tick still reads, change or not, and this count needs nothing
        # further from it -- only how many ticks it took to REACH the switch
        # changed above, not this part of the claim.
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
        # Paced so the poll's own change is observed before the count below
        # is taken -- otherwise a poll-driven change still in flight could
        # satisfy the assertion that only the emitted SIGNAL is supposed to.
        #
        # Three values, not two (Task 6): reaching the switch needs the
        # wl-paste token to diverge on two CONSECUTIVE ticks (settled-clears,
        # spec 4.2), and _observe_tick's on_tick callback receives the exact
        # same (before, current) pair PollingWatcher.pump just used to decide
        # whether to report a content change -- see pump's own docstring, the
        # single-comparison-loop paragraph. So a confirming second tick is
        # NECESSARILY also a second poll-driven content change; there is no
        # way to make the verdict diverge twice without the poll ALSO
        # reporting two changes, and pacing only forces the ordering BETWEEN
        # a change being taken and the next probe -- not between the second
        # tick's own on_tick (synchronous, on the poll thread) and the
        # worker's processing of that same tick's change (a separate thread
        # hop). Waiting for the switch and then asserting `== 1` would
        # therefore race the worker instead of testing anything about this
        # test's actual subject.
        clipboard = ScriptedReadClipboard([b"a", b"b", b"c"], paced=True)
        watcher, fake_process = self.start_watcher(clipboard)

        self.wait_until(lambda: self.switch_log_lines())
        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")
        self.assertFalse(
            fake_process.terminated,
            "the subscription must be left alive to recover on its own",
        )

        # The script is exhausted after "c" and repeats it forever, so the
        # poll cannot add a THIRD change here: only the emitted signal can.
        # Both poll-driven changes must have been OBSERVED first, or the
        # count below could be satisfied by one of them landing late --
        # waiting for >= 2 (not just "some") and then asserting the exact
        # values is what makes this deterministic rather than a race won
        # early.
        self.wait_until(lambda: len(self.changes) >= 2)
        self.assertEqual(
            self.changes, [b"b", b"c"], "both poll-driven changes must be in")
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
        spend up to SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT = 13s in its
    clipboard read -- is a safety net that has
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


