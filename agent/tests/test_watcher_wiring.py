# agent/tests/test_watcher_wiring.py
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
    UpgradingWatcher,
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
    QueueClipboard,
    SpyWatcher,
)
class TestMakeWatcher(unittest.TestCase):
    def test_uses_gpaste_when_available(self):
        clipboard = object()
        on_degrade = object()
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=clipboard, on_degrade=on_degrade)
        self.assertIsInstance(watcher, GPasteWatcher)
        self.assertIs(
            watcher.clipboard, clipboard,
            "the clipboard must reach the watcher, or its safety net polls nothing",
        )
        # The other half of the same forwarding contract, and the one this
        # class never checked: `degraded` (the verdict handed IN) is pinned
        # by the two tests below, but `on_degrade` -- the callback that
        # carries the verdict back OUT to Agent._note_event_source_degraded
        # -- was not asserted anywhere. It is the single production link
        # that makes the degraded latch connection-scoped rather than
        # watcher-scoped: without it, clipboard_lost/clipboard_became_ready
        # discards the watcher that reached the verdict, the rebuilt one
        # starts undiagnosed, and a mid-connection Wayland flap puts
        # PC-to-Mac sync back on the 30-second detection budget on an
        # installation already known to be broken. Verified by mutation:
        # hardcoding `on_degrade=None` at the forwarding site below left the
        # entire suite green before this assertion existed.
        self.assertIs(
            watcher._on_degrade, on_degrade,
            "the degraded verdict must be able to travel back out to the Agent, "
            "or the latch dies with the watcher that reached it",
        )

    def test_the_watcher_line_reports_the_interval_the_poller_actually_got(self):
        """Spec 5.3, applied to the line a person reads FIRST.

        This line stated the SAFETY_NET_POLL_SECONDS constant while the poller
        it had just built ran at whatever CLIPWIRE_SAFETY_NET_SECONDS resolved
        to -- so a harness run's agent log said "every 30s" during a 0.4 s
        tier. It was reachable only from a harness, which is the aggravation
        and not the mitigation: that log is what somebody reads when a harness
        test fails, and telling them the tier is 75x slower than it is costs
        more than a line nobody reads.

        ASSERTED AGAINST THE POLLER'S OWN INTERVAL rather than against a
        literal, so this cannot be satisfied by a second hardcoded number
        agreeing with the first. The env-var phase is the one that kills the
        original defect: with the constant restored, the line reads "30s"
        while `_safety_net.interval` is 0.4.

        The FORMAT is pinned by the same phase, and deliberately: %.0f renders
        0.4 as "0", so a line reading "every 0s" would be a second false
        statement in the same sentence and this assertion would still pass if
        it only compared numbers loosely.
        """
        self.addCleanup(os.environ.pop, "CLIPWIRE_SAFETY_NET_SECONDS", None)
        os.environ.pop("CLIPWIRE_SAFETY_NET_SECONDS", None)

        for injected, expected in ((None, "%g" % SAFETY_NET_POLL_SECONDS), ("0.4", "0.4")):
            if injected is None:
                os.environ.pop("CLIPWIRE_SAFETY_NET_SECONDS", None)
            else:
                os.environ["CLIPWIRE_SAFETY_NET_SECONDS"] = injected
            lines = []
            with mock.patch.object(clipwire_agent, "log", lines.append), \
                    mock.patch.object(GPasteWatcher, "available", return_value=True):
                watcher = make_watcher(clipboard=object())
            watcher.stop()
            reported = [line for line in lines if "safety-net poll every" in line]
            self.assertEqual(
                len(reported), 1,
                "make_watcher must say once which watcher it built: %r" % lines)
            self.assertIn(
                "safety-net poll every %ss" % expected, reported[0],
                "the line must report the interval the poller was given, not the "
                "constant it defaults from (CLIPWIRE_SAFETY_NET_SECONDS=%r)" % injected)
            self.assertEqual(
                "%g" % watcher._safety_net.interval, expected,
                "and the poller must actually be on it, or the line above is "
                "agreeing with a number nothing runs at")

    def test_falls_back_to_polling_when_gpaste_unavailable(self):
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)
        self.assertIsInstance(watcher, PollingWatcher)
        self.assertEqual(watcher.interval, 2.5)

    def test_the_polling_fallback_defaults_to_the_interval_degraded_mode_uses(self):
        """One constant, two modes: a machine that never had GPaste and a
        connection whose GPaste went silent must poll at the same rate, or the
        two silently drift the next time one of them is tuned."""
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object())
        self.assertEqual(watcher.interval, DEGRADED_POLL_SECONDS)

    def test_the_fallback_interval_is_the_one_knob_for_both_degraded_modes(self):
        """The previous test only pins the never-had-GPaste branch, and pins the
        default value at that -- so it cannot see the GPaste branch quietly
        keeping its own hardcoded rate. `fallback_interval_seconds` means "how
        fast we poll when signals cannot be relied on", and there are two ways
        to arrive there: GPaste was never available, or its event source was
        diagnosed silent. Tuning the knob has to move both, or make_watcher ends
        up logging one value while polling another."""
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)
        self.assertEqual(
            watcher._degraded_interval, 2.5,
            "the GPaste watcher's degraded rate must come from the same knob",
        )

        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            rebuilt = make_watcher(clipboard=object(), fallback_interval_seconds=2.5,
                                   degraded=True)
        self.assertEqual(
            rebuilt._safety_net.interval, 2.5,
            "and a watcher rebuilt already degraded must come up polling at it",
        )

    def test_the_idle_tick_predicate_reaches_both_watcher_shapes(self):
        """And the plain poller is the branch that matters, which is why
        both are asserted here rather than only the interesting-looking one.

        A machine with no GPaste gets the fallback below, and it is exactly
        the machine whose applied image is never re-offered -- so a hook
        wired only into the GPaste watcher would be inert in the one
        installation Task 5 exists for. That is the shape of the two fixes
        this bug has already outlived: correct code that no production path
        reaches."""
        predicate = object()

        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            gpaste = make_watcher(clipboard=object(), on_idle_tick=predicate)
        self.assertIs(
            gpaste._safety_net._on_idle_tick, predicate,
            "the GPaste watcher must hand it to the safety-net poll it composes",
        )

        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            plain = make_watcher(clipboard=object(), on_idle_tick=predicate)
        self.assertIs(
            plain._on_idle_tick, predicate,
            "and the no-GPaste fallback must get it too: that is the machine whose "
            "re-offer never comes, so a hook it never receives is a fix that is inert "
            "exactly where it is needed",
        )

    def test_make_watcher_is_what_wires_the_idle_gate_and_the_tracking_read(self):
        """THE OTHER END OF A DELIBERATE DEFAULT. GPasteWatcher defaults both
        of these D-Bus readers to None -- see its __init__ for the rule (a
        reader whose absence only COSTS may default off; one whose absence
        breaks a verdict may not) -- and that default is only safe because
        exactly one production path wires them.

        Without this test the failure is silent and total: drop either
        argument from make_watcher and every machine runs the slow tier
        ungated and reports "gpaste_Active=unavailable" forever, with no
        test red and no log line to say so. That is the same "correct code no
        production path reaches" shape the idle-tick test above was written
        for, which is why the two sit together.

        Identity, not truthiness: `read_idle_gate=lambda: True` would pass a
        truthiness check while gating nothing on the real IdleMonitor."""
        with mock.patch.object(GPasteWatcher, "available", return_value=True):
            watcher = make_watcher(clipboard=object())

        self.assertIs(
            watcher._read_idle_gate, clipwire_agent._user_recently_active,
            "spec 4.2's idle gate is wired nowhere else, so an unwired "
            "production watcher forks a wl-paste on every slow tick forever",
        )
        self.assertIs(
            watcher._read_tracking, clipwire_agent.gpaste_tracking,
            "spec 5.3's reading is wired nowhere else, so an unwired "
            "production watcher reports the property it replaced a guess with "
            "as permanently unavailable",
        )
        # assertEqual, not assertIs: bound methods compare equal by
        # (__func__, __self__) but are fresh objects on every attribute
        # lookup, so identity here would fail against correct code.
        self.assertEqual(
            watcher._safety_net._should_probe, watcher._slow_tier_should_probe,
            "and the gate must actually reach the poll loop through the "
            "None-proceeds predicate, not merely be stored on the watcher",
        )

    def test_the_standalone_poller_is_never_gated(self):
        """Spec 2: the machine with no GPaste at all is untouched. Its
        clipboard may legitimately never move again -- that is the machine the
        re-offer hook above exists for -- so gating its ticks on user activity
        would be the wrong trade there even if it were free."""
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            plain = make_watcher(clipboard=object())
        self.assertIsNone(
            plain._should_probe,
            "the standalone path must reach the poll loop with no gate at all",
        )


class FlapRecordingWatcher:
    """Records the degraded-latch wiring make_watcher was handed, and can fire
    the diagnosis on demand -- standing in for a real safety-net verdict
    without any thread or subprocess."""

    def __init__(self, degraded=False, on_degrade=None, on_idle_tick=None):
        self.degraded = degraded
        self._on_degrade = on_degrade
        # Recorded rather than ignored: the disarm that keeps a re-offer
        # expectation from swallowing the user's next image copy is reached
        # only through this hook, so a rebuild that quietly dropped it would
        # leave every connection after the first Wayland flap with an
        # expectation nothing can ever disarm.
        self.on_idle_tick = on_idle_tick
        self.started_with = None
        self.stopped = False

    def start(self, on_change):
        self.started_with = on_change

    def stop(self):
        self.stopped = True

    def diagnose(self):
        """What GPasteWatcher._observe_tick does once it concludes the event
        source is dead."""
        self.degraded = True
        self._on_degrade()


class TestWatcherLifecycleWiring(unittest.TestCase):
    """clipboard_became_ready()/clipboard_lost() must create and start a
    watcher exactly once per session, and stop it the moment the session
    goes away -- a gdbus monitor against a dead session is pointless.
    make_watcher is patched throughout so these tests never touch a real
    subprocess or thread."""

    def setUp(self):
        # clipboard_became_ready()'s new announce step persists through
        # save_clip_state/load_clip_state, which touch the real production
        # path when clip_state_path is None -- see TestEchoBookkeeping.setUp.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready),
            clip_state_path=self.clip_state_path,
        )

    def test_watcher_is_created_and_started_when_the_clipboard_becomes_ready(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy):
            agent.clipboard_became_ready()
        self.assertIs(agent._watcher, spy)
        # Bound methods are recreated on each attribute access, so `is` would
        # fail even when correctly wired; == compares __self__ and __func__.
        self.assertEqual(spy.started_with, agent._local_change)

    def test_watcher_is_stopped_and_cleared_when_the_clipboard_is_lost(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy):
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertTrue(spy.stopped)
        self.assertIsNone(agent._watcher)

    def test_watcher_is_not_recreated_while_already_running(self):
        agent = self.build(ready=True)
        spy = SpyWatcher()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=spy) as factory:
            agent.clipboard_became_ready()
            agent.clipboard_became_ready()
        self.assertEqual(factory.call_count, 1)
        # factory.call_count alone would not catch a bug that re-called
        # start() on the existing watcher instead of skipping it entirely.
        self.assertEqual(spy.start_count, 1)

    def test_losing_a_clipboard_that_never_became_ready_does_not_raise(self):
        agent = self.build(ready=False)
        agent.clipboard_lost()  # must be a no-op, not an AttributeError
        self.assertIsNone(agent._watcher)

    def test_a_wayland_flap_does_not_re_arm_the_detection_budget(self):
        """clipboard_lost() discards the watcher and clipboard_became_ready()
        builds a fresh one, so a dead-event-source verdict living only on the
        watcher resets on any mid-connection Wayland flap (a logout/login with
        the SSH channel still up): the switch line logs a second time and
        PC->Mac sync drops back to 30-second latency for another full detection
        cycle on an installation already diagnosed.

        This agent is one process per SSH connection (see _clip_state_sent), so
        an Agent-level flag is connection-scoped by construction -- which is
        exactly what "for the rest of the connection" means. And re-enabling
        the gnome-shell extension, the one thing that actually fixes a dead
        source, does not tear down the Wayland session, so a flap is no
        evidence whatsoever that the source recovered."""
        agent = self.build(ready=True)
        built = []

        def factory(clipboard, **kwargs):
            watcher = FlapRecordingWatcher(**kwargs)
            built.append(watcher)
            return watcher

        with mock.patch.object(clipwire_agent, "make_watcher", factory):
            agent.clipboard_became_ready()
            self.assertFalse(
                built[0].degraded, "a fresh connection starts on the detection budget"
            )
            built[0].diagnose()          # the safety net's verdict lands
            agent.clipboard_lost()
            agent.clipboard_became_ready()

        self.assertEqual(len(built), 2, "the flap must have rebuilt the watcher")
        self.assertTrue(
            built[1].degraded,
            "the watcher rebuilt after a flap must start already degraded: the "
            "diagnosis has to outlive the watcher the flap discarded",
        )
        self.assertEqual(
            built[1].started_with, agent._local_change,
            "and it must still be wired to the one observation funnel",
        )
        self.assertEqual(
            built[1].on_idle_tick, agent._reoffer_pending,
            "and to the predicate that lets a static clipboard still disarm a "
            "re-offer expectation -- a rebuild that dropped it would leave the "
            "user's next image copy to be absorbed as a re-offer that never came",
        )


class _StaticClipboard:
    """probe() is the only thing PollingWatcher's pump asks of a clipboard. A
    constant token means the poll never signals a change, so what these tests
    observe is the promotion and nothing else."""

    def probe(self):
        return "unchanged"

    def read(self):
        return None


class _FakeGPaste:
    """Stands in for GPasteWatcher: answers available() from a script, and
    records the start()/stop() the promotion is supposed to drive. Nothing
    here spawns a subprocess, so the wait window is the test's to control."""

    def __init__(self, available_at_call=1, gate=None):
        self._available_at_call = available_at_call
        self._gate = gate
        self.calls = 0
        self.started_with = None
        self.stopped = False
        self.started = threading.Event()

    def available(self):
        if self._gate is not None:
            self._gate.wait(5.0)
        self.calls += 1
        return self.calls >= self._available_at_call

    def start(self, on_change=None):
        self.started_with = on_change
        self.started.set()

    def stop(self):
        self.stopped = True


class TestUpgradingWatcher(unittest.TestCase):
    """A BOOT RACE, NOT AN ABSENT GPASTE. sshd answers while GNOME is still
    coming up, so the Mac's connect can land seconds before GPaste takes its
    bus name -- and make_watcher's verdict was final for the whole connection.
    The observed cost was not theoretical: one connection spent its life on
    the 1s fallback poll, where reads that take 0.02s through GPaste took
    1.5-3.0s and timed out, against a GPaste that had been answering for
    minutes.

    The fix cannot be a wait inside make_watcher: it is called from
    clipboard_became_ready(), on the protocol loop, and available() costs up
    to SUBPROCESS_TIMEOUT per ask. So the poll starts immediately and the
    asking happens behind it."""

    def _watcher(self, gpaste, wait_seconds=5.0, probe_interval_seconds=0.01):
        return UpgradingWatcher(
            _StaticClipboard(), 1.0, gpaste=gpaste,
            wait_seconds=wait_seconds,
            probe_interval_seconds=probe_interval_seconds)

    def test_promotes_itself_once_gpaste_answers(self):
        gpaste = _FakeGPaste(available_at_call=3)
        on_change = object()
        watcher = self._watcher(gpaste)
        self.addCleanup(watcher.stop)

        watcher.start(on_change)

        self.assertTrue(
            gpaste.started.wait(5.0),
            "GPaste answered on the third ask, so the watcher must promote "
            "itself to it rather than keep the fallback poll for the whole "
            "connection")
        self.assertIs(
            gpaste.started_with, on_change,
            "and the promoted watcher must get the same observation funnel the "
            "poll had, or changes stop reaching the agent at the moment the "
            "promotion looks like it succeeded")

    def test_the_promotion_stops_the_poll_it_replaces(self):
        """Otherwise the promotion ADDS a watcher instead of replacing one, and
        the 1s poll that caused the symptom keeps running underneath the fix."""
        gpaste = _FakeGPaste(available_at_call=1)
        watcher = self._watcher(gpaste)
        self.addCleanup(watcher.stop)

        watcher.start(object())

        self.assertTrue(gpaste.started.wait(5.0), "precondition: it promoted")
        self.assertTrue(
            watcher._stop.is_set(),
            "the fallback poll must be stopped by the promotion, or both it and "
            "GPaste observe the clipboard at once")

    def test_the_wait_is_bounded_and_the_poll_survives_it(self):
        """A machine that genuinely has no GPaste must not be left with a
        thread asking forever, and must keep the only watcher it can have."""
        gpaste = _FakeGPaste(available_at_call=10 ** 6)
        watcher = self._watcher(gpaste, wait_seconds=0.05)
        self.addCleanup(watcher.stop)

        watcher.start(object())
        watcher._promotion.join(5.0)

        self.assertFalse(
            watcher._promotion.is_alive(),
            "the wait must be bounded, not a thread that asks for the life of "
            "the connection")
        self.assertFalse(gpaste.started.is_set())
        self.assertFalse(
            watcher._stop.is_set(),
            "and the fallback poll must survive the giving-up: GPaste never "
            "came, so the poll is all this machine has")

    def test_a_stop_that_races_the_promotion_leaves_nothing_running(self):
        """stop() runs on the protocol loop when the connection drops. A
        promotion resolving just after it must not leave a GPaste watcher
        running behind the agent's back -- that one would outlive the
        connection that owns it."""
        gate = threading.Event()
        gpaste = _FakeGPaste(available_at_call=1, gate=gate)
        watcher = self._watcher(gpaste)

        watcher.start(object())
        watcher.stop()
        gate.set()
        watcher._promotion.join(5.0)

        self.assertTrue(
            gpaste.stopped or not gpaste.started.is_set(),
            "after stop() the promoted watcher must either never have started "
            "or have been stopped too")

    def test_make_watcher_builds_the_promotable_fallback(self):
        """The wiring end: the class above only matters if the production
        factory is what returns it."""
        with mock.patch.object(GPasteWatcher, "available", return_value=False):
            watcher = make_watcher(clipboard=object(), fallback_interval_seconds=2.5)

        self.assertIsInstance(
            watcher, UpgradingWatcher,
            "a GPaste that is silent at connect time is the boot race until "
            "proven otherwise, so the fallback has to keep asking")
        self.assertIsInstance(
            watcher, PollingWatcher,
            "and it must still BE the polling fallback, not a wrapper around "
            "one: every caller and test that treats it as a poller still does")
        self.assertEqual(
            watcher.interval, 2.5,
            "on the interval the caller asked for, promotion or not")

