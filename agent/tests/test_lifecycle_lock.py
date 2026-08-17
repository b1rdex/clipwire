# agent/tests/test_lifecycle_lock.py
"""Connect-under-lock and the flush report (v3.5 spec §6, §7.1-7.2): clips
coalesce silently, the store is untouched, the unlock line carries the count."""
import io
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    KIND_TEXT,
    encode_clip_payload,
)
# Only importable once agent_under_test has run -- that is what registers
# this name in sys.modules. Must follow the import above (test_lifecycle.py
# and test_lock_gate.py both order it the same way, for the same reason):
# import clipwire_agent as the FIRST line in this file works today only by
# accident of unittest discover's alphabetical load order, and breaks the
# instant this shard is run on its own.
import clipwire_agent
from test_lifecycle import FakeClipboard, _NoOpWatcher


class _GatedClipboard(FakeClipboard):
    """FakeClipboard plus the v3.5 stretch report."""
    def __init__(self, ready=False):
        super().__init__(ready=ready)
        self.stretch = None
    def lock_stretch_ended(self):
        value, self.stretch = self.stretch, None
        return value


class TestHeldClips(unittest.TestCase):
    def _agent(self, clipboard):
        # stdin/stdout as io.BytesIO(): the shape every Agent() call in
        # test_lifecycle.py uses, not the brief sketch's stdin=None /
        # stdout=mock.Mock() -- send() is overridden below in every test
        # here, so nothing ever reads either object, but there is no reason
        # to introduce a second constructor shape for that.
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path="/nonexistent/clip-state.json")
        return agent

    def test_clips_coalesce_and_the_newest_wins_with_a_count(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._on_clip(encode_clip_payload(1755500000.0, b"one"))
        agent._on_clip(encode_clip_payload(1755500001.0, b"two"))
        agent._on_clip(encode_clip_payload(1755500002.0, b"three"))
        self.assertEqual(clipboard.written, [])
        self.assertEqual(sent, [])
        clipboard.stretch = 63.0
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [(KIND_TEXT, b"three")])
        combined = [m for m in logged if "session unlocked after 63s" in m]
        self.assertEqual(len(combined), 1)
        self.assertIn("3 clips arrived while locked, applied the newest", combined[0])

    def test_zero_clips_still_reports_the_stretch(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        clipboard.stretch = 5.0
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertTrue(any("no clips arrived while locked" in m for m in logged))

    def test_counter_resets_per_stretch(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        agent._on_clip(encode_clip_payload(1755500000.0, b"one"))
        clipboard.stretch = 1.0
        # This first flush's own combined line isn't what the test below is
        # pinning (the second flush's is) -- patched anyway, or the real
        # log() sprays a genuine "1 clips arrived" line to stderr on every run.
        with mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        clipboard.stretch = 2.0
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertTrue(any("no clips arrived" in m for m in logged))

    def test_a_flush_with_no_stretch_still_spends_the_count(self):
        """The reset is unconditional, and this is the branch that proves it:
        a pre-login flush (no lock, no stretch) must SPEND the count, or the
        next genuine unlock inherits it and reports clips that arrived while
        the session was wide open. Verified by mutation: indenting the reset
        inside `if stretch is not None:` leaves the whole rest of the suite
        green and turns this assertion into "1 clips arrived while locked" --
        a fabricated incident, not a missing one."""
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        agent._on_clip(encode_clip_payload(1755500000.0, b"one"))
        with mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()          # stretch stays None: no lock here
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        clipboard.stretch = 30.0
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertTrue(any("no clips arrived while locked" in m for m in logged),
                        "a count inherited across a lock-free flush becomes a "
                        "fabricated incident: %r" % logged)

    def test_no_stretch_no_new_line(self):
        """A socket flap (pre-login path) keeps today's log exactly."""
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertFalse(any("session unlocked" in m for m in logged))


if __name__ == "__main__":
    unittest.main()
