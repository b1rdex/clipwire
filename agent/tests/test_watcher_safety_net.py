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
        # THE ASSERTION ABOVE WAS VACUOUS UNTIL TASK 12, and saying so is the
        # point: the line it forbids ("is the gnome-shell extension enabled?")
        # is the v2 wording, which this file replaced in the same commit that
        # added the assertion -- while the REPLACEMENT went on saying "the
        # gnome-shell extension being disabled is one possible cause", a
        # different sentence making the identical unknowable claim, and this
        # test stayed green through all of it. Spec 5.3 counted that as the
        # project's third shipped guess. Matching the SUBSTRING both wordings
        # share is what makes the check bite on the claim rather than on one
        # phrasing of it.
        self.assertNotIn(
            "gnome-shell extension", line,
            "the verdict named a cause measured false in all three incidents",
        )
        self.assertIn(
            "gpaste_Active=", line,
            "spec 5.3 replaces the guess with a READING -- an observed value, "
            "or an honest 'unavailable', never a cause invented at the point "
            "of reporting",
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
        extension for it.

        WHAT REACHES THE VERDICT CHANGED UNDER v3.3's SPEC 5.1 RULE, and
        this test now passes for a different reason than it used to. It
        used to be that both the failure (a->None) and the recovery
        (None->a) looked like changes to the comparison, and the point was
        that neither may be read as a missed change. On the composed
        watcher this builds, neither is READ AS A CHANGE by the comparison:
        the failing tick is `unresolved`, so the comparison is never
        evaluated for it at all -- it holds the baseline and observes
        nothing -- while the recovery tick DOES evaluate it, comparing b"a"
        against a still-held b"a", gets False, and signals through
        `recovered` instead. This said "neither reaches the comparison at
        all", which its own next sentence then contradicted by describing
        what the recovery tick compares: `current != previous or recovered`
        evaluates its left operand first, so on that tick the comparison is
        reached and merely yields False. The
        assertion below is unchanged and still the one that matters -- a
        failed read and its recovery must produce no verdict -- so this is
        kept as the regression check it always was, now covering the
        held-baseline path rather than the None-transition one."""
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
        identical note.

        A script that keeps MOVING, for the same reason as
        test_after_switching_the_loop_actually_ticks_at_the_degraded_rate
        below (Task 9), and it makes this equality DETERMINISTIC rather than
        merely usually right. With a settled script an idle tick landing
        between the switch log appearing and quiesce() would back the interval
        off to 0.1 and fail this assertion; that needs a ~50ms stall of this
        thread against a 1ms wait_until loop, and it never fired in 540 runs
        under parallel load -- but "rare" is not "impossible", and the fix
        costs one line and weakens nothing, since every post-switch tick then
        takes the reset branch and re-writes 0.05."""
        clipboard = ScriptedReadClipboard([b"v%d" % i for i in range(64)])
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
        #
        # A script that keeps MOVING for the whole window, not three values
        # and then "c" forever (Task 9). This test's premise was that the
        # degraded rate is FLAT, so three ticks cost 3 x degraded; spec 6.2's
        # backoff makes that true only while ticks keep observing changes --
        # a settled token doubles the interval, and three ticks then cost
        # 7 x degraded from the floor, or 28 x if `at_switch` is sampled a
        # tick or two late. MEASURED, not predicted: the old script failed
        # this assertion 7 times in 160 runs under parallel load, and 0 in
        # 180 at the previous commit. So the fix restores the premise instead
        # of widening the window -- window must stay under `budget` for "a
        # loop still on the budget delivers none" to hold, which leaves no
        # room to widen. An always-moving clipboard is also the degraded
        # state that actually matters: the floor exists for a user who is
        # copying, and it is only an idle one that is supposed to back off.
        clipboard = ScriptedReadClipboard([b"v%d" % i for i in range(64)])
        watcher, _ = self.start_watcher(
            clipboard, safety_net_interval_seconds=budget,
            degraded_interval_seconds=degraded)

        self.wait_until(lambda: self.switch_log_lines())
        self.assertEqual(len(self.switch_log_lines()), 1, "the switch must have happened")

        # From here every tick still reads and still sees a NEW value, so the
        # backoff resets on each one and the rate stays the floor -- which is
        # what this count is about. Only how many ticks it took to REACH the
        # switch changed above, not this part of the claim.
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


class TestDegradedBackoff(unittest.TestCase):
    """Spec 6.2: degraded mode is the ONE state where a wl-paste interval is
    still a focus-steal knob, and a flat rate there is the measured pain this
    whole release exists to remove -- `wl-paste --list-types` blinks the
    foreground app (spec 1's blind A/B) and probe() runs on EVERY tick, so the
    blink rate IS the poll rate (spec 1.1).

    Placed HERE rather than in test_watcher_uuid_tier.py's TestSlowTierVerdict,
    and the split is by SUBJECT rather than by which function is called: that
    class owns _observe_tick's VERDICT (spec 4.0/4.0.1/4.2 -- does the
    predicate arm, clear, confirm), this one owns the poll INTERVAL, which is
    what every other _safety_net.interval assertion in the suite already lives
    beside (TestGPasteSafetyNet above: the production defaults, the switch's
    interval, the already-degraded watcher's). The brief named
    test_watcher.py, which is stale twice over -- v3.2.1's split moved
    _observe_tick's tests out of it, and it contains zero _observe_tick
    references today; see task-6-report.md, which settled the same question
    for the verdict half.

    Its own class rather than more methods on TestGPasteSafetyNet: every test
    below drives _observe_tick DIRECTLY on a watcher that is never started, so
    no thread, no gdbus, no clipboard and no timing is involved, and an
    interval assertion cannot be raced by a poll tick landing between the
    wait and the assertion. TestGPasteSafetyNet's own fixture starts threads
    for all of its tests, which is what those tests are about and what these
    deliberately are not."""

    def setUp(self):
        """The verdict path logs, and one test below reads that line back. A
        watcher that is never started still reaches _observe_tick's verdict
        when it is called by hand, so the patch is needed for every test here
        whether or not it asserts on the output -- otherwise a stray verdict
        line prints to the real log during an unrelated test run."""
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def degraded_watcher(self):
        """A connection already past spec 6.2's verdict, at the production
        constants: an already-degraded watcher comes up ON the degraded
        interval (GPasteWatcher.__init__'s own ternary), which is the floor
        this backoff resets to.

        clipboard=None is safe and deliberate, following
        test_production_defaults_are_a_thirty_second_budget_and_a_one_second_degraded_poll:
        nothing here is ever started, and _observe_tick only ever compares the
        two TOKENS it is handed -- it never touches the clipboard."""
        watcher = GPasteWatcher(clipboard=None, degraded=True)
        self.addCleanup(watcher.stop)
        return watcher

    def test_an_idle_degraded_connection_backs_off_and_a_change_resets_it(self):
        """The task, in one script. Exact equalities rather than the
        assertGreater the brief asked for: "greater than the floor" passes
        against a x1.5 backoff, against a backoff that jumps straight to the
        cap, and against one that adds a constant -- three different rates,
        one assertion, no way to tell them apart. The doubling is the shape
        spec 6.2 names, so the shape is what is pinned."""
        watcher = self.degraded_watcher()
        self.assertEqual(
            watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
            "an already-degraded watcher must start on the floor")

        watcher._observe_tick(("text", "a"), ("text", "a"))
        self.assertEqual(
            watcher._safety_net.interval, 2 * DEGRADED_POLL_SECONDS,
            "an idle degraded connection kept blinking at the floor rate")
        watcher._observe_tick(("text", "a"), ("text", "a"))
        self.assertEqual(
            watcher._safety_net.interval, 4 * DEGRADED_POLL_SECONDS,
            "the backoff stopped after one step instead of compounding")

        watcher._observe_tick(("text", "a"), ("text", "b"))
        self.assertEqual(
            watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
            "a real change did not restore responsiveness")

    def test_the_backoff_stops_at_the_detection_budget(self):
        """The cap, and it is SAFETY_NET_POLL_SECONDS the CONSTANT -- the same
        rule and the same constant Task 8's retry backoff uses for the same
        wait, so the two claims on this loop cannot drift onto different
        ceilings. 20 idle ticks is far past the 6 that reach it from the
        production floor (1 -> 2 -> 4 -> 8 -> 16 -> 30), so this pins "never
        exceeds" rather than "reaches"."""
        watcher = self.degraded_watcher()
        for _ in range(20):
            watcher._observe_tick(("text", "a"), ("text", "a"))
        self.assertEqual(
            watcher._safety_net.interval, SAFETY_NET_POLL_SECONDS,
            "an uncapped doubling would leave a degraded connection polling "
            "once a fortnight")

    def test_a_tick_that_read_nothing_neither_resets_nor_backs_off(self):
        """A failed probe is NOT an observed change, and the brief's snippet
        (`if previous != current`) read it as one. pump's own comment says why
        in as many words: "`before` is passed rather than a bare `changed`
        flag so the observer can tell a real change from a failed read and its
        recovery -- both are `!=` here, and neither involves a selection
        change at all."

        Both halves of read_ok are exercised, and they belong to TWO DIFFERENT
        SHAPES rather than to two ticks of one hang -- no single hang produces
        both, and an earlier revision of this docstring said it did while its
        own next two sentences said otherwise.

          - (held token, None) is the hang. probe() returns None for a
            wl-paste that timed out -- a powered-off monitor does exactly
            that, spec 1.4 -- and spec 5.1 holds pump's baseline across the
            run, so every tick of one arrives in this shape.
          - (None, token) is NOT a hang: `unresolved` is `current is None`,
            so pump treats this tick as a full change, signals it and
            advances its baseline. It is the first tick to RESOLVE after a
            baseline probe that found nothing -- an empty clipboard at
            connect, or a wl-paste already hanging then, in which case any
            number of unresolved ticks come between the two.

        The second call below therefore pins the GUARD rather than
        reproducing a production state, the same convention
        test_no_verdict_while_the_uuid_is_unknown in test_watcher_uuid_tier.py
        uses for the identical reason: in production a (None, token) tick can
        only be the first RESOLVED tick -- not the first loop tick, which an
        earlier revision of this docstring said and the hang-at-connect case
        above disproves. (pump's `previous = current` sits in its resolved
        branch, so `previous` holds the baseline's None across however many
        unresolved ticks follow and can never be put back to None once it
        advances.) A degraded watcher is still on the floor there, so freezing
        and resetting would write the same number and the guard would be
        invisible. Backing the interval off first is what makes the two
        outcomes distinguishable at all.

        Frozen, not doubled. For the hang half that is pump's own rule ("AND
        NOTHING ELSE ON THIS TICK"); for the other half it is that read_ok is
        used WHOLE, so the rate and the verdict agree about which ticks are
        evidence. It is also what keeps pump's precedence paragraph true --
        that argument turns on this write happening only on RESOLVED ticks,
        which are exactly the ticks where self._retry_interval has just been
        cleared."""
        watcher = self.degraded_watcher()
        watcher._observe_tick(("text", "a"), ("text", "a"))
        self.assertEqual(watcher._safety_net.interval, 2 * DEGRADED_POLL_SECONDS,
                         "test precondition: one idle tick must have backed off")

        watcher._observe_tick(("text", "a"), None)
        self.assertEqual(
            watcher._safety_net.interval, 2 * DEGRADED_POLL_SECONDS,
            "a probe that answered nothing was read as an observed change")

        watcher._observe_tick(None, ("text", "a"))
        self.assertEqual(
            watcher._safety_net.interval, 2 * DEGRADED_POLL_SECONDS,
            "a probe recovering from nothing was read as an observed change")

    def test_a_healthy_connection_keeps_its_detection_budget(self):
        """The gate. This backoff belongs to spec 6.2 alone -- 6.1 ("signal
        path silent, tracking alive") is explicitly not a degraded state and
        changes no interval, and a healthy connection's 30s is a DETECTION
        budget rather than a rate anything is allowed to retune.

        The CHANGE tick is what catches a deleted `self._degraded` gate, not
        the idle one: doubling from the 30s budget is inert by construction
        (min() of two thirty-second figures), exactly as Task 8's retry
        backoff is at the same rate, so an ungated backoff would look
        identical on an idle tick and only betray itself by resetting a
        healthy poller down to the degraded floor."""
        watcher = GPasteWatcher(clipboard=None)   # never started, never degraded
        self.addCleanup(watcher.stop)
        self.assertEqual(watcher._safety_net.interval, SAFETY_NET_POLL_SECONDS,
                         "test precondition: a healthy watcher polls the budget")

        watcher._observe_tick(("text", "a"), ("text", "a"))
        watcher._observe_tick(("text", "a"), ("text", "b"))
        self.assertFalse(watcher._degraded,
                         "test precondition: no verdict may have been reached")
        self.assertEqual(
            watcher._safety_net.interval, SAFETY_NET_POLL_SECONDS,
            "the degraded backoff retuned a connection that is not degraded")

    def test_the_verdict_tick_lands_on_the_floor_its_own_log_line_promises(self):
        """The seam between the two writers of this field inside ONE tick:
        _observe_tick's degrade latch assigns the floor, and the backoff below
        it runs on that same tick. The latch is reached only when `confirmed`
        is true, and `confirmed` is `token_moved`, so the backoff necessarily
        takes its reset branch and re-writes the same floor -- but nothing
        about the two lines says so on its face, and a backoff that doubled
        here would leave the connection polling at twice the rate the line it
        just logged promises.

        The log line is read back for the same reason: this project's
        single most-recorded defect is a correct line paired with prose that
        overstates it, and spec 8 names this very line ("the verdict line's
        wording") as one the release invalidates. %g renders the production
        constants as "1" and "30"."""
        watcher = GPasteWatcher(clipboard=None)
        self.addCleanup(watcher.stop)
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"

        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        watcher._observe_tick(("text", "b"), ("text", "c"))   # confirms
        self.assertTrue(watcher._degraded,
                        "test precondition: the verdict must have fired")

        self.assertEqual(
            watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
            "the verdict tick must leave the poll on the floor, not one "
            "backoff step above it")
        line = [l for l in self.log_lines if "reported no clipboard change" in l][0]
        self.assertIn("every 1s", line)
        self.assertIn("at most 30s", line)



class TestDivergenceReprobe(unittest.TestCase):
    """Spec 4.2: "on divergence, re-probe within seconds instead of waiting a
    full slow interval. A takeover settles; a dead tracker persists."

    PLACED HERE, beside TestDegradedBackoff, and by the same rule that class
    states for itself: the split is by SUBJECT, and this one's subject is the
    POLL INTERVAL. TestSlowTierVerdict in test_watcher_uuid_tier.py owns
    whether the predicate arms, clears and confirms; every _safety_net.interval
    assertion in this suite lives in this file. The re-probe is the FIFTH
    claimant on that interval and the second one written by _observe_tick, so
    its tests belong next to the other one's.

    Every test drives _observe_tick DIRECTLY on a watcher that is never
    started -- no thread, no gdbus, no clipboard, no timing -- so an interval
    assertion cannot be raced by a poll tick landing between two lines.

    The intervals are passed EXPLICITLY rather than left at the production
    constants because the call site takes min() with whatever the safety net
    is on: a test at this suite's usual 0.01s would make every re-probe a
    silent no-op and pin nothing at all."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def watcher(self, slow=30.0, reprobe=10.0, degraded=False):
        watcher = GPasteWatcher(clipboard=None,
                                safety_net_interval_seconds=slow,
                                reprobe_interval_seconds=reprobe,
                                degraded=degraded)
        self.addCleanup(watcher.stop)
        # The uuid pair primed to one value, the steady state every slow-tier
        # test in this suite starts from: "frozen" is what makes a moving
        # wl-paste token a DIVERGENCE rather than an ordinary copy. See
        # TestGPasteSafetyNet.start_watcher for the full reasoning.
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"
        return watcher

    def test_a_divergence_re_probes_within_seconds(self):
        watcher = self.watcher()
        self.assertEqual(watcher._safety_net.interval, 30.0,
                         "test precondition: the tier starts on its slow rate")

        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms

        self.assertTrue(watcher._armed, "test precondition: the run must be armed")
        self.assertEqual(
            watcher._safety_net.interval, 10.0,
            "an armed run waited a full slow interval to find out whether the "
            "token settled -- at spec 4.2's eventual 5-15 minutes that is the "
            "difference between a diagnosis and a shrug")

    def test_a_settled_token_gives_the_interval_back(self):
        """The excursion is ONE-SHOT. A takeover settles, the run clears, and
        the tier returns to its own rate rather than staying fast forever --
        which on the composed slow tier would be a permanent focus-steal rate
        bought with one benign re-offer."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        watcher._observe_tick(("text", "b"), ("text", "b"))   # settles, clears

        self.assertFalse(watcher._armed, "test precondition: the run must have cleared")
        self.assertEqual(watcher._safety_net.interval, 30.0,
                         "the re-probe rate outlived the run that justified it")

    def test_it_gives_back_what_it_TOOK_not_what_it_was_built_with(self):
        """The interleaving the snapshot exists for: spec 4.3's fallback
        writes SAFETY_NET_POLL_SECONDS from the FAST-TIER thread, and a
        re-probe that restored a remembered __init__ figure would silently
        undo that write. What was taken is what comes back."""
        watcher = self.watcher(slow=5.0, reprobe=1.0)
        watcher._safety_net.interval = SAFETY_NET_POLL_SECONDS   # as _fast_tick's fallback does

        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        self.assertEqual(watcher._safety_net.interval, 1.0)
        watcher._observe_tick(("text", "b"), ("text", "b"))   # clears

        self.assertEqual(
            watcher._safety_net.interval, SAFETY_NET_POLL_SECONDS,
            "the give-back reverted a write this block never made")

    def test_it_can_only_make_the_tier_faster(self):
        """min(), not assignment, and the inversion it guards is REACHABLE
        rather than theoretical: every test in this project builds the safety
        net well below any sane re-probe rate, so a bare assignment would ship
        a "re-probe" that slowed the tier by orders of magnitude -- in the
        tests, silently, which is where it would never be noticed."""
        watcher = self.watcher(slow=0.01, reprobe=10.0)

        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms

        self.assertEqual(
            watcher._safety_net.interval, 0.01,
            "the re-probe made the tier 1000x SLOWER than the rate it was "
            "already polling at")

    def test_a_confirmed_run_leaves_the_degraded_floor_not_the_re_probe_rate(self):
        """The seam between the fifth claimant and spec 6.2's two, inside ONE
        tick. The latch sets self._degraded BEFORE this block runs, so the
        re-probe stands down on exactly the tick its question was answered --
        and the connection is left on the floor its own log line promises,
        not on a re-probe rate nothing announced."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms, re-probes
        watcher._observe_tick(("text", "b"), ("text", "c"))   # confirms

        self.assertTrue(watcher._degraded, "test precondition: the verdict must have fired")
        self.assertEqual(
            watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
            "the degraded state is the more specific one and must win the wait")

    def test_the_idle_gate_stands_down_once_the_connection_is_degraded(self):
        """Spec 6.2's loop is the connection's LAST change detector, and
        `pump` is shared with the slow tier, so the gate reaches it -- and
        make_watcher wires the gate on the already-degraded path too.

        THREE THINGS ARE PINNED, because the first alone would pass against a
        gate scoped at construction time and this state ARRIVES MID-
        CONNECTION:

          1. a watcher built already degraded never consults the gate;
          2. a watcher that degrades DURING the connection stops consulting it
             from that tick on -- which a constructor-time scoping cannot do;
          3. the gate is not merely ignored but never CALLED, since the call
             itself costs 103 ms and would be ~10% duty cycle on a 1 s loop.

        The measurement behind the decision, and the argument that settles it,
        are in _slow_tier_should_probe's own docstring: a gated tick skips
        _on_tick, so spec 6.2's backoff never runs, so an idle degraded
        connection stays PINNED at the floor instead of doubling away from it
        -- the gate defeating the mechanism Task 9 built for this exact
        state."""
        calls = []
        watcher = GPasteWatcher(clipboard=None, degraded=True,
                                read_idle_gate=lambda: calls.append(True) or False)
        self.addCleanup(watcher.stop)
        self.assertTrue(
            watcher._slow_tier_should_probe(),
            "a degraded connection's only change detector must not be gated")
        self.assertEqual(
            calls, [],
            "the gate must not even be ASKED once degraded: the call costs "
            "103 ms, which is ~10% duty cycle on the 1s degraded loop")

        mid = GPasteWatcher(clipboard=None,
                            read_idle_gate=lambda: calls.append(True) or False)
        self.addCleanup(mid.stop)
        mid._last_uuid = "frozen"
        mid._uuid_at_last_tick = "frozen"
        self.assertFalse(
            mid._slow_tier_should_probe(),
            "test precondition: an idle user gates a HEALTHY connection out")
        mid._observe_tick(("t", "a"), ("t", "b"))    # arms
        mid._observe_tick(("t", "b"), ("t", "c"))    # confirms -> degraded
        self.assertTrue(mid._degraded, "test precondition: the verdict must have fired")

        before = len(calls)
        self.assertTrue(
            mid._slow_tier_should_probe(),
            "the gate must stand down the moment the verdict lands, not only "
            "for watchers BUILT degraded -- the state arrives mid-connection")
        self.assertEqual(len(calls), before,
                         "and it must stop calling the gate, not just ignore it")

    def test_a_degraded_loop_gated_by_an_idle_user_still_backs_off(self):
        """The consequence the scoping exists for, driven through the REAL
        poll loop rather than through the predicate. Spec 6.2's backoff runs
        at the bottom of _observe_tick, and _observe_tick runs only from
        _on_tick, which a gated tick skips -- so a gate that reached this loop
        would pin an idle degraded connection at its floor forever, paying a
        103 ms gate call every tick to sync nothing.

        Asserted as "the interval moved off the floor", not as an exact value:
        the number depends on how many ticks fit in the sleep, and pinning
        that would be a timing test. The doubling itself already has an exact
        test in TestDegradedBackoff."""
        class Clip:
            def __init__(self): self.calls = 0
            def probe(self): self.calls += 1; return ("t", "a")
            read = probe

        watcher = GPasteWatcher(clipboard=None, degraded=True,
                                degraded_interval_seconds=0.005,
                                read_idle_gate=lambda: False)   # user long idle
        self.addCleanup(watcher.stop)
        clipboard = Clip()
        watcher._safety_net.clipboard = clipboard
        watcher._safety_net.start(lambda: None)
        time.sleep(0.25)
        watcher._safety_net.stop()

        self.assertGreater(
            clipboard.calls, 1,
            "an idle user silenced the connection's only change detector")
        self.assertGreater(
            watcher._safety_net.interval, 0.005,
            "the degraded backoff never ran: a gated tick skips _on_tick, so "
            "the loop stays pinned at the floor for as long as the user is "
            "idle -- polling the gate, syncing nothing")

    def test_an_already_degraded_watcher_never_re_probes(self):
        """`not self._degraded`, the same precedence rule spec 4.3's fallback
        uses. It costs nothing to defer: DEGRADED_POLL_SECONDS is already
        faster than any re-probe rate, so a re-probe there could only ever
        slow the connection's only remaining sync down."""
        watcher = self.watcher(slow=30.0, reprobe=10.0, degraded=True)
        self.assertEqual(watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
                         "test precondition: it comes up on the floor")

        watcher._observe_tick(("text", "a"), ("text", "b"))   # would arm

        self.assertTrue(watcher._armed, "test precondition: the run still arms")
        self.assertEqual(
            watcher._safety_net.interval, DEGRADED_POLL_SECONDS,
            "spec 6.2's backoff resets to the floor on a moved token, and the "
            "re-probe must not overwrite it")
        self.assertIsNone(watcher._interval_before_reprobe,
                          "no excursion may even have been recorded")

    def test_a_failed_probe_clears_the_run_and_the_re_probe_with_it(self):
        """Spec 5.1's retry and this re-probe can never be in force together,
        and the exclusion is structural rather than ordered: an unresolved
        tick is read_ok False, which clears self._armed on the same tick that
        starts the retry run."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        self.assertEqual(watcher._safety_net.interval, 10.0)

        watcher._observe_tick(("text", "b"), None)            # a hung wl-paste

        self.assertFalse(watcher._armed)
        self.assertEqual(watcher._safety_net.interval, 30.0,
                         "a run that can no longer be confirmed kept polling fast")

    def test_an_excursion_is_never_in_progress_while_a_run_is_still_armed(self):
        """THE INVARIANT THAT MAKES TWO GUARDS REDUNDANT, and it is pinned
        rather than the guards, because a mutation run proved the guards
        cannot be pinned: deleting either the `is None` on the take or the
        `not self._armed` on the give-back leaves the whole suite green, and
        no test could do better -- both are EQUIVALENT MUTANTS today, not
        holes. Reported in task-12-report.md rather than patched.

        What they are redundant BY is this: _observe_tick can only leave
        self._armed True while undegraded through the `else` branch, which is
        reached only when self._armed was FALSE on entry -- so no excursion
        can be in progress. Established by driving the real _observe_tick over
        its whole reachable state space and recording the pair AS THE BLOCK
        SEES IT (8192 runs, four ticks each); the state never occurs. This
        test is the cheap standing version of that sweep.

        So the guards are defensive, and this is what would go red if a later
        edit made the state reachable -- at which point they stop being
        redundant and start deciding, which is exactly when someone needs to
        know."""
        tokens = [(("t", "a"), ("t", "a")), (("t", "a"), ("t", "b")),
                  (("t", "a"), None), (None, ("t", "a"))]
        for degraded in (False, True):
            for first in tokens:
                for second in tokens:
                    for third in tokens:
                        watcher = self.watcher(degraded=degraded)
                        for tick in (first, second, third):
                            # THE PAIR AS THE BLOCK SEES IT, which is not the
                            # pair left after the tick: self._armed is set by
                            # THIS tick's decision, self._interval_before_
                            # reprobe by the PREVIOUS tick's block. Reading
                            # both afterwards would assert on the state the
                            # block just WROTE -- (armed, excursion) is the
                            # normal, correct outcome of an arming tick -- and
                            # would fail against correct code, which is how
                            # this test's first revision failed.
                            excursion_before = (
                                watcher._interval_before_reprobe is not None)
                            watcher._observe_tick(*tick)
                            if not watcher._degraded:
                                self.assertFalse(
                                    watcher._armed and excursion_before,
                                    "an armed run with an excursion already in "
                                    "progress reaches the re-probe block, where "
                                    "two guards that never decided anything now "
                                    "do: %r" % (tick,))

    def test_a_healthy_connection_that_never_diverges_keeps_its_budget(self):
        """The gate on the whole block. A tick that arms nothing must leave
        the detection budget alone -- a re-probe fired on every tick would be
        a permanent focus-steal rate on a machine with nothing wrong."""
        watcher = self.watcher()
        watcher._last_uuid = "moved"        # the uuid tracked the copy: healthy

        watcher._observe_tick(("text", "a"), ("text", "b"))

        self.assertFalse(watcher._armed, "test precondition: nothing may arm")
        self.assertEqual(watcher._safety_net.interval, 30.0,
                         "a healthy connection was retuned by the re-probe")


class TestUntrackedChangeSignal(unittest.TestCase):
    """v3.3 §4.2's excluded-clip contract -- "the slow tier always feeds the
    worker, so clips GPaste fails to track still sync at the slow cadence" --
    kept alive through v3.4's read guard.

    When the wl-paste token moves while GPaste's history uuid stands still,
    GPaste did not record that clip: an excluded app, a password manager, its
    own image re-offer. The history TOP is then somebody else's older clip,
    so a tier read would answer with the wrong content rather than with
    nothing -- either swallowed as an echo of what was already sent, or sent
    as a stale clip. Only wl-paste can see the real selection. So the tick
    raises a one-shot flag and Agent._tier_read declines the tier on it.

    PLACED HERE beside TestDivergenceReprobe, by that class's own rule: the
    split is by SUBJECT, and _observe_tick's non-verdict OUTPUTS live in this
    file, while TestSlowTierVerdict (test_watcher_uuid_tier.py) owns whether
    the degrade verdict itself arms, clears and confirms. That shard was
    considered -- the flag's condition IS the uuid-vs-token divergence it
    owns -- but this flag decides nothing about the verdict, and no test
    below reads _degraded or an interval.

    Every test drives _observe_tick DIRECTLY on a watcher that is never
    started -- no thread, no gdbus, no clipboard -- so the flag's reading
    cannot be raced by a poll tick landing between two lines."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def watcher(self, uuid="frozen", uuid_at_last_tick="frozen"):
        watcher = GPasteWatcher(clipboard=None, safety_net_interval_seconds=30.0)
        self.addCleanup(watcher.stop)
        # Both fields primed, and by default to the SAME value: a brand-new
        # watcher can never see uuid_frozen True on its first tick, because
        # "frozen" is a DELTA against _uuid_at_last_tick and not a presence
        # check. See TestGPasteSafetyNet.start_watcher for the full reasoning.
        watcher._last_uuid = uuid
        watcher._uuid_at_last_tick = uuid_at_last_tick
        return watcher

    def test_a_moved_token_over_a_frozen_uuid_raises_the_flag(self):
        """THE CONTRACT. GPaste recorded nothing, so the read guard must not
        be allowed to answer this observation out of GPaste's history."""
        watcher = self.watcher()

        watcher._observe_tick(("text", "a"), ("text", "b"))

        self.assertTrue(
            watcher.consume_untracked_change(),
            "an unrecorded clip would be answered from the OLD history top: "
            "either suppressed as an echo and silently lost, or sent stale")

    def test_the_flag_is_one_shot(self):
        """Spent on the observation it belongs to. A flag that stayed set
        would put EVERY later copy back on wl-paste -- every one of them a
        blink v3.4 exists to remove."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "b"))

        self.assertTrue(watcher.consume_untracked_change())
        self.assertFalse(watcher.consume_untracked_change(),
                         "the one-shot outlived the observation that raised it")

    def test_a_uuid_that_moved_with_the_token_raises_nothing(self):
        """The ordinary tracked copy, which is the whole point of v3.4: GPaste
        recorded it, so the tier holds exactly this clip and must serve it."""
        watcher = self.watcher(uuid="moved", uuid_at_last_tick="frozen")

        watcher._observe_tick(("text", "a"), ("text", "b"))

        self.assertFalse(watcher.consume_untracked_change(),
                         "a tracked copy was pushed back onto wl-paste")

    def test_an_unreadable_probe_raises_nothing(self):
        """A hung or failed wl-paste is not evidence that anything moved: the
        token pair is unknown, not different."""
        for previous, current in ((("text", "a"), None), (None, ("text", "b"))):
            with self.subTest(previous=previous, current=current):
                watcher = self.watcher()

                watcher._observe_tick(previous, current)

                self.assertFalse(watcher.consume_untracked_change())

    def test_a_settled_token_raises_nothing(self):
        watcher = self.watcher()

        watcher._observe_tick(("text", "a"), ("text", "a"))

        self.assertFalse(watcher.consume_untracked_change(),
                         "a quiet tick raised an untracked-clip signal")

    def test_a_fresh_watcher_has_nothing_to_consume(self):
        watcher = GPasteWatcher(clipboard=None)
        self.addCleanup(watcher.stop)
        self.assertFalse(watcher.consume_untracked_change())


class TestVerdictNamesTheCause(unittest.TestCase):
    """Spec 5.3: "read GPaste's tracking-state property over D-Bus and put the
    observed value in the log line. The current verdict line blames a disabled
    gnome-shell extension; the extension was measured enabled and ACTIVE
    during all three incidents. That makes three shipped guesses."

    THE FIELD IS `gpaste_Active=`. Spec 5.3 said `Tracking` until it was
    corrected against the target machine: GPaste 45.3 has `Track(b)` as a
    METHOD and `Active` as the boolean property, and no `Tracking` property at
    all. The literal spelling is asserted below rather than derived from
    GPASTE_TRACKING_PROPERTY, because this is USER-VISIBLE OUTPUT -- a
    derived assertion would follow the constant silently through a rename and
    pin nothing about what production actually prints. The constant's VALUE
    gets its own test, so the pair covers both "the field says what we think"
    and "we are asking for the property that exists".

    TestGPasteSafetyNet above already pins that the line carries evidence and
    asserts no cause; this class pins what REPLACED the guess. Separate from
    that class because these need no threads: the verdict is reached by
    calling _observe_tick directly, so the reading is exact and the gdbus call
    is a stub rather than a race."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def verdict_line(self, read_tracking=None):
        watcher = GPasteWatcher(clipboard=None, read_tracking=read_tracking)
        self.addCleanup(watcher.stop)
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"
        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        watcher._observe_tick(("text", "b"), ("text", "c"))   # confirms
        lines = [l for l in self.log_lines if "reported no clipboard change" in l]
        self.assertEqual(len(lines), 1, "the verdict must have fired exactly once")
        return lines[0]

    def test_a_tracking_daemon_is_reported_as_tracking(self):
        line = self.verdict_line(read_tracking=lambda: True)
        self.assertIn("gpaste_Active=true", line)

    def test_a_daemon_that_stopped_tracking_is_reported_as_such(self):
        """The reading this release was written to be able to make. Three
        production incidents were diagnosed from a line that guessed instead."""
        line = self.verdict_line(read_tracking=lambda: False)
        self.assertIn("gpaste_Active=false", line)

    def test_a_read_that_failed_is_reported_as_unavailable_and_never_as_false(self):
        """"We could not ask" is DIFFERENT EVIDENCE from "GPaste says no", and
        a line that rendered None as false would be a fourth shipped guess in
        the place the third one was removed from."""
        line = self.verdict_line(read_tracking=lambda: None)
        self.assertIn("gpaste_Active=unavailable", line)
        self.assertNotIn("gpaste_Active=false", line)

    def test_the_property_asked_for_is_the_one_that_exists(self):
        """MEASURED, 2026-08-04, on the target machine running GPaste 45.3:

            gdbus introspect --session --dest org.gnome.GPaste \
                             --object-path /org/gnome/GPaste
              interface org.gnome.GPaste2 {
                  Track(in  b tracking-state);    <- a METHOD
                  readonly b Active = true;       <- the boolean PROPERTY
              }
            Properties.Get org.gnome.GPaste2 Active   -> (<true>,)  exit 0
            Properties.Get org.gnome.GPaste2 Tracking -> exit 1, empty stdout,
              InvalidArgs: No such property "Tracking"

        Spec 5.3 named `Tracking` until that measurement corrected it. Pinned
        here as a VALUE rather than left to the constant's comment, because a
        comment cannot go red: reverting this constant to the spec's original
        word would make every production verdict report the field unavailable
        forever, and nothing else in this suite would notice -- the deliverable
        would be inert, which is the failure shape spec 5.3 exists to end.

        The interface is asserted too: the bus name is org.gnome.GPaste and
        the interface is org.gnome.GPaste2, a confusion this file has already
        shipped once in the other direction (see GPASTE_INTERFACE)."""
        self.assertEqual(
            clipwire_agent.GPASTE_TRACKING_PROPERTY, "Active",
            "GPaste 45.3 has no `Tracking` property: asking for it exits 1 and "
            "the verdict reports the field unavailable on every machine")
        self.assertEqual(clipwire_agent.GPASTE_INTERFACE, "org.gnome.GPaste2")
        self.assertEqual(clipwire_agent.GPASTE_BUS_NAME, "org.gnome.GPaste")

    def test_the_property_name_is_in_the_line(self):
        """THE NAME IS MEASURED -- see test_the_property_asked_for_is_the_one_
        that_exists thirty lines above, and GPASTE_TRACKING_PROPERTY's own
        comment. This docstring used to say it "could not be verified against
        a live GPaste at authoring time", which was true of the round that
        wrote it and false of the round that shipped it: the introspection
        landed in the same commit as the sentence denying it.

        The line still carries the property name, and the reason is now a
        forward one rather than an open question: GPaste renames things
        between releases (this file already carries the GPaste/GPaste2 bus-vs-
        interface confusion for the same reason), so a future `unavailable`
        stays legible as "we asked for something this GPaste does not have"
        instead of being indistinguishable from "GPaste would not answer"."""
        line = self.verdict_line(read_tracking=lambda: True)
        self.assertIn(clipwire_agent.GPASTE_TRACKING_PROPERTY, line)

    def test_the_guess_is_gone(self):
        line = self.verdict_line(read_tracking=lambda: True)
        self.assertNotIn("gnome-shell extension", line)
        self.assertNotIn("possible cause", line)

    def test_every_evidence_field_survives_the_replacement(self):
        """The incident reconstruction was built from these four. The
        tracking-state read is an ADDITION, and a replacement that quietly dropped one of
        them would cost more than the guess did."""
        line = self.verdict_line(read_tracking=lambda: True)
        for field in ("signals=", "signals_at_last_tick=",
                      "pump_alive=", "worker_alive="):
            self.assertIn(field, line, "the verdict lost an evidence field")

    def test_a_read_that_hangs_cannot_stop_the_verdict_being_reached(self):
        """The read is on the poll thread and can block like every other gdbus
        call in this file. self._degraded is assigned BEFORE it, so a slow
        read delays the line -- once per connection, bounded by the call's own
        timeout -- and cannot un-reach the verdict. Simulated by the extreme
        case: a reader that RAISES, which is strictly worse than one that
        hangs, since a hang eventually returns None and a raise never returns.

        AND THIS TEST PINS THE HALF-APPLIED OUTCOME TOO, rather than only the
        good half. Everything AFTER the read is skipped by a raise -- the log
        line, the interval write and on_degrade -- permanently, because the
        latch never clears. That is asserted below rather than left implied,
        because the docstring used to stop at "the verdict is reached" and a
        reader would have taken the rest for granted.

        Unreachable through production's reader: gpaste_tracking returns None
        for everything a failing gdbus can do, so only an injected reader can
        raise. Disclosed rather than wrapped -- see _observe_tick's own
        comment for why a bare `except Exception` on the verdict path was
        weighed and rejected, and for what to do instead if this ever becomes
        reachable."""
        degrades = []
        watcher = GPasteWatcher(clipboard=None,
                                read_tracking=_raise_gdbus_exploded,
                                on_degrade=lambda: degrades.append(True))
        self.addCleanup(watcher.stop)
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"
        watcher._observe_tick(("text", "a"), ("text", "b"))
        with self.assertRaises(RuntimeError):
            watcher._observe_tick(("text", "b"), ("text", "c"))

        self.assertTrue(
            watcher._degraded,
            "the verdict must be reached before the read, or a D-Bus that "
            "will not answer suppresses the diagnosis entirely")
        self.assertEqual(
            [l for l in self.log_lines if "reported no clipboard change" in l], [],
            "documented, not desired: a raise takes the log line with it")
        # SHARPER THAN "the interval write is skipped", and found by writing
        # the assertion rather than by reasoning about it: the raise happens
        # inside the verdict branch, which is ABOVE the re-probe block at the
        # bottom of _observe_tick, so that block is skipped too. The arming
        # tick had already taken the excursion, so the connection is stranded
        # on the RE-PROBE rate -- neither the detection budget it left nor the
        # degraded floor its verdict called for, and with the give-back's own
        # memory still holding, so nothing will ever hand it back.
        self.assertEqual(
            watcher._safety_net.interval,
            min(SAFETY_NET_POLL_SECONDS, clipwire_agent.DIVERGENCE_REPROBE_SECONDS),
            "documented, not desired: a raise skips the interval write AND "
            "the re-probe's give-back, stranding the poller mid-excursion")
        self.assertIsNotNone(
            watcher._interval_before_reprobe,
            "and the excursion's memory is left armed, so no later tick can "
            "return the interval either -- the latch keeps them all out")
        self.assertEqual(
            degrades, [],
            "documented, not desired, and the worst of the three: Agent never "
            "learns the verdict, so the next Wayland flap rebuilds an "
            "undegraded watcher and re-arms the whole detection budget")

    def test_the_property_is_read_only_at_the_verdict_never_on_every_tick(self):
        """A GAP A MUTATION RUN FOUND. Moving the read out of the
        `confirmed and not self._degraded` branch left the whole suite green,
        and the difference is not cosmetic: it turns one gdbus call per
        CONNECTION into one per slow tick, each one able to stall the poll
        thread for GPASTE_CALL_TIMEOUT. That is a 3s hang budget spent on
        every tick of every healthy machine, to fill in a field only the
        verdict line prints.

        Counted rather than reasoned about: ticks that arm, clear, fail and
        confirm, and the count must still be exactly one."""
        calls = []
        watcher = GPasteWatcher(clipboard=None,
                                read_tracking=lambda: calls.append(True) or True)
        self.addCleanup(watcher.stop)
        watcher._last_uuid = "frozen"
        watcher._uuid_at_last_tick = "frozen"

        watcher._observe_tick(("t", "a"), ("t", "a"))    # quiet
        watcher._observe_tick(("t", "a"), ("t", "b"))    # arms
        watcher._observe_tick(("t", "b"), ("t", "b"))    # clears
        watcher._observe_tick(("t", "b"), None)          # a failed probe
        self.assertEqual(calls, [], "the property was read before any verdict")

        watcher._observe_tick(("t", "b"), ("t", "c"))    # arms
        watcher._observe_tick(("t", "c"), ("t", "d"))    # confirms
        self.assertTrue(watcher._degraded, "test precondition: the verdict fired")
        self.assertEqual(len(calls), 1, "the verdict must read it exactly once")

        watcher._observe_tick(("t", "d"), ("t", "e"))    # more ticks, latched
        watcher._observe_tick(("t", "e"), ("t", "f"))
        self.assertEqual(
            len(calls), 1,
            "the read must be behind the same latch the log line is: it is "
            "evidence for ONE verdict, not a per-tick charge on the poll "
            "thread")

    def test_an_unwired_reader_reports_unavailable_rather_than_raising(self):
        """GPasteWatcher defaults read_tracking to None -- see its __init__ for
        why this reader's absence is allowed and read_history_uuid's is not.
        The line must degrade to "unavailable", not to a TypeError that takes
        the poll thread's whole judgement with it."""
        line = self.verdict_line(read_tracking=None)
        self.assertIn("gpaste_Active=unavailable", line)


def _raise_gdbus_exploded():
    raise RuntimeError("gdbus exploded")
