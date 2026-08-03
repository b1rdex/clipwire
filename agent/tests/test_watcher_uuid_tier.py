"""The v3.3 fast tier: GPaste's history uuid as the change token.

Design: docs/superpowers/specs/2026-08-03-v3.3-focus-free-detection-design.md
"""
import os
import subprocess
import unittest

import agent_under_test as agent


class _StubClipboard:
    """Enough clipboard for a watcher to be constructed. The fast tier never
    touches it -- that is the point of the tier."""
    def probe(self):
        return None

    def read(self):
        return None


class TestIntervalInjection(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS", None)

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
        here. A discriminating case would continue with a THIRD distinct
        reading (e.g. ["a", None, "b"]) and assert the signal DOES fire --
        proving the baseline survived the None untouched rather than merely
        proving a None does not fire one of its own. Not added here: see the
        mutation table for why this gap is reported rather than silently
        closed.
        """
        watcher = self.watcher(["a", None, "a"])
        watcher._fast_tick()
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set(), "a failed call signalled")
        watcher._fast_tick()
        self.assertFalse(watcher._event.is_set(),
                         "recovery from a failed call signalled a change nobody made")


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

    def tearDown(self):
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


if __name__ == "__main__":
    unittest.main()
