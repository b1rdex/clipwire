"""Spec 1.2's false verdict, driven against the REAL fakes.

Design: docs/superpowers/specs/2026-08-03-v3.3-focus-free-detection-design.md

THE INCIDENT THIS RECONSTRUCTS, production `2026-08-02T04:21:52Z`: an image
lands, the watcher takes its baseline, GPaste takes the selection back and
re-offers the same picture under its own long type list WITH NO `Update`, and
the quiet ticks that follow confirm a dead event source. Before v3.3 the
connection then dropped to 1 Hz `wl-paste` polling -- stealing keyboard focus
on every tick, on a machine where nothing was wrong -- for the rest of the
connection.

WHY THIS FILE FORKS Tests/fakes WHEN NOTHING ELSE IN agent/tests DOES, said
here because it is a new pattern in this directory and should not be mistaken
for house style. Every other test here patches `subprocess.Popen` and injects
`read_history_uuid`, which is right for pinning a rule and is what
TestSlowTierVerdict.test_a_takeover_does_not_degrade already does for THIS
rule -- it calls `_observe_tick` twice, by hand, with both uuid fields primed.
That test is not replaced and is not duplicated here; what it cannot see is
everything between the rule and the machine:

  - that `Tests/fakes/gdbus monitor` really WITHHOLDS the `Update` for a
    silent state change, while the agent's own pump thread is reading its
    stdout. That capability (spec 9.0's first) was built by v3.3 task 2 and
    until this file had exactly one consumer: the fakes' own suite. A
    capability nobody connects is this project's recorded defect shape;
  - that `Tests/fakes/gdbus call GetElementAtIndex` -- spec 9.0's second
    capability, built by task 1 -- really reports a FROZEN uuid across the
    re-offer, because the fake derives it from `generation` and a re-offer
    creates no history entry;
  - that the real `wl-paste --list-types` really reports the moved type list,
    so the divergence the slow tier judges is one a real fork observed rather
    than one a stub was told to report;
  - that all of it holds with the three real threads running (the gdbus pump,
    the fast tier, the safety net) against real intervals, rather than with
    `_observe_tick` called synchronously from the test's own thread.

WHAT THIS FILE IS NOT, and the reason is worth reading before assuming this
closes the plan's task 10. The end-to-end test that release names -- the two
IMPLEMENTATIONS paired, a clip crossing a real pipe to the Swift side -- is
`Tests/clipwireTests/PairingHarnessTests.swift`, and it cannot host this
scenario today: the verdict lives on the safety net's poll thread, and NOTHING
the harness can set changes that thread's interval. `make_watcher` passes no
`safety_net_interval_seconds`, so every connection's poller gets the
`SAFETY_NET_POLL_SECONDS` constant, 30 s; `CLIPWIRE_SLOW_TIER_SECONDS` reaches
only `self._slow_interval`, whose one consumer is
`_uuid_failures_before_fallback`. The plan's task 3 named a third variable,
`CLIPWIRE_SAFETY_NET_SECONDS`, alongside the two that shipped, and it was
never implemented -- so spec 9.0's third capability, "interval injection into
the agent spawned by the harness", is injection of the one interval this
scenario is measured in. Reproducing it there costs three 30-second ticks
against a Swift suite that runs in under ten seconds. This file is what can be
honest today, at a quarter of a second per tick, because a constructor
argument reaches what an environment variable does not.

Spec 9.0's FIRST capability also shipped in a different shape than the spec
asked for, and this file depends on the difference rather than papering over
it: the spec wanted the takeover fired "after the Nth probe", inside the fake.
What task 2 built is a `silent` flag the caller sets, with no counter -- so
the determinism has to come from the caller instead, and it does: the takeover
below is written after an OBSERVED condition (the slow tier having snapshotted
a uuid), never after a sleep. Same guarantee, different owner.
"""
import base64
import json
import os
import pathlib
import shutil
import tempfile
import time
import unittest

import agent_under_test as agent
# The loaded module under its own name, so `log` can be captured: log() is
# resolved against clipwire_agent's globals, not agent_under_test's. Same
# import and same reason as TestFastTierIntegration in
# test_watcher_uuid_tier.py.
import clipwire_agent
# ONE owner for the type list GPaste re-offers an image under, rather than a
# second copy of it here. NOTE, since this file writes it into a real
# clipboard and a reader will count it: the constant holds TWENTY-TWO
# members, not the twenty-three its name claims -- verified by `len()`. The
# rule under test is indifferent to the number (any list that differs from
# `image/png` moves the token), so nothing here rests on it, and the missing
# member is not invented back.
from test_watcher_uuid_tier import TWENTY_THREE

FAKES = pathlib.Path(__file__).resolve().parents[2] / "Tests" / "fakes"

# Generous relative to the sub-second intervals below, matching test_watcher's
# JOIN_TIMEOUT convention. Not imported from there: this file waits on real
# subprocesses rather than on thread joins, and a fake invocation has been
# measured at 25-30 ms warm with a first call of up to 0.8 s (see
# fake_clipboard.py, "WHAT THEY COST"), so the budget is this file's own.
WAIT_TIMEOUT = 10

# The slow tier, as a constructor argument -- the knob the Swift harness has
# no way to reach. Everything this file measures is one of these ticks.
SLOW_SECONDS = 0.25
FAST_SECONDS = 0.05
# Spec 4.2's divergence re-probe, kept BELOW the slow tick so the excursion
# actually happens (the call site takes min() with the interval the net is
# on) and above the fast tick so a uuid reading can land inside it.
REPROBE_SECONDS = 0.1

# The same 2x2 PNG the Swift harness copies, byte for byte -- a real image,
# so `choose_kind` reads KIND_IMAGE and `probe()` returns the offered TYPE
# LIST rather than a body, which is the token spec 1.2 is about.
PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
    0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x02,
    0x00, 0x00, 0x00, 0x02, 0x08, 0x06, 0x00, 0x00, 0x00, 0x72, 0xB6, 0x0D, 0x24,
    0x00, 0x00, 0x00, 0x09, 0x70, 0x48, 0x59, 0x73, 0x00, 0x00, 0x16, 0x25,
    0x00, 0x00, 0x16, 0x25, 0x01, 0x49, 0x52, 0x24, 0xF0,
    0x00, 0x00, 0x00, 0x11, 0x49, 0x44, 0x41, 0x54, 0x78, 0xDA, 0x63, 0xF8,
    0xCF, 0xC0, 0x00, 0x42, 0xFF, 0x19, 0x60, 0x0C, 0x00, 0x43, 0xCE, 0x07,
    0xF9, 0x25, 0x02, 0xF9, 0xBE,
    0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
])

# The prefix of the verdict line, matched rather than the whole sentence: the
# rest of it interpolates evidence fields and two intervals, and pinning those
# here would make a tuning change look like a regression. The prefix is byte
# for byte what merge-base 58321cf logged too, which is what lets the same
# assertion be read against an older agent.
VERDICT = "GPaste reported no clipboard change"


class TestAGPasteReofferIsNotADeadEventSource(unittest.TestCase):
    """Spec 1.2 and spec 9.1's first acceptance criterion, as far as this
    suite can carry them.

    MEASURED RED, which is the only thing that makes this evidence rather
    than decoration. Run against `git show 58d3c60:agent/clipwire-agent.py`
    -- the commit before `6ac6eec`, which introduced BOTH the uuid
    discriminator and spec 4.2's settled-clears rule -- this test fails at
    `_degraded`, having passed every positive assertion above it first, and
    the captured log holds:

        GPaste reported no clipboard change while the content changed
        (signals=1 signals_at_last_tick=1 pump_alive=True worker_alive=True);
        the gnome-shell extension being disabled is one possible cause.
        Polling every 1s for the rest of this connection.

    `signals=1` is the production incident's own shape: one Update for the
    copy, none for the re-offer -- and it is also why merge-base's predicate
    fires, since `signals == self._signals_at_last_tick` reads 1 == 1 at the
    arming tick. That run needs FOUR edits this file does not carry, because
    58d3c60 predates what it passes and reads: drop the three constructor
    arguments `reprobe_interval_seconds`, `read_idle_gate` and
    `read_tracking`, and wait on `_signals_at_last_tick == 1` in place of
    `_uuid_at_last_tick is not None`, a field that does not exist until
    `58196e9`. Merge-base `58321cf` reaches the same verdict, established
    with a standalone driver rather than with this test: it has no fast tier
    at all, so the uuid assertions below have nothing to read.

    WHICH HALF OF THE FIX THIS SCENARIO ACTUALLY RESTS ON, measured by
    mutation and stated because the answer is not the obvious one: SETTLED-
    CLEARS. Reverting the armed branch alone to merge-base's unconditional
    `confirmed = True` turns this test red; reverting the DISCRIMINATOR alone
    to `signals == self._signals_at_last_tick` leaves it green, because a
    settled token still clears the run. So this file is spec 4.2's evidence,
    not spec 4.0's -- the uuid tier's own rows are covered by
    TestSlowTierVerdict in test_watcher_uuid_tier.py, and what is asserted
    here about the uuid is only that it stays frozen, i.e. that the
    discriminator is being handed the evidence it is entitled to.
    """

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="clipwire-reoffer-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.state = os.path.join(self.root, "clipboard.json")
        runtime = os.path.join(self.root, "runtime")
        os.makedirs(runtime)

        # The fakes ahead of anything real, and the state file they share.
        # Restored rather than left set: this process runs the whole suite,
        # and a leaked PATH would silently hand a later test a fake gdbus.
        self.export("PATH", str(FAKES) + os.pathsep + os.environ.get("PATH", ""))
        self.export("CLIPWIRE_FAKE_CLIPBOARD_STATE", self.state)
        # clipboard_env() rewrites XDG_RUNTIME_DIR for every subprocess it
        # spawns, so pointing it at this run's own directory is what keeps a
        # fake from being handed the developer's real session path.
        self.export("XDG_RUNTIME_DIR", runtime)

        # Captured because the test READS it -- the verdict line is the
        # user-visible half of the defect -- and, secondarily, because the
        # agent force-logs the duration of its first clipboard read, which
        # would otherwise print to a real stderr in the middle of a suite
        # run. Nothing else here reaches the log: the idle gate's `gdbus
        # call` at org.gnome.Mutter.IdleMonitor IS refused by this fake, but
        # _user_recently_active captures that subprocess's output and returns
        # None without logging, which is also why the refusal is harmless
        # (None means "not measured", and the gate proceeds).
        original = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original)

    def export(self, name, value):
        previous = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(self.restore, name, previous)

    @staticmethod
    def restore(name, previous):
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous

    # -- acting as the two halves of the world ---------------------------

    def write_state(self, **changes):
        """Every key not named is carried over -- the same rule
        fake_clipboard.set_body follows, so a takeover cannot silently
        rebuild the entry the uuid is derived from.

        With one difference from set_body, and it is deliberate: that
        function CLEARS `silent`, because a write reaching it came from the
        agent's own wl-copy and a real write is never silent. This one is the
        harness's own hand, so it clears nothing and each caller says what it
        means -- see the two below, one of which sets `silent` and one of
        which explicitly puts it back."""
        try:
            with open(self.state, "rb") as handle:
                state = json.loads(handle.read().decode("utf-8"))
        except FileNotFoundError:
            state = {"substitute": False}
        state.update(changes)
        temporary = self.state + ".tmp"
        with open(temporary, "wb") as handle:
            handle.write(json.dumps(state).encode("utf-8"))
        # Atomically: the fake gdbus digests the whole file every 50 ms and
        # wl-paste parses it, so a torn write would produce both a spurious
        # Update and a fake that cannot read its own state.
        os.replace(temporary, self.state)

    def copy_an_image_on_the_pc(self):
        """A person copying a screenshot: one offered type, a new history
        entry (`generation`), and an `Update` for it."""
        self.write_state(types=["image/png"],
                         body=base64.b64encode(PNG).decode("ascii"),
                         generation="copied-%d" % time.time_ns(),
                         silent=False)

    def gpaste_takes_the_selection_back(self):
        """THE EVENT THE RELEASE IS ABOUT. The offered type list changes and
        NOTHING ELSE DOES:

          - `generation` is untouched, so the fake's `history_uuid` -- derived
            from it -- stays frozen. That is the whole point: GPaste re-offers
            its OWN content and creates no history entry;
          - `body` is untouched, because the picture did not change;
          - `silent` is set, which makes the fake gdbus monitor absorb this
            one state change into its baseline and emit no `Update`.

        `silent` is NOT self-clearing -- only an agent write through
        `wl-copy` clears it (fake_clipboard.set_body) -- so this must be the
        last direct state write a test makes, or every later one is invisible
        to the monitor too.
        """
        self.write_state(types=list(TWENTY_THREE), silent=True)

    # -- waiting ----------------------------------------------------------

    def wait_until(self, predicate, what):
        deadline = time.monotonic() + WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        self.fail("timed out waiting for %s\n%s" % (what, self.diagnostics()))

    def wait_for_the_monitor(self):
        """Until the fake gdbus monitor has taken its baseline digest.

        Not politeness, and PairingHarness.assertTheAgentIsOnTheEventPath
        carries the same two steps for the same reason: the monitor digests
        the whole state file once at startup and treats any later difference
        as a change, so a copy written before that digest is absorbed into it
        and NO Update follows. The test would then be judging a scenario in
        which the signal path never reported the copy either -- silence for
        two reasons, one of them the harness's own fault.

        The log line goes out immediately BEFORE the digest is taken, so a
        few of the monitor's own 0.05 s poll intervals close the last gap.
        """
        self.wait_until(lambda: "monitor started" in self.invocations(),
                        "the fake gdbus monitor to start")
        time.sleep(0.2)

    def invocations(self):
        try:
            with open(self.state + ".log", "r", encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return ""

    def diagnostics(self):
        """Both logs, in the failure itself. The fakes' invocation log is the
        one that matters here and is not decoration: "the agent never forked
        us" and "the clipboard was empty" are otherwise the same silence."""
        return ("--- the agent's log ---\n%s\n--- what the agent forked ---\n%s"
                % ("\n".join(self.log_lines),
                   self.invocations() or "(the fakes were never invoked)"))

    def watcher(self):
        """Production's wiring, plus the three intervals it defaults.

        Only ONE of those three is the reason this test cannot go through
        `make_watcher`: `fast_interval_seconds` has an environment override
        (`CLIPWIRE_FAST_TIER_SECONDS`) and could have been set from outside,
        while `safety_net_interval_seconds` -- the tick every verdict here is
        measured in -- and `reprobe_interval_seconds` have none, and
        `make_watcher` accepts neither. Naming that distinction here rather
        than lumping the three together: it is the whole of why this file
        exists in this suite instead of the Swift one.

        And going around `make_watcher` costs something, which is why the two
        D-Bus readers below are passed by hand: that function is the only
        place production wires the idle gate and the tracking read, so a
        watcher built anywhere else silently runs ungated. `read_history_uuid`
        is left at its default on purpose, so the fast tier forks the real
        `gdbus call` rather than a stub.
        """
        watcher = agent.GPasteWatcher(
            agent.WaylandClipboard(),
            safety_net_interval_seconds=SLOW_SECONDS,
            fast_interval_seconds=FAST_SECONDS,
            reprobe_interval_seconds=REPROBE_SECONDS,
            read_idle_gate=agent._user_recently_active,
            read_tracking=agent.gpaste_tracking,
        )
        self.addCleanup(watcher.stop)
        return watcher

    def test_a_gpaste_reoffer_does_not_degrade_the_connection(self):
        watcher = self.watcher()
        self.assertTrue(
            watcher.available(),
            "the fake gdbus did not answer `introspect`, so this run would "
            "have tested the polling fallback instead of the event path")

        changes = []
        watcher.start(lambda: changes.append(time.monotonic()))

        # 1. THE IMAGE LANDS, through the real event path: the fake gdbus
        #    monitor sees the state file move, prints a real Update line down
        #    a real pipe, the pump counts it and the worker runs the handler.
        self.wait_for_the_monitor()
        self.copy_an_image_on_the_pc()
        self.wait_until(lambda: changes, "the agent to observe the copy")
        signals_after_the_copy = watcher._signals
        self.assertEqual(signals_after_the_copy, 1,
                         "exactly one Update per copy, which is what makes "
                         "the silence below mean something")

        # 2. THE WATCHER TAKES ITS BASELINE. The fast tier has to have read a
        #    uuid AND a slow tick has to have snapshotted it: `uuid_frozen`
        #    is a delta against the last tick, so a brand new watcher's first
        #    tick can never see it True. Waiting the warm-up out is what
        #    stops this test passing for that reason instead of the one it
        #    is about.
        self.wait_until(lambda: watcher._uuid_at_last_tick is not None,
                        "the slow tier's first tick to snapshot a uuid")
        uuid_before = watcher._last_uuid
        self.assertIsNotNone(
            uuid_before,
            "the fast tier never read a history uuid through the fake "
            "`gdbus call`, so there is no tracking evidence to judge against")

        # 3. GPASTE RE-OFFERS ITS OWN CONTENT, silently.
        #
        #    Pinned first, because the whole scenario rests on it and the
        #    constant it rests on lives in another file: the re-offered list
        #    must still read as an IMAGE. `probe()` returns the offered type
        #    list for an image and the BODY for text, and the body does not
        #    change across a re-offer -- so a list that tipped `choose_kind`
        #    to text would leave the token motionless, no divergence would be
        #    observed, and this test would pass having staged nothing. The
        #    list carries `text/ico`, which is close enough to that cliff to
        #    be worth an assertion rather than an argument.
        self.assertEqual(
            agent.choose_kind(list(TWENTY_THREE)), agent.KIND_IMAGE,
            "the re-offered type list no longer reads as an image, so the "
            "token below is a body that a re-offer cannot move")
        self.gpaste_takes_the_selection_back()

        # 4. The tick that sees the divergence, and the one that resolves it.
        #    Polled rather than slept through, so what is asserted below is
        #    that the run was ARMED and then let go -- not that a window
        #    passed with nothing in it.
        armed = []
        deadline = time.monotonic() + WAIT_TIMEOUT
        while time.monotonic() < deadline:
            if watcher._armed:
                armed.append(time.monotonic())
            if watcher._degraded or (armed and not watcher._armed):
                break
            time.sleep(0.002)

        # -- what actually happened, before what did not ------------------

        self.assertEqual(
            watcher._signals, signals_after_the_copy,
            "the fake gdbus monitor emitted an Update for the re-offer. This "
            "test then proves nothing: the whole scenario is a selection "
            "change the signal path never reports")
        self.assertEqual(
            watcher._last_uuid, uuid_before,
            "the history uuid moved across the re-offer. On the real machine "
            "it does not -- GPaste re-offering its own content adds no entry "
            "-- and if it moves here the fast tier is being handed the very "
            "evidence the slow tier needs to be denied")
        self.assertTrue(
            armed,
            "the slow tier never saw the divergence at all, so 'it reached no "
            "verdict' is a claim about a tick that did not happen")

        # -- and the verdict that must not have been reached ---------------

        self.assertFalse(
            watcher._degraded,
            "GPaste's own re-offer was diagnosed as a dead event source: the "
            "connection is now polling wl-paste, and stealing focus, for the "
            "rest of its life")
        # A SECOND READING OF ONE LATCH, not a second guard, and that is said
        # here rather than left to be discovered: the line and self._degraded
        # are written by the same block behind the same condition, so no
        # mutation THIS scenario can reach kills this assertion without
        # killing the one above it first. Checked, not assumed -- corrupting
        # the latch assignment while leaving the log call in place leaves this
        # test green, because with the fix in place `confirmed` never becomes
        # True and the block never runs at all. It is kept because this
        # sentence is the artifact three production incidents were actually
        # read from, and because a future release that moved the reporting
        # out from behind the latch would be caught here rather than on a
        # user's machine.
        self.assertEqual(
            [line for line in self.log_lines if VERDICT in line], [],
            "the verdict line was logged. Three production incidents were "
            "misdiagnosed from this sentence; it must not be reachable by a "
            "healthy machine's clipboard being re-offered to it")


if __name__ == "__main__":
    unittest.main()
