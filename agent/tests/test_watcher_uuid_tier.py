"""The v3.3 fast tier: GPaste's history uuid as the change token.

Design: docs/superpowers/specs/2026-08-03-v3.3-focus-free-detection-design.md
"""
import inspect
import os
import subprocess
import time
import unittest
from unittest import mock

import agent_under_test as agent
# agent_under_test registers the loaded module under this name in
# sys.modules (see test_watcher.py's own comment on the same import) --
# needed here only by TestFastTierIntegration, to patch clipwire_agent.log
# the same way test_watcher_polling.py's TestPollingWatcher and
# test_watcher_safety_net.py's TestGPasteSafetyNet do: `agent.log = ...`
# would patch the wrong module, since log() is resolved against
# clipwire_agent's own globals, not agent_under_test's.
import clipwire_agent

from test_watcher import FakeGPasteProcess, JOIN_TIMEOUT


class _StubClipboard:
    """Enough clipboard for a watcher to be constructed. The fast tier never
    touches it -- that is the point of the tier."""
    def probe(self):
        return None

    def read(self):
        return None


class TestIntervalInjection(unittest.TestCase):
    def setUp(self):
        """Scrub BEFORE, and restore rather than delete.

        This was a pop-only tearDown, which is the wrong half of the
        problem twice over -- found in fix round 1 by running the suite with
        both variables set in the ambient environment and watching
        test_defaults_when_unset, a test whose NAME is "when unset", fail
        with 7.0 != 5.0. A tearDown cannot make a precondition true for the
        test it follows, and popping destroys whatever the developer or CI
        harness had deliberately exported instead of putting it back.
        mock.patch.dict snapshots the whole mapping and restores it on
        cleanup, so both halves are covered for every test in this class --
        including the two below that set the variable on purpose."""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS", None)
        os.environ.pop("CLIPWIRE_SLOW_TIER_SECONDS", None)

    def test_defaults_when_unset(self):
        self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 5.0)

    def test_reads_a_float(self):
        os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = "0.05"
        self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 0.05)

    def test_junk_falls_back_to_the_default(self):
        """A typo in a harness must not silently produce a zero-second poll
        that spins a core, nor a crash on a machine where the variable was
        never meant to be read at all."""
        for junk in ("", "abc", "-1", "0"):
            os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = junk
            self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 5.0,
                             "%r was not rejected" % junk)


class TestHistoryUuidProbe(unittest.TestCase):
    def probe(self, stdout=b"", returncode=0, raises=None):
        def run(argv, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")
        return agent.gpaste_history_uuid(run=run)

    def test_parses_the_uuid_from_a_real_shaped_reply(self):
        self.assertEqual(
            self.probe(b"('ecf318fd-a295-40a8-b591-9721fcf7cbcc', 'CD-3167')\n"),
            "ecf318fd-a295-40a8-b591-9721fcf7cbcc")

    def test_a_nonzero_exit_is_not_measured(self):
        """A well-formed reply, not empty stdout: b"" independently trips the
        `len(parts) < 2` parse guard regardless of returncode, so an empty
        payload here could not tell this guard apart from that one -- fix
        round 1 finding. A real gdbus non-zero exit does not print the
        method's reply at all, but this deliberately gives it one anyway, so
        a `returncode != 0` check that gets deleted is caught HERE rather
        than only coincidentally by whatever stdout happens to be empty."""
        self.assertIsNone(self.probe(b"('would-be-uuid', 'text')\n", returncode=1))

    def test_a_timeout_is_not_measured(self):
        self.assertIsNone(self.probe(
            raises=subprocess.TimeoutExpired(["gdbus"], 3)))

    def test_a_missing_gdbus_is_not_measured(self):
        self.assertIsNone(self.probe(raises=FileNotFoundError()))

    def test_unparseable_output_is_not_measured(self):
        """A renamed method or a changed reply shape must read as 'unknown',
        never as a uuid and never as a crash -- spec 4.0.1.

        The first four values below all contain no "'" at all, so every one
        of them is rejected by the parser's `len(parts) < 2` guard alone --
        none reaches the `not parts[1]` guard beside it. `('', 'text')`
        (an empty first field between two real quotes) is the one value here
        that DOES reach `len(parts) >= 2`, with parts[1] == "". Delete the
        `not parts[1]` half of the check and this loop still passes for the
        first four junk values and only catches the regression because this
        fifth one is here -- verified by deleting that check locally and
        watching this test stay green until this value was added."""
        for junk in (b"", b"()\n", b"no quotes here\n", b"(uint64 0,)\n",
                     b"('', 'text')\n"):
            self.assertIsNone(self.probe(junk), "%r parsed as a uuid" % junk)

    def test_the_payload_never_reaches_the_return_value(self):
        """Spec 5.2: only the uuid may be retained."""
        uuid = self.probe(b"('abc-def', 'hunter2 the password')\n")
        self.assertEqual(uuid, "abc-def")
        self.assertNotIn("hunter2", uuid)


class TestIdleGate(unittest.TestCase):
    """Spec 4.2's second gate: _user_recently_active, read from
    org.gnome.Mutter.IdleMonitor.

    PLACED HERE, beside TestHistoryUuidProbe above, and the reason is that
    the two subjects are the same subject: a module-level three-valued gdbus
    reader with a partial parse and a "None is not a value" rule. Task 12's
    brief named this file for exactly this class and the naming is right for
    _user_recently_active itself. Its COMPOSITION with the poll loop -- the
    tick that does or does not happen -- is a different subject and lives in
    test_watcher_polling.py's TestSlowTierIdleGate, because the gate is a
    `continue` in PollingWatcher.pump and that file owns pump; and the
    divergence re-probe, which writes an interval, lives in
    test_watcher_safety_net.py beside TestDegradedBackoff, the suite's home
    for every _safety_net.interval assertion. Three subjects, three homes,
    stated because a single brief named a single file for all of it."""

    def idle(self, stdout=b"", returncode=0, raises=None):
        def run(argv, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")
        return agent._user_recently_active(run=run)

    def test_a_recent_input_reads_as_active(self):
        self.assertIs(self.idle(b"(uint64 1200,)\n"), True)

    def test_a_long_idle_reads_as_inactive(self):
        self.assertIs(self.idle(b"(uint64 600000,)\n"), False)

    def test_a_failed_call_is_not_measured(self):
        """None is 'we do not know', and the caller must PROCEED on it. A gate
        that skipped the slow tier whenever IdleMonitor was unavailable would
        silently disable the only thing that can see a dead tracker -- on every
        machine without that D-Bus name."""
        for failure in (dict(returncode=1), dict(stdout=b"junk"),
                        dict(raises=subprocess.TimeoutExpired(["gdbus"], 3)),
                        dict(raises=FileNotFoundError())):
            self.assertIsNone(self.idle(**failure), failure)

    def test_the_threshold_is_the_named_constant_and_not_a_literal(self):
        """The number is a JUDGEMENT, so it has to be arguable in one place
        rather than spelled at a comparison. Both sides of it are pinned FROM
        the constant, so retuning USER_IDLE_GATE_SECONDS moves this test with
        it instead of breaking it -- what is pinned is that the constant is
        what decides, which is the property a literal would silently lose."""
        just_inside = int((agent.USER_IDLE_GATE_SECONDS - 1) * 1000)
        just_outside = int((agent.USER_IDLE_GATE_SECONDS + 1) * 1000)
        self.assertIs(self.idle(b"(uint64 %d,)\n" % just_inside), True)
        self.assertIs(self.idle(b"(uint64 %d,)\n" % just_outside), False)

    def test_the_boundary_resolves_to_active(self):
        """Exactly USER_IDLE_GATE_SECONDS of idle time PROCEEDS. Same
        fail-open direction as the None rule, and chosen for the same reason:
        an ambiguous reading should cost a fork, never a verdict. `<` instead
        of `<=` is the one-character mutation this pins."""
        exactly = int(agent.USER_IDLE_GATE_SECONDS * 1000)
        self.assertIs(self.idle(b"(uint64 %d,)\n" % exactly), True)

    def test_an_unrecognised_reply_shape_is_not_measured(self):
        """A renamed method, a changed reply shape or a D-Bus error string must
        read as 'not measured' -- which PROCEEDS -- and never as False.

        Each value below is rejected by a DIFFERENT clause, which is what
        makes this a mutation table rather than a repetition: no "uint64" at
        all (the partition guard), "uint64" with nothing numeric after it,
        and a signed value that int() would have accepted while isdigit()
        does not."""
        for junk in (b"", b"()\n", b"(true,)\n", b"(uint64 ,)\n",
                     b"(uint64 -1,)\n", b"(uint64 abc,)\n"):
            self.assertIsNone(self.idle(junk), "%r parsed as an idle time" % junk)

    def test_zero_idle_time_is_active_rather_than_falsy(self):
        """A user typing RIGHT NOW reads `(uint64 0,)`. The parse must not
        treat that as "nothing to see": 0 is the most active reading there
        is, and a truthiness test on the parsed value would invert it."""
        self.assertIs(self.idle(b"(uint64 0,)\n"), True)

    def test_the_call_asks_the_idle_monitor_and_never_gpaste(self):
        """One gdbus call, and it must be the RIGHT one: the gate is
        affordable only because GetIdletime takes no focus and costs
        milliseconds. Also pins that the payload rule has nothing to fear
        here -- the reply is a number, so there is no clipboard content in
        this call's stdout to leak (spec 5.2)."""
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"(uint64 10,)\n", b"")

        agent._user_recently_active(run=run)
        argv, kwargs = seen[0]
        self.assertEqual(argv[0], "gdbus")
        self.assertIn(agent.IDLE_MONITOR_BUS_NAME, argv)
        self.assertIn(agent.IDLE_MONITOR_OBJECT_PATH, argv)
        self.assertIn("%s.GetIdletime" % agent.IDLE_MONITOR_INTERFACE, argv)
        self.assertNotIn(agent.GPASTE_BUS_NAME, argv)
        self.assertEqual(
            kwargs["timeout"], agent.GPASTE_CALL_TIMEOUT,
            "an ungated gdbus call on the poll thread can hang it forever")


class TestTrackingProbe(unittest.TestCase):
    """Spec 5.3: gpaste_tracking, the reading that replaced a guess.

    The verdict line shipped "the gnome-shell extension being disabled is one
    possible cause" through three production incidents where the extension was
    measured Enabled/ACTIVE with track-changes true. This function is what the
    line asks instead."""

    def tracking(self, stdout=b"", returncode=0, raises=None):
        def run(argv, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")
        return agent.gpaste_tracking(run=run)

    def test_a_true_reply_reads_as_true(self):
        self.assertIs(self.tracking(b"(<true>,)\n"), True)

    def test_a_false_reply_reads_as_false(self):
        """The reading this whole change exists to make possible: GPaste
        itself saying it stopped tracking."""
        self.assertIs(self.tracking(b"(<false>,)\n"), False)

    def test_a_failed_call_is_not_measured_and_is_never_false(self):
        """THE DISTINCTION THAT MATTERS. "GPaste says it is not tracking" and
        "we could not ask GPaste" are opposite pieces of evidence, and
        collapsing them would re-commit the exact sin spec 5.3 exists to
        undo -- reporting something the line does not know.

        `returncode=1` is also where a WRONG PROPERTY NAME lands: gdbus exits
        non-zero on "No such property", so a name this file guessed wrong
        reports unavailable rather than false."""
        for failure in (dict(returncode=1), dict(stdout=b"junk"),
                        dict(stdout=b""),
                        dict(raises=subprocess.TimeoutExpired(["gdbus"], 3)),
                        dict(raises=FileNotFoundError())):
            self.assertIsNone(self.tracking(**failure), failure)

    def test_a_nonzero_exit_is_not_measured_even_with_a_true_shaped_reply(self):
        """The returncode check must come BEFORE the parse. gdbus writes its
        error to stderr and can still leave something on stdout; a parser
        reached on a failed call would read a stale or partial reply as a
        measurement."""
        self.assertIsNone(self.tracking(b"(<true>,)\n", returncode=1))

    def test_the_call_asks_gpaste_for_the_named_property(self):
        """Pins the property name to the module constant rather than to a
        literal in the argv, because that name is the one thing in this
        function that could not be verified against a live GPaste at
        authoring time -- so its correction must be one line, and the log
        line reports it for exactly that reason (see the constant's own
        comment). Also pins that this reads a PROPERTY and never
        GetElementAtIndex, whose reply carries clipboard text (spec 5.2)."""
        seen = []

        def run(argv, **kwargs):
            seen.append((argv, kwargs))
            return subprocess.CompletedProcess(argv, 0, b"(<true>,)\n", b"")

        agent.gpaste_tracking(run=run)
        argv, kwargs = seen[0]
        self.assertIn("org.freedesktop.DBus.Properties.Get", argv)
        self.assertIn(agent.GPASTE_INTERFACE, argv)
        self.assertIn(agent.GPASTE_TRACKING_PROPERTY, argv)
        self.assertNotIn("%s.GetElementAtIndex" % agent.GPASTE_INTERFACE, argv)
        self.assertEqual(kwargs["timeout"], agent.GPASTE_CALL_TIMEOUT)


class TestFastTier(unittest.TestCase):
    def watcher(self, uuids):
        """A watcher whose fast tier reads `uuids` in order."""
        readings = list(uuids)
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: readings.pop(0))
        self.addCleanup(watcher.stop)
        return watcher

    def test_a_moved_uuid_signals(self):
        watcher = self.watcher(["a", "b"])
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set(), "the baseline reading signalled")
        watcher._fast_tick()
        self.assertTrue(watcher._event.is_set(), "a new history entry did not signal")

    def test_a_frozen_uuid_does_not_signal(self):
        """GPaste's re-offer: the selection changed, the history did not."""
        watcher = self.watcher(["a", "a"])
        watcher._fast_tick()
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set())

    def test_an_unmeasured_reading_does_not_signal_and_does_not_poison(self):
        """Spec 4.0.1: None is not a value. A failed call between two identical
        readings must not look like two changes.

        NOTE on what this test can and cannot prove, recorded because the
        project's own history is five-for-five on tests weaker than their
        docstring (see the mutation table in task-5-report.md): both
        assertions below are `assertFalse`, and the fast tier's own
        moved/frozen/unknown condition requires `previous is not None`
        before it will ever signal. That conjunct alone -- nothing about
        remembering or not remembering the pre-failure reading -- already
        forces both assertions here to pass whether or not `self._last_uuid`
        is correctly preserved across the None reading. So this test proves
        the FALSE-POSITIVE half of the docstring's claim (a failed call does
        not itself look like a change) but NOT the "does not poison" half:
        it cannot distinguish "correctly remembered the last real reading"
        from "incorrectly reset it to None", because both produce no signal
        here. The discriminating case -- a THIRD, distinct reading after the
        None, asserting the signal DOES fire -- is
        test_a_real_change_survives_a_failed_call_between_two_readings,
        immediately below: added in fix round 1 after the coordinator's
        review of task-5-report.md's mutation table, which named this exact
        gap and the exact input that closes it.
        """
        watcher = self.watcher(["a", None, "a"])
        watcher._fast_tick()
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set(), "a failed call signalled")
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set(),
                         "recovery from a failed call signalled a change nobody made")

    def test_a_real_change_survives_a_failed_call_between_two_readings(self):
        """Fix round 1, row 5 of task-5-report.md's original mutation table.

        test_an_unmeasured_reading_does_not_signal_and_does_not_poison just
        above re-uses "a" on both sides of the None, so it cannot tell
        "self._last_uuid correctly remembered across the failed call" apart
        from "incorrectly reset to None by it" -- both produce no signal
        there. This uses a THIRD, DISTINCT reading after the None instead.

        If the remembering guard (`if current is not None:` in _fast_tick)
        were weakened to an unconditional assignment, the failed call's None
        would overwrite self._last_uuid, and the real a->b change on the
        next tick would compare "b" against a wrongly-None baseline instead
        of "a" -- so `previous is not None` would be false, and the
        signal that SHOULD fire would not. Confirmed empirically during
        Task 5 (task-5-report.md): the mutated code printed False for this
        exact sequence; the code as shipped printed True. This is the copy a
        user would feel -- silently never syncing -- so it is asserted
        directly, not just reasoned about.
        """
        watcher = self.watcher(["a", None, "b"])
        watcher._fast_tick()   # baseline: "a"
        watcher._fast_tick()   # a failed call: must not overwrite the baseline
        self.assertFalse(watcher._event.is_set(), "a failed call signalled")
        watcher._fast_tick()   # the real change, straddling the failed call
        self.assertTrue(
            watcher._event.is_set(),
            "a real change (a -> b) across a failed call must still signal",
        )

    def test_the_production_default_read_history_uuid_is_the_real_probe(self):
        """Fix round 1, row 7 of task-5-report.md's original mutation table.

        Every OTHER test in this class passes its own read_history_uuid
        stub, so none of them can tell whether GPasteWatcher.__init__'s
        DEFAULT -- what production actually uses when nobody overrides it --
        points at the real gpaste_history_uuid or somewhere else entirely;
        changing the default to `lambda: None` left the full 369-test suite
        green (task-5-report.md). Checked directly against the signature's
        own default value, not through a behavioral test built on a scripted
        stub, which could not distinguish "the real default" from
        "coincidentally correct because this test injected its own"."""
        default = inspect.signature(agent.GPasteWatcher.__init__).parameters[
            "read_history_uuid"].default
        self.assertIs(default, agent.gpaste_history_uuid)


# What GPaste offers after it re-encodes an image, measured on the live
# machine: one type becomes twenty-three, with no Update signal. Design doc
# S1.2.
TWENTY_THREE = tuple(sorted([
    "image/webp", "image/tiff", "image/jpeg", "text/ico", "image/icon", "image/ico",
    "application/ico", "image/vnd.microsoft.icon", "image/x-win-bitmap", "image/x-ico",
    "image/x-icon", "image/x-MS-bmp", "image/x-bmp", "image/bmp", "image/avif",
    "audio/x-riff", "image/jxl", "image/png", "SAVE_TARGETS", "MULTIPLE", "TARGETS",
    "TIMESTAMP"]))


class TestSlowTierVerdict(unittest.TestCase):
    """Spec 4.0 / 4.0.1 / 4.2: _observe_tick's OWN verdict, now judged
    against the fast tier's uuid instead of the signal counter.

    Placed HERE, not in test_watcher_safety_net.py's TestGPasteSafetyNet --
    the pre-v3.3 home of _observe_tick's OTHER tests, and the literal file
    task-6-brief.md names. That name is stale: docs/superpowers/specs/
    2026-08-02-v3.2.1-test-split-design.md moved _observe_tick's tests into
    test_watcher_safety_net.py a cycle before this one, and test_watcher.py
    is that split's fixture ANCHOR (FakeGPasteProcess, ScriptedReadClipboard,
    etc.), never home to a `_observe_tick` test itself -- confirmed by grep,
    zero hits. This file is the right one on independent grounds too: the
    brief's own snippet below calls `agent.GPasteWatcher(...)` and
    `_StubClipboard()`, both of which exist only in THIS module's namespace
    (test_watcher_safety_net.py imports GPasteWatcher bare, with no `agent.`
    alias, and has no _StubClipboard at all) -- so the code as given could
    only ever run here. See task-6-report.md for the full reasoning."""

    def watcher(self, uuid="frozen"):
        watcher = agent.GPasteWatcher(clipboard=_StubClipboard(),
                                      read_history_uuid=lambda: uuid)
        # Both fields primed to the SAME value: this represents a connection
        # that has already been through at least one prior slow-tier tick
        # with a stable uuid, which is the steady state every test below
        # means to start from -- not a brand new watcher's first-ever tick,
        # which can never see uuid_frozen True by construction (see
        # _observe_tick's own predicate comment on the one-tick warm-up
        # cost). Priming only self._last_uuid (as an earlier revision of
        # this helper did) left self._uuid_at_last_tick at its __init__
        # default of None, so every test's FIRST _observe_tick call saw
        # uuid_frozen False regardless of what the test needed to pin --
        # found and fixed in review, alongside the predicate defect this
        # helper exists to exercise correctly.
        watcher._last_uuid = uuid
        watcher._uuid_at_last_tick = uuid
        self.addCleanup(watcher.stop)
        return watcher

    def test_a_takeover_does_not_degrade(self):
        """THE DEFECT. The token moves, the uuid does not, and one quiet tick
        follows. Today this confirms; it must not."""
        watcher = self.watcher()
        watcher._observe_tick(("image", ("image/png",)), ("image", TWENTY_THREE))
        watcher._observe_tick(("image", TWENTY_THREE), ("image", TWENTY_THREE))
        self.assertFalse(watcher._degraded, "GPaste's own re-offer degraded the connection")

    def test_a_dead_tracker_still_degrades(self):
        """THE MIRROR, and the reason the rule above is not simply 'never
        degrade'. A dead tracker under an active user diverges REPEATEDLY."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertTrue(watcher._degraded, "a genuinely dead tracker was not diagnosed")

    def test_no_verdict_while_the_uuid_is_unknown(self):
        """Spec 4.0.1: a failing GetElementAtIndex plus an active user looks
        exactly like a dead tracker. It must reach no verdict at all.

        _last_uuid is set to None BY HAND: that is the state production is in
        only before the fast tier's first successful reading (or, later, once
        Task 7's own persistent-failure fallback -- spec 4.3 -- re-engages
        this table; see spec 4.0.1's closing sentence). self._last_uuid never
        reverts to None once a reading has SUCCEEDED even once -- a later
        failure leaves it holding the last good value, by design (see
        _fast_tick) -- so this test pins the predicate's own logic rather
        than claiming to reproduce the renamed-method scenario end to end."""
        watcher = self.watcher()
        watcher._last_uuid = None
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertFalse(watcher._degraded,
                         "a failing uuid call was read as a dead tracker")

    def test_no_verdict_while_a_uuid_failure_run_is_in_progress(self):
        """Spec 4.0.1's closing paragraph -- the half Task 6's own review
        explicitly left for this task, named in _observe_tick's "NOT CLOSED
        BY THIS DELTA ALONE" comment: self._last_uuid is STICKY (_fast_tick
        assigns it only on a MEASURED reading), so a fast tier that measured
        once and then fails PERSISTENTLY leaves uuid_frozen reading True
        forever -- not because the history is idle, but because nothing has
        been read since. self._uuid_failures is the one piece of state that
        tells "idle" apart from "unknown": a nonzero run means the most
        recent fast-tier call(s) did not resolve, so whatever this tick's
        uuid_frozen computed is judging a stale pair of readings, not a live
        one.

        watcher() primes BOTH uuid fields to the SAME value, so uuid_frozen
        reads True from the OLD delta alone -- exactly the false "tracking
        is dead" signature spec 4.0.1 warns about. Only self._uuid_failures
        being nonzero should be standing between these two diverging ticks
        and a false verdict; this test hand-sets it rather than driving
        _fast_tick for real, the same convention
        test_no_verdict_while_the_uuid_is_unknown above already uses for the
        identical reason (see that test's own docstring)."""
        watcher = self.watcher()
        watcher._uuid_failures = 1   # a run in progress, well short of the fallback threshold
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertFalse(
            watcher._degraded,
            "a uuid failure run in progress was read as a dead tracker")

    def test_verdicts_resume_once_the_fallback_has_engaged(self):
        """The mirror of the test above, and spec 4.0.1's OTHER half: "Once
        section 4.3's fallback engages, this table stops applying and
        today's mechanics resume." self._uuid_failures keeps climbing
        forever in the ordinary case once the fallback has fired -- the
        method that broke does not fix itself, and recovery is explicitly
        out of scope this release (spec 4.3's closing paragraph: "next
        cycle's latch work") -- so a failure-run guard that kept applying
        past the fallback would leave _observe_tick unable to reach a
        verdict again for the rest of the connection: silently WORSE than
        pre-v3.3, which had no uuid tier to get stuck behind at all.

        HOW that resumption works, corrected in fix round 1: _observe_tick
        SWITCHES DISCRIMINATOR once self._uuid_tier_failed latches, back to
        today's `signals == self._signals_at_last_tick` (merge-base
        58321cf:3505). An earlier revision of this docstring claimed instead
        that today's mechanics "fall out of the EXISTING wl-paste-divergence
        check" once uuid_frozen settles permanently True -- which was FALSE,
        and left this file asserting a parity the code did not have: every
        uuid term went constant post-fallback, so the predicate degenerated
        to `if not read_ok:` and the signal counter reached the decision
        nowhere. See TestUuidTierFallback's three post-fallback tests, which
        drive the real composed path this hand-set one cannot -- setting
        both flags directly is exactly why the defect stayed invisible here.

        Signals are pinned at 0 by construction in this class (no gdbus line
        ever reaches these watchers), so the restored discriminator reads
        "still silent" on both ticks below and the divergence confirms."""
        watcher = self.watcher()
        watcher._uuid_failures = 5
        watcher._uuid_tier_failed = True
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertTrue(
            watcher._degraded,
            "a verdict did not resume once the uuid-tier fallback had engaged")

    def test_an_actively_copying_healthy_connection_does_not_degrade(self):
        """THE regression found in review, and the one a user would have
        felt: "self._last_uuid is not None" alone is permanently true from
        the fast tier's first successful reading onward -- it is STICKY
        (_fast_tick only ever assigns it on a measured reading) -- so it has
        NO discriminating power against two consecutive REAL, TRACKED
        copies, which is exactly what a healthy actively-copying connection
        looks like. "Two consecutive windows each containing a copy" is the
        walkthrough that found this: without a delta against what the uuid
        was at the LAST tick, that pattern alone would arm-then-confirm,
        which is a regression against the predicate this task replaced --
        `signals == self._signals_at_last_tick` protected exactly this user,
        because Updates move 1:1 with real copies.

        The uuid changes every tick below, exactly as it would if GPaste is
        genuinely tracking the user's copies -- must not degrade."""
        watcher = self.watcher()
        watcher._last_uuid = "uuid-1"
        watcher._observe_tick(("text", "a"), ("text", "b"))   # window 1: a real, tracked copy
        watcher._last_uuid = "uuid-2"
        watcher._observe_tick(("text", "b"), ("text", "c"))   # window 2: another real, tracked copy
        self.assertFalse(
            watcher._degraded,
            "an actively-copying healthy connection was degraded")

    def test_the_snapshot_updates_every_tick_so_a_tracker_that_goes_dead_is_still_caught(self):
        """Closes a mutation hole found while re-verifying the fix round:
        deleting `self._uuid_at_last_tick = self._last_uuid` at the bottom
        of the function leaves the snapshot stuck at whatever it was primed
        with, forever. A tracker that is dead from the very start would
        still (coincidentally) be diagnosed correctly, because a NEVER-
        UPDATED snapshot happens to equal a NEVER-CHANGING self._last_uuid
        either way -- so this needs a uuid that moves ONCE and then holds,
        which only a snapshot updated every tick can compare against
        correctly on the tick after."""
        watcher = self.watcher(uuid="uuid-A")   # primes both fields to "uuid-A"
        watcher._last_uuid = "uuid-B"           # one real, tracked copy: the uuid moves
        watcher._observe_tick(("text", "a"), ("text", "b"))   # tick 1: uuid-B vs primed uuid-A -- correctly NOT frozen
        # From here self._last_uuid is left at "uuid-B": the tracker goes
        # dead right after that one real copy, exactly the scenario a
        # stuck snapshot cannot tell apart from a healthy idle uuid.
        watcher._observe_tick(("text", "b"), ("text", "c"))   # tick 2: uuid stayed at B -- ARMS
        watcher._observe_tick(("text", "c"), ("text", "d"))   # tick 3: uuid still B -- CONFIRMS
        self.assertTrue(
            watcher._degraded,
            "a tracker that went dead right after one real copy was not diagnosed")

    def test_a_brand_new_watcher_with_no_prior_snapshot_reaches_no_verdict(self):
        """The startup case the predicate comment names (spec 4.0.1's
        warm-up cost): a brand new watcher's first-EVER tick has no
        snapshot to compare against (self._uuid_at_last_tick starts at
        None in __init__, and nothing primes it here), so it must reach no
        verdict regardless of self._last_uuid -- including the one case a
        naive `A == B` on two None values gets wrong (`None == None` is
        True in Python), which is exactly why the predicate checks BOTH
        readings are present and not only that they are equal."""
        watcher = agent.GPasteWatcher(clipboard=_StubClipboard(),
                                      read_history_uuid=lambda: None)
        self.addCleanup(watcher.stop)
        # Neither field seeded: both sit at __init__'s default, None.
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertFalse(
            watcher._degraded,
            "a watcher with no prior snapshot reached a verdict from nothing")

    def test_a_cleared_run_rearms_rather_than_immediately_confirming(self):
        """A settle that clears must actually reset self._armed to False, not
        merely compute confirmed=False for that one tick and leave the flag
        stuck true. Otherwise the run's THIRD tick would treat any next
        divergence -- however unrelated to the first one -- as tick two of a
        pair that was never really re-armed, and confirm one tick early.

        Four ticks: arms, settles (clears), diverges again (must ARM, not
        confirm, since the run was genuinely cleared), diverges once more
        (NOW it may confirm)."""
        watcher = self.watcher()
        watcher._observe_tick(("image", ("image/png",)), ("image", TWENTY_THREE))  # arms
        watcher._observe_tick(("image", TWENTY_THREE), ("image", TWENTY_THREE))    # settles, clears
        watcher._observe_tick(("image", TWENTY_THREE), ("image", ("image/png",)))  # unrelated divergence
        self.assertFalse(
            watcher._degraded,
            "a cleared run confirmed on its very next divergence instead of re-arming")
        watcher._observe_tick(("image", ("image/png",)), ("image", TWENTY_THREE))  # confirms the re-armed pair
        self.assertTrue(
            watcher._degraded,
            "a genuinely re-armed run must still be able to confirm")

    def test_a_frozen_tick_does_not_pre_arm_the_next_real_divergence(self):
        """The mirror of the test above, from the other side: a tick where
        NOTHING moved must leave the run genuinely unarmed, not merely
        compute confirmed=False for that one tick and leave the flag stuck
        true. Otherwise the FIRST real divergence right after a frozen tick
        would be treated as tick two of a pair that was never truly armed,
        and confirm one tick early -- a dead source diagnosed on its very
        first observed copy, with no second confirming tick at all."""
        watcher = self.watcher()
        watcher._observe_tick(("text", "a"), ("text", "a"))   # frozen: nothing happened
        watcher._observe_tick(("text", "a"), ("text", "b"))   # the FIRST real divergence
        self.assertFalse(
            watcher._degraded,
            "a frozen tick pre-armed the run, confirming on the first real divergence")


class TestSignalPathSilenceLog(unittest.TestCase):
    """Spec 4.0's top row, second clause, and spec 6.1: the uuid moving means
    tracking is alive, full stop -- but whether the SIGNAL PATH also saw that
    same move is a separate question, answered by the accepted-signal counter
    alone. Silent there is spec 6.1, "signal path silent, tracking alive",
    and it is explicitly NOT a degraded state (spec 6: "its own one-shot
    flag, and it is a log flag, not a mode latch") -- the fast tier already
    IS the sync at that point, at _fast_interval seconds and zero focus cost.
    A log line and nothing else; "log only" is a claim about what the code
    does NOT do, so it gets its own test below rather than living only in
    the log-line tests' incidental silence.

    Owned by _fast_tick (Task 5's function) because it is the only code that
    ever OBSERVES a uuid MOVE -- _observe_tick only ever sees whether
    self._last_uuid is frozen, never the transition itself. Routed to this
    task per the dispatch: Task 5's implementer correctly declined to invent
    this line since the brief it built from did not contain it; this task
    owns the state table, so it owns the line (task-5-report.md, "Disagreement
    with the brief")."""

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def marker_lines(self):
        return [line for line in self.log_lines if "signal path may be silent" in line]

    def watcher(self, uuids):
        """A watcher whose fast tier reads `uuids` in order -- TestFastTier's
        own helper, duplicated rather than shared because that one lives
        inside TestFastTier and importing across sibling TestCase classes in
        the same module for one helper is not worth the coupling."""
        readings = list(uuids)
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: readings.pop(0))
        self.addCleanup(watcher.stop)
        return watcher

    def test_a_moved_uuid_with_no_signal_logs_once_per_connection(self):
        watcher = self.watcher(["a", "b", "c"])
        watcher._fast_tick()   # baseline: previous is None, nothing to judge
        watcher._fast_tick()   # a -> b, and no signal has ever arrived
        self.assertEqual(
            len(self.marker_lines()), 1,
            "a real uuid move alongside a silent signal path must be logged")
        watcher._fast_tick()   # b -> c, still silent -- must NOT log again
        self.assertEqual(
            len(self.marker_lines()), 1,
            "the flag is one-shot per CONNECTION, not per tick")

    def test_a_moved_uuid_with_a_signal_does_not_log(self):
        """The healthy case: the signal path caught this exact move, so
        there is nothing to report."""
        watcher = self.watcher(["a", "b"])
        watcher._fast_tick()          # baseline
        watcher._signals = 1          # a signal arrived in this window
        watcher._fast_tick()          # a -> b, WITH a signal
        self.assertEqual(
            self.marker_lines(), [],
            "a healthy signal path must not be reported as silent")

    def test_a_frozen_uuid_never_logs(self):
        """Nothing moved; there is no "is the signal path silent" question to
        even ask -- spec 4.0's top row is conditioned on the uuid MOVING."""
        watcher = self.watcher(["a", "a", "a"])
        watcher._fast_tick()
        watcher._fast_tick()
        watcher._fast_tick()
        self.assertEqual(self.marker_lines(), [], "a frozen uuid logged something")

    def test_a_stale_signal_count_does_not_suppress_a_later_silent_move(self):
        """A signal in one window must not hide silence in the NEXT one. The
        two tests above cannot tell a correctly-updated per-tick baseline
        from one that is set once and never touched again: with no signal
        ever, both look silent every time; with a signal every time, both
        look healthy every time. This needs a signal in window 1 and none in
        window 2, which only a per-tick baseline gets right -- a stale one
        would still be comparing window 3's count against window 1's signal,
        see it as "unchanged", and wrongly call window 2 healthy too."""
        watcher = self.watcher(["a", "b", "c"])
        watcher._fast_tick()          # baseline: "a"
        watcher._signals = 1          # a signal arrives in this window
        watcher._fast_tick()          # a -> b, WITH a signal: must not log
        self.assertEqual(self.marker_lines(), [], "the healthy window logged")
        watcher._fast_tick()          # b -> c: no NEW signal since the last tick
        self.assertEqual(
            len(self.marker_lines()), 1,
            "a later silent window must still be caught even though an "
            "earlier one had a signal")

    def test_the_log_line_changes_no_interval_and_sets_no_latch(self):
        """Spec 6: a LOG flag, not a mode latch -- 6.1 is explicitly NOT
        degraded, unlike 6.2's _observe_tick verdict."""
        watcher = self.watcher(["a", "b"])
        before = watcher._safety_net.interval
        watcher._fast_tick()
        watcher._fast_tick()
        self.assertEqual(
            len(self.marker_lines()), 1,
            "must actually have logged, or this test proves nothing")
        self.assertEqual(watcher._safety_net.interval, before,
                         "6.1 must not touch the poll interval")
        self.assertFalse(watcher._degraded, "6.1 must not set the degrade latch")


class TestTierIntervalResolution(unittest.TestCase):
    """__init__ wiring, not _env_seconds in isolation. Every test above and
    below this class exercises _fast_tick/_env_seconds directly or through a
    watcher built with an explicit fast_interval_seconds -- none of them
    would notice a GPasteWatcher.__init__ that hardcoded
    `self._fast_interval = FAST_TIER_SECONDS` (or the slow-tier equivalent)
    and never read the argument or the environment at all. This is the one
    place that gap is closed.

    Both tiers are resolved by construction with the identical three-step
    rule -- argument, then environment, then default -- so one test walks
    both rather than duplicating the same three phases twice. This is the
    task's own gap-closing addition (the brief defines fast_interval_seconds
    but Task 7 needs self._slow_interval, which nothing else defines), not
    coverage for a brief-supplied test, so it is in scope to add rather than
    something to report as a hole -- see task-5-report.md.
    """

    def setUp(self):
        # Same save-restore shape as TestIntervalInjection.setUp above, and
        # for the same two reasons stated there: scrubbing has to happen
        # BEFORE the test that needs a clean environment, and an ambient
        # value has to be put back rather than deleted. This class's phases
        # set and pop both variables as they go, so it needs the restore
        # more than most.
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS", None)
        os.environ.pop("CLIPWIRE_SLOW_TIER_SECONDS", None)

    def test_fast_and_slow_intervals_resolve_argument_then_env_then_default(self):
        # Phase 1: an explicit argument wins even with the environment set,
        # for both tiers.
        os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = "9"
        os.environ["CLIPWIRE_SLOW_TIER_SECONDS"] = "9"
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), fast_interval_seconds=1.5,
            slow_interval_seconds=2.5, safety_net_interval_seconds=42.0)
        watcher.stop()
        self.assertEqual(watcher._fast_interval, 1.5)
        self.assertEqual(watcher._slow_interval, 2.5)

        # Phase 2: no argument, no environment -- the fast tier falls back to
        # its own constant, and the slow tier falls back to the EXISTING
        # safety_net_interval_seconds parameter, never a second timer of its
        # own ("the slow tier IS the existing safety net").
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS")
        os.environ.pop("CLIPWIRE_SLOW_TIER_SECONDS")
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), safety_net_interval_seconds=42.0)
        watcher.stop()
        self.assertEqual(watcher._fast_interval, agent.FAST_TIER_SECONDS)
        self.assertEqual(watcher._slow_interval, 42.0)

        # Phase 3: no argument, environment set -- the override reaches both
        # tiers, mirrored identically.
        os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = "0.25"
        os.environ["CLIPWIRE_SLOW_TIER_SECONDS"] = "0.75"
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), safety_net_interval_seconds=42.0)
        watcher.stop()
        self.assertEqual(watcher._fast_interval, 0.25)
        self.assertEqual(watcher._slow_interval, 0.75)


class TestFastTierIntegration(unittest.TestCase):
    """Fix round 1: closes rows 1 and 6 of task-5-report.md's original
    mutation table. Both shared one root cause, named in the coordinator's
    review: nothing in the suite ever called GPasteWatcher.start() and let
    the fast tier run as a REAL thread against a real interval.
    TestFastTier's tests all call _fast_tick() directly, which cannot catch
    either start() silently never launching self._fast_thread (row 1 --
    deleting those 3 lines left the full 369-test suite green) or
    _fast_loop's try/except silently not existing (row 6 -- same result,
    since nothing ever ran the loop for an exception to reach).

    No real gdbus subprocess: subprocess.Popen is patched to a
    FakeGPasteProcess exactly as TestGPasteWatcherLifecycle does in
    test_watcher.py, so the gdbus pump thread this same start() call also
    launches has a real pipe to block on -- nothing is ever written to it,
    so it produces zero signals of its own -- instead of forking a real
    gdbus monitor process. The safety net's own interval is set far past
    JOIN_TIMEOUT, the convention used throughout this suite (see
    TestGPasteWatcherLifecycle.start_watcher and
    TestGPasteSafetyNet.start_watcher in test_watcher*.py), so within this
    test's window it can only ever take its one baseline read and cannot
    itself be the thing that sets the event.
    """

    def setUp(self):
        # This test deliberately makes the fast tier's first tick raise, to
        # prove _fast_loop survives it (row 6) -- so a real traceback is
        # expected on the log path. Captured rather than left to print to
        # real stderr during a test run, the same convention
        # test_watcher_polling.py's TestPollingWatcher and
        # test_watcher_safety_net.py's TestGPasteSafetyNet use for the
        # identical reason (a deliberately-triggered error path).
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def wait_until(self, predicate):
        deadline = time.monotonic() + JOIN_TIMEOUT
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.005)

    def test_start_runs_the_fast_tier_as_a_real_thread_and_survives_a_failing_tick(self):
        """Row 1: start() must launch a thread that reaches a real
        observation on its own -- nobody here calls _fast_tick by hand. The
        thread's existence is also checked directly (assertIsNotNone/
        is_alive) immediately after start() returns, rather than inferred
        only from an observation eventually happening: _fast_loop's very
        first action is `self._stop.wait(self._fast_interval)`, which blocks
        for at least fast_interval_seconds before doing anything else, so a
        correctly launched thread is GUARANTEED still alive at that
        checkpoint -- this assertion is not a timing gamble, and it pins row
        1 even if some future change altered the pump or safety net's own
        timing within the window this test does not otherwise rule out.

        Row 6, in the same test: the FIRST scripted reading raises. A fast
        tier whose loop dies on an uncaught exception (deleting
        _fast_loop's try/except -- see the report) would never reach the
        second and third readings the final assertion below depends on, so
        reaching a real observation at all proves the loop survived tick 1's
        exception and kept going rather than dying silently -- the
        production defect this file's whole "signalled, never called" shape
        exists to prevent, applied to the tier's own internal robustness
        rather than to the handler-call boundary.

        Waits on `changes` -- the HANDLER having run -- not on
        `watcher._event.is_set()`. The first draft of this test waited on
        the raw event and failed 100% of the time even against known-correct
        code: `_start_observer`'s worker (started by this same start() call)
        is ALSO parked in `event.wait()` and clears the event BEFORE calling
        the handler, so by the time this thread's own polling loop (checking
        every 5ms) gets to look, the worker has almost always already
        consumed it -- the event is a transient wakeup, not a status flag.
        Confirmed by a standalone repro that drove _fast_loop directly with
        no observer attached, where waiting on the bare event worked every
        time. Mirrors test_watcher.py's own documented rationale for this
        exact choice (see SignallingClipboard's docstring: "waits on the
        CALLBACK having run rather than the watcher's private signal
        counter, so the test is evidence about observable behaviour").

        The stub raises a named AssertionError rather than letting a plain
        iterator run dry into StopIteration past its 3 scripted readings:
        both are caught identically by _fast_loop's `except Exception`, so
        this changes nothing about which assertion below catches a real
        failure -- only what a later reader sees in the captured log if the
        tier ticks more times than expected, whether from a slow CI box or
        an unrelated mutation, rather than a bare, unexplained StopIteration.
        """
        readings = [RuntimeError("simulated gdbus failure"), "a", "b"]
        calls = 0

        def read_history_uuid():
            nonlocal calls
            index = calls
            calls += 1
            if index >= len(readings):
                raise AssertionError(
                    "the fast tier ticked %d times; this test scripted only "
                    "%d readings" % (index + 1, len(readings)))
            value = readings[index]
            if isinstance(value, BaseException):
                raise value
            return value

        fake_process = FakeGPasteProcess()
        patcher = mock.patch("subprocess.Popen", return_value=fake_process)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(fake_process.close)
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(),
            safety_net_interval_seconds=JOIN_TIMEOUT * 100,
            read_history_uuid=read_history_uuid,
            fast_interval_seconds=0.01,
        )
        self.addCleanup(watcher.stop)
        changes = []
        watcher.start(lambda: changes.append(1))

        self.assertIsNotNone(
            watcher._fast_thread,
            "start() must launch the fast tier's own thread")
        self.assertTrue(
            watcher._fast_thread.is_alive(),
            "the fast tier's thread must still be running immediately after "
            "start() -- a correct _fast_loop cannot have finished even one "
            "iteration this fast, since its first action always waits at "
            "least fast_interval_seconds",
        )

        self.wait_until(lambda: changes)
        self.assertEqual(
            changes, [1],
            "the fast tier's own thread never drove a real observation -- "
            "either it is not running, or it died on the first (raising) "
            "tick instead of surviving to the next one",
        )


class TestUuidTierFallback(unittest.TestCase):
    """Spec 4.3: the uuid tier itself failing -- a third state, distinct from
    both "moved" and "frozen" (spec 4.0's table, bottom row: "unknown (method
    failing)"). Treating each failed GetElementAtIndex call as a solitary
    None probe is correct for an EPISODE (already handled -- see
    TestFastTier's own unmeasured-reading tests above) and catastrophic as a
    STEADY STATE: the fast tier goes mute, the slow tier is the only thing
    left, and -- per spec 1 -- that is a 30-180x sync regression with not one
    line in the log. This class is the fallback that makes the steady state
    loud instead of silent.

    The brief this class is built from (task-7-brief.md) references a
    `_capture_log` context-manager helper that does not exist anywhere in
    this codebase -- confirmed by grep across agent/tests/, zero hits -- a
    brief defect, reported rather than invented around (see task-7-report.md).
    This file's OWN established convention for the identical need --
    TestSignalPathSilenceLog.setUp and TestFastTierIntegration.setUp, both
    above -- is a plain setUp/addCleanup pair patching clipwire_agent.log
    directly, so that is what this class uses too, rather than inventing a
    second capture mechanism the file does not otherwise have.
    """

    def setUp(self):
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def marker_lines(self):
        return [line for line in self.log_lines if "history uuid" in line]

    def test_the_default_intervals_derive_twelve(self):
        """Spec 4.3: "N is defined, not left to the plan: enough consecutive
        failures to span two slow-tier ticks ... express it as a duration
        (>= 2x the slow interval) rather than a raw count, and let the count
        follow from the interval." At the PRODUCTION defaults --
        FAST_TIER_SECONDS=5, SAFETY_NET_POLL_SECONDS=30 -- that is
        2 * 30 / 5 == 12, smaller than the spec text's own illustrative
        "well over a hundred" (which describes a 5-minute slow tier nothing
        in this codebase currently configures by default). Pinned against
        the actual constants rather than the spec's illustrative figure.

        The environment is scrubbed first, and that is not defensive
        boilerplate: this watcher is built with NO explicit intervals, so
        __init__ resolves both from _env_seconds, and a stray
        CLIPWIRE_FAST_TIER_SECONDS or CLIPWIRE_SLOW_TIER_SECONDS left in the
        ambient environment -- by a developer's shell, a CI harness, or a
        sibling class scrubbing only one of the two -- silently changes the
        derived count and fails this hardcoded 12 for a reason that has
        nothing to do with the code under test. TestIntervalInjection.setUp
        and TestIntervalResolution.setUp use the identical save-restore
        shape, for the reasons spelled out on the first of them.
        mock.patch.dict restores whatever was there afterwards, so scrubbing
        here cannot leak into any other test either."""
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS", None)
        os.environ.pop("CLIPWIRE_SLOW_TIER_SECONDS", None)
        watcher = agent.GPasteWatcher(clipboard=_StubClipboard())
        self.addCleanup(watcher.stop)
        self.assertEqual(watcher._uuid_failures_before_fallback, 12)

    def test_the_floor_holds_when_the_slow_tier_is_not_slower_than_the_fast_one(self):
        """max(3, ...) is not decoration. Without it, this ratio (slow <=
        fast -- a test harness's own tuning, not a production shape, but
        __init__ does not refuse it) derives 1: a SINGLE failed call, an
        episode by this task's own definition (see self._uuid_failures's
        __init__ comment), would then be enough to trigger the fallback --
        the exact episode/state conflation this design exists to avoid,
        approached from the threshold side rather than the counter side."""
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), fast_interval_seconds=1.0,
            slow_interval_seconds=0.5)
        self.addCleanup(watcher.stop)
        self.assertEqual(watcher._uuid_failures_before_fallback, 3)

    def test_a_persistent_failure_logs_once_and_falls_back(self):
        """task-7-brief.md Step 1, adapted twice over from what the brief
        gives -- both changes needed to make this test's own claims true of
        what it actually checks, not just of what it is named for.

        (1) The brief builds this watcher with NO explicit
        safety_net_interval_seconds, so self._safety_net.interval is ALREADY
        SAFETY_NET_POLL_SECONDS before the first tick -- identical to the
        value this test asserts the fallback PRODUCES. Deleting the
        implementation's interval-assignment line entirely would leave that
        assertion passing by coincidence. Fixed by starting the safety net at
        a distinguishable 42.0 instead, so the assertion has to actually
        observe a write.

        (2) Added a boundary check the brief did not ask for: threshold - 1
        ticks must NOT have fired yet, proving the threshold fires EXACTLY
        at watcher._uuid_failures_before_fallback rather than merely
        "eventually" -- an off-by-one here (> instead of >=) would leave
        detection one full fast-tier interval later than the spec's own
        accounting assumes, silently, and nothing in the brief's own script
        would have caught it (it only ever checks threshold + 2).
        """
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: None,
            fast_interval_seconds=0.001, slow_interval_seconds=0.01,
            safety_net_interval_seconds=42.0)
        self.addCleanup(watcher.stop)
        threshold = watcher._uuid_failures_before_fallback

        for _ in range(threshold - 1):
            watcher._fast_tick()
        self.assertFalse(
            watcher._uuid_tier_failed,
            "the fallback fired before its own threshold was reached")

        watcher._fast_tick()   # the Nth failure -- exactly AT the threshold
        self.assertTrue(
            watcher._uuid_tier_failed,
            "did not fire exactly at the threshold (>= vs > boundary)")

        for _ in range(2):   # held past it: must not re-log or re-assign
            watcher._fast_tick()
        self.assertEqual(len(self.marker_lines()), 1,
                         "the fallback logged more than once")
        self.assertEqual(watcher._safety_net.interval, agent.SAFETY_NET_POLL_SECONDS)

    def test_an_intermittent_failure_does_not_fall_back(self):
        """task-7-brief.md Step 1 / Step 5, corrected. "One bad call is an
        episode, not a state" -- but the brief's own script (5 reads, 2
        Nones, default intervals => threshold 12) cannot prove that: its
        Step 5 break-check instructs deleting the `else: self._uuid_failures
        = 0` reset and claims this test "must FAIL", but with only 2
        CUMULATIVE failures against a threshold of 12, the counter reaches 2
        either way and the assertion stays green with the reset gone --
        empirically confirmed by running the deletion against the brief's
        own numbers before writing this version. A brief defect in the same
        "check weaker than its claim" family this project has now shipped
        (and caught) five times.

        Fixed by flooring the threshold at 3 (fast_interval == slow_interval)
        and scripting exactly 3 ISOLATED failures, each surrounded by a
        success: with the run-reset present, the longest RUN is 1 and the
        fallback never fires; with the reset deleted, the CUMULATIVE count
        reaches 3 on the third failure and it does -- verified both
        directions below, not merely reasoned about (see Step 5 of
        task-7-report.md).
        """
        readings = ["a", None, "b", None, "c", None, "d"]
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: readings.pop(0),
            fast_interval_seconds=0.001, slow_interval_seconds=0.001)
        self.addCleanup(watcher.stop)
        self.assertEqual(watcher._uuid_failures_before_fallback, 3,
                         "test precondition: the floor, not a derived ratio")
        for _ in range(len(readings)):
            watcher._fast_tick()
        self.assertFalse(watcher._uuid_tier_failed)
        self.assertEqual(watcher._uuid_failures, 0,
                         "the run must be freshly reset by the final (successful) reading")

    def test_the_log_line_states_the_true_duration_not_a_truncated_zero(self):
        """This file's OWN twice-shipped lesson -- _observe_tick's and
        _fast_tick's existing log lines, BOTH carry some variant of "%g, not
        %.1f: ... this is exercised with millisecond-scale intervals in
        tests, where %.1f renders '0.0s' and the line would misstate what
        the code actually did" -- applies a third time here: task-7-brief.md
        Step 3's snippet used %.0f for the duration, which renders any
        sub-second value as the literal string "0s". Not merely imprecise
        but FALSE, in the same spirit spec 5.3 names for a guessed CAUSE
        even though this line states an effect, not a cause.
        """
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: None,
            fast_interval_seconds=0.001, slow_interval_seconds=0.01)
        self.addCleanup(watcher.stop)
        for _ in range(watcher._uuid_failures_before_fallback):
            watcher._fast_tick()
        lines = self.marker_lines()
        self.assertEqual(len(lines), 1)
        self.assertNotIn("(0s)", lines[0],
                         "a real sub-second duration was rendered as exactly zero")

    def test_the_degrade_backoff_wins_the_interval_but_the_log_still_fires(self):
        """The pre-flight ruling (progress.md) and this task's own dispatch:
        Tasks 7 and 9 both write self._safety_net.interval, from DIFFERENT
        THREADS (this fallback from the fast-tier loop, the degraded backoff
        from the poll thread inside _observe_tick). The degraded backoff is
        the MORE SPECIFIC state -- a verdict was actually reached about this
        connection's tracker, not merely that one more D-Bus call failed --
        so it wins: this fallback's interval write is skipped while
        self._degraded is already set. The log line is NOT skipped -- it
        reports a distinct, still-true fact (the uuid CALL is failing)
        regardless of what interval the other diagnosis already chose.
        """
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(), read_history_uuid=lambda: None,
            fast_interval_seconds=0.001, slow_interval_seconds=0.01,
            degraded=True)
        self.addCleanup(watcher.stop)
        before = watcher._safety_net.interval
        self.assertEqual(
            before, agent.DEGRADED_POLL_SECONDS,
            "test precondition: an already-degraded watcher starts on the "
            "degraded interval, per GPasteWatcher.__init__'s own ternary "
            "when it builds self._safety_net -- NOT make_watcher, which is "
            "the never-had-GPaste path and is not what this test drives")
        for _ in range(watcher._uuid_failures_before_fallback + 2):
            watcher._fast_tick()
        self.assertTrue(watcher._uuid_tier_failed)
        self.assertEqual(
            len(self.marker_lines()), 1,
            "the fallback must still report what it observed even though "
            "its interval write below is deferred")
        self.assertEqual(
            watcher._safety_net.interval, before,
            "the more specific degraded backoff must not be overwritten by "
            "the flatter SAFETY_NET_POLL_SECONDS")

    def test_a_real_failure_run_read_by_a_real_slow_tick_reaches_no_verdict(self):
        """THE COMPOSED PATH, not the predicate in isolation. Every other
        test of this guard drives just one side of the seam: this class's
        own tests above call _fast_tick repeatedly but never call
        _observe_tick at all, while TestSlowTierVerdict's
        test_no_verdict_while_a_uuid_failure_run_is_in_progress hand-sets
        self._uuid_failures and calls _observe_tick without ever calling
        _fast_tick. Neither proves the two functions actually PRODUCE this
        state when run against each other, one real tick at a time -- the
        exact "a guard nothing connects" shape a mutation enumeration
        cannot see if nothing in the suite exercises the seam itself
        (task-5-report.md, row 1: deleting the fast tier's thread launch
        left the full suite green for the identical reason).

        The specific risk this closes: _observe_tick's own bottom line
        unconditionally does `self._uuid_at_last_tick = self._last_uuid` on
        EVERY tick, including ones where no verdict was reached -- so a
        REAL failure run (built by _fast_tick, which leaves self._last_uuid
        untouched while it runs) needs a REAL prior _observe_tick tick to
        have already caught self._uuid_at_last_tick up to that same stale
        value, or this test would not even reach the uuid_frozen==True
        precondition the guard exists to override. Hand-setting both
        fields to the same value (TestSlowTierVerdict.watcher()'s own
        convention) assumes that composition rather than demonstrating it.
        """
        readings = iter(["a"])   # one real success, then real failures forever
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(),
            read_history_uuid=lambda: next(readings, None),
            fast_interval_seconds=0.001, slow_interval_seconds=0.01)
        self.addCleanup(watcher.stop)

        watcher._fast_tick()   # measures "a" for real: self._last_uuid = "a"
        # A settled slow tick, exactly as a real prior one would leave the
        # connection: self._uuid_at_last_tick catches up to "a" too.
        watcher._observe_tick(("text", "x"), ("text", "x"))

        for _ in range(3):   # a REAL failure run, well short of the fallback threshold
            watcher._fast_tick()
        self.assertEqual(
            watcher._uuid_failures, 3,
            "test precondition: a run built by _fast_tick, not hand-set")
        self.assertFalse(
            watcher._uuid_tier_failed,
            "test precondition: short of the threshold, the fallback has not engaged")

        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertFalse(
            watcher._degraded,
            "a real uuid failure run, read by a real slow tick, was "
            "diagnosed as a dead tracker")

    def fallen_back(self, ever_measured=True):
        """A watcher driven into spec 4.3's POST-fallback state through the
        real composed path -- N real failing _fast_tick calls -- rather than
        by hand-setting self._uuid_tier_failed.

        `ever_measured` picks between the two histories a post-fallback
        connection can have, and they are not interchangeable: they used to
        produce OPPOSITE post-fallback behaviour from the same state, which
        is the second-order defect the tests below exist to pin. With a
        first successful reading, self._last_uuid holds it forever (the
        failures that follow never overwrite it) and a settling slow tick
        catches self._uuid_at_last_tick up to the same value, so the old
        uuid_frozen read permanently True. With NO successful reading ever,
        self._last_uuid stays None and the same expression read permanently
        FALSE. Post-fallback the uuid terms feed no decision at all, so both
        histories must now behave identically -- hence one helper, two
        callers, and a parameter rather than two near-copies.
        """
        readings = iter(["a"] if ever_measured else [])
        watcher = agent.GPasteWatcher(
            clipboard=_StubClipboard(),
            read_history_uuid=lambda: next(readings, None),
            fast_interval_seconds=0.001, slow_interval_seconds=0.001)
        self.addCleanup(watcher.stop)
        self.assertEqual(
            watcher._uuid_failures_before_fallback, 3,
            "test precondition: the floor, so the run below is short enough "
            "to read at a glance")
        if ever_measured:
            watcher._fast_tick()   # one real success: self._last_uuid = "a"
            # A settled slow tick, so self._uuid_at_last_tick catches up to
            # "a" as a real prior tick would have left it.
            watcher._observe_tick(("text", "x"), ("text", "x"))
        for _ in range(watcher._uuid_failures_before_fallback):
            watcher._fast_tick()
        self.assertTrue(
            watcher._uuid_tier_failed,
            "test precondition: the fallback engaged through real ticks")
        self.assertFalse(
            watcher._degraded,
            "test precondition: nothing has been diagnosed about the TRACKER "
            "yet -- only that the uuid call is failing")
        return watcher

    def test_a_live_signal_path_post_fallback_reaches_no_verdict(self):
        """THE DEFECT fix round 1 found, and the one the hand-set
        test_verdicts_resume_once_the_fallback_has_engaged could not see.

        Once self._uuid_tier_failed latched, every uuid term in the slow
        tier's predicate went constant: the failure-run guard was written to
        lift there, and self._last_uuid stops being updated so the frozen
        delta settles permanently True. The predicate degenerated to `if not
        read_ok:` -- the signal counter reached the decision NOWHERE -- so
        any two consecutive ticks in which the wl-paste token moved
        confirmed, whatever the signal path was doing.

        That is strictly WEAKER than both the predicate it claimed parity
        with (merge-base: `signals == self._signals_at_last_tick`) and the
        v3.3 one it replaced, and it re-creates the exact failure spec 4.0.1
        exists to prevent -- "a failing GetElementAtIndex plus an active user
        reads as 'uuid frozen while wl-paste moves' ... all because a method
        was renamed" -- merely displaced past the fallback boundary. Concrete
        shape: GetElementAtIndex breaks while the signal path is perfectly
        healthy (spec 4.3's own "a renamed method, an older interface"), and
        the user's next two copies across two consecutive slow ticks latch
        self._degraded permanently on a healthy machine.

        Signals arriving is what the restored discriminator sees here; the
        token moving on both ticks is what the broken one saw instead."""
        watcher = self.fallen_back()
        watcher._signals = 1   # the signal path is alive and reporting
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._signals = 2   # and it reports this copy too
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertFalse(
            watcher._degraded,
            "a healthy signal path was diagnosed as a dead tracker once the "
            "uuid tier's own fallback had engaged")

    def test_a_silent_signal_path_post_fallback_still_reaches_a_verdict(self):
        """THE MIRROR, and the reason the rule above is not "no verdict once
        the fallback engages" -- spec 4.0.1's closing sentence is "today's
        mechanics RESUME", not "detection stops". Today's mechanics
        (merge-base 58321cf:3505/3549) are the signal-gated predicate, and
        with the signal counter unmoved across two diverging ticks they
        confirm, exactly as they did before v3.3 existed."""
        watcher = self.fallen_back()
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertTrue(
            watcher._degraded,
            "today's mechanics did not resume once the fallback engaged")

    def test_a_takeover_post_fallback_still_does_not_degrade(self):
        """ONLY THE DISCRIMINATOR REVERTS -- the confirmation SHAPE does not.

        Merge-base's armed branch was `confirmed = True; self._armed =
        False`: a settled second tick CONFIRMED. Task 6 replaced that with
        settled-clears (spec 4.2), and fix round 1's ruling keeps it in BOTH
        regimes rather than reverting it along with the discriminator. The
        reason is that the false positive it kills -- GPaste taking the
        selection back and re-offering it one to six seconds after every
        copy, so an armed tick is followed by a settled one -- has nothing
        to do with the uuid tier and is present whether that tier is alive
        or dead. "Today's mechanics resume" cannot sensibly be read as
        "reintroduce the defect this release exists to remove."

        Without this test the both-regimes half of that ruling is only
        structural (one shared branch, so it must hold): a mutant that
        restored merge-base's shape for the post-fallback case alone would
        go uncaught, since test_a_takeover_does_not_degrade above drives the
        ALIVE regime only. Signals stay silent throughout, so the restored
        discriminator says "looks dead" on both ticks and the settle is the
        only thing standing between this connection and a false verdict."""
        watcher = self.fallen_back()
        watcher._observe_tick(("image", ("image/png",)), ("image", TWENTY_THREE))
        watcher._observe_tick(("image", TWENTY_THREE), ("image", TWENTY_THREE))
        self.assertFalse(
            watcher._degraded,
            "GPaste's own re-offer degraded the connection once the uuid "
            "tier's fallback had engaged")

    def test_a_failed_probe_post_fallback_clears_an_armed_run(self):
        """`not read_ok` gates BOTH regimes, which is what "today's mechanics
        restored exactly" has to mean -- merge-base's predicate was `if not
        read_ok or not still_silent:` and the read_ok half is not optional.

        probe() returns None for a timed-out wl-paste and for an empty
        selection alike, so it is evidence about nothing either way; a run
        armed before it must CLEAR rather than confirm on the next
        divergence. Fix round 1 disclosed this as the one hole its mutation
        table could not see: the term is shared by both branches in the
        source, so deleting it reds pre-existing alive-regime tests, and
        nothing exercised a failed probe once the fallback had engaged.
        Cheap to close, so closed rather than left as a disclosure."""
        watcher = self.fallen_back()
        watcher._observe_tick(("text", "a"), ("text", "b"))   # arms
        watcher._observe_tick(("text", "b"), None)            # probe failed: must CLEAR
        watcher._observe_tick(("text", "b"), ("text", "c"))   # re-arms; must not confirm
        self.assertFalse(
            watcher._degraded,
            "a failed wl-paste probe post-fallback was treated as evidence "
            "of a dead tracker instead of clearing the armed run")

    def test_a_uuid_never_measured_even_once_behaves_the_same_post_fallback(self):
        """THE SECOND-ORDER FORK, closed by the same change. Before it,
        post-fallback behaviour depended on an irrelevant historical
        accident: whether a uuid had ever been measured successfully at all.

          - measured once, then persistent failure: self._last_uuid holds
            that value forever, the frozen delta settles permanently True,
            and the tier OVER-degraded (the test above).
          - never measured (GetElementAtIndex renamed before this connection
            ever started -- spec 4.3's own scenario, and the likelier of the
            two): self._last_uuid stays None, the frozen delta reads
            permanently FALSE, and the slow tier reached NO VERDICT AT ALL
            for the rest of the connection. Silently worse than pre-v3.3,
            which had no uuid tier to get stuck behind.

        Neither fork was "today's mechanics". Dropping the uuid terms out of
        the post-fallback decision closes both at once, which is what this
        test and the two above assert TOGETHER: identical setup apart from
        the one reading, identical outcome."""
        watcher = self.fallen_back(ever_measured=False)
        self.assertIsNone(
            watcher._last_uuid,
            "test precondition: no uuid was ever measured on this connection")
        watcher._observe_tick(("text", "a"), ("text", "b"))
        watcher._observe_tick(("text", "b"), ("text", "c"))
        self.assertTrue(
            watcher._degraded,
            "a connection that never measured a uuid reached no verdict at "
            "all post-fallback, instead of today's mechanics")


class _WriteOrderRecorder(agent.GPasteWatcher):
    """Records the ORDER in which _fast_tick's success branch stores the
    pair _observe_tick reads: self._last_uuid and self._uuid_failures.

    Recording is off until a test arms it, so __init__'s own writes to both
    fields (and the failure branch's increments) do not pollute the log."""

    _recording = False
    writes = None

    def __setattr__(self, name, value):
        if self._recording and name in ("_last_uuid", "_uuid_failures"):
            self.writes.append(name)
        object.__setattr__(self, name, value)


class TestUuidPairWriteOrder(unittest.TestCase):
    """Fix round 1 reported the write-order swap as an UNCATCHABLE guard --
    a mutation (M23) that no test could red, because the defect it prevents
    needs a poll-thread read to land between two fast-thread stores and
    nothing here schedules threads at that granularity.

    That conflated two different things, and the re-review was right to
    split them. The INTERLEAVE is genuinely unschedulable, and a test that
    tried would be testing CPython rather than this code. But what the
    mutation actually changes is the WRITE ORDER, which is a property of a
    single thread's execution and is therefore observable exactly, with no
    scheduler involved at all. One failing tick, one succeeding tick, and a
    recording __setattr__ is the whole apparatus.

    Worth the twenty lines for a reason beyond closing a table row: read
    together with the residual disclosure in _fast_tick's `else` branch,
    the write order is the least-verified and most-strongly-argued part of
    this task's diff, and an argument nothing checks is how the previous
    two rounds each shipped their worst defect."""

    def test_the_value_is_stored_before_the_run_is_reset(self):
        readings = iter([None, "a"])   # one failure, then a real success
        watcher = _WriteOrderRecorder(
            clipboard=_StubClipboard(),
            read_history_uuid=lambda: next(readings, None),
            fast_interval_seconds=0.001, slow_interval_seconds=0.001)
        self.addCleanup(watcher.stop)

        watcher._fast_tick()   # the failure: builds a run to be reset
        self.assertEqual(
            watcher._uuid_failures, 1,
            "test precondition: a run is in progress, so the success below "
            "actually has something to reset")

        watcher.writes = []
        watcher._recording = True
        watcher._fast_tick()   # the success: both stores happen in here
        watcher._recording = False

        self.assertEqual(
            watcher.writes, ["_last_uuid", "_uuid_failures"],
            "the success branch must store the VALUE before clearing the "
            "failure count. The other order lets a poll-thread reader see "
            "'no failure run in progress' beside a self._last_uuid still "
            "holding the stale pre-run value -- exactly the pair the "
            "failure-run guard exists to reject")


if __name__ == "__main__":
    unittest.main()
