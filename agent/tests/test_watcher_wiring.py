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


