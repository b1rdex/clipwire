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
        clipboard.stretch = (63.0, "unlocked", 100.0, 163.0)
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [(KIND_TEXT, b"three")])
        combined = [m for m in logged if "session unlocked after 1m 3s" in m]
        self.assertEqual(len(combined), 1)
        self.assertIn("3 clips arrived while locked, applied the newest", combined[0])

    def test_zero_clips_still_reports_the_stretch(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        clipboard.stretch = (5.0, "unlocked", 200.0, 205.0)
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertTrue(any("no clips arrived while locked" in m for m in logged))

    def test_a_stretch_that_falls_open_reports_its_own_pair_of_lines(self):
        """F3(b) / spec §6: 'a stretch that ends in fail-open still
        reports' -- a locked stretch the GATE ends as ended_by="fell-open"
        (the monitor losing the session, or its Get failing, mid-lock)
        must not lose the clips-held report the way a silent fail-open
        used to. Same shape as the real-unlock combined line, a different
        verb -- and it must NOT say "session unlocked", which asserts a
        fact (LockedHint=false was actually observed) this path never
        has."""
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._on_clip(encode_clip_payload(1755500000.0, b"one"))
        agent._on_clip(encode_clip_payload(1755500001.0, b"two"))
        clipboard.stretch = (7.0, "fell-open", 300.0, 307.0)
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [(KIND_TEXT, b"two")])
        combined = [m for m in logged if "lock gate fell open after 7s" in m]
        self.assertEqual(len(combined), 1)
        self.assertIn("2 clips arrived while locked, applied the newest", combined[0])
        self.assertFalse(any("session unlocked" in m for m in logged))

    def test_a_fell_open_stretch_with_no_clips_gets_the_no_clips_mirror(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        clipboard.stretch = (9.0, "fell-open", 400.0, 409.0)
        logged = []
        with mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        self.assertTrue(any(
            "lock gate fell open after 9s; no clips arrived while locked" in m
            for m in logged))

    def test_counter_resets_per_stretch(self):
        clipboard = _GatedClipboard(ready=False)
        agent = self._agent(clipboard)
        agent.send = lambda t, p: None
        agent._on_clip(encode_clip_payload(1755500000.0, b"one"))
        clipboard.stretch = (1.0, "unlocked", 500.0, 501.0)
        # This first flush's own combined line isn't what the test below is
        # pinning (the second flush's is) -- patched anyway, or the real
        # log() sprays a genuine "1 clips arrived" line to stderr on every run.
        with mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            clipboard.become_ready()
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        clipboard.stretch = (2.0, "unlocked", 600.0, 602.0)
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
        clipboard.stretch = (30.0, "unlocked", 700.0, 730.0)
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


class TestUnlockReassert(unittest.TestCase):
    """v3.5 §5: a tier write timestamped inside the locked interval just
    ended is re-Select()ed at unlock. Deviation from the brief (recorded in
    the task-10 report): the reassert fires AFTER clipboard_became_ready's
    own seed read, not before -- the seed read unconditionally overwrites
    _last_seen a few lines after where the brief placed the block, which
    would make the arm dead on arrival against every double in this file
    (FakeClipboard.read() -> None always). Placing it after is the only
    version where the arm survives to protect a later _observe_local_change
    -- verified empirically before implementing (see the report)."""

    def _agent_with_stretch(self, stretch, record):
        """The file's usual ready agent (TestHeldClips._agent's shape): a
        _GatedClipboard armed to report `stretch` once clipboard_became_ready
        asks, carrying `record` as though it were the tier's last confirmed
        write. Construction only -- the caller drives clipboard_became_ready()
        itself, inside whatever gpaste_select/log/make_watcher patches that
        call needs active."""
        clipboard = _GatedClipboard(ready=False)
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path="/nonexistent/clip-state.json")
        clipboard.stretch = stretch
        agent._last_tier_write = record
        clipboard.become_ready()
        return agent

    def test_a_write_inside_the_interval_is_reselected(self):
        calls = []
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0),
                                             ("u-9", 130.0, "ab" * 32))
            agent.clipboard_became_ready()
        self.assertEqual(calls, ["u-9"])
        self.assertIsNone(agent._last_tier_write)          # consumed
        with agent._echo_lock:
            self.assertEqual(agent._last_seen, (KIND_TEXT, "ab" * 32))

    def test_a_write_exactly_at_the_lock_edge_is_reselected(self):
        """The lower bound is inclusive (`lock_edge <= record[1]`), not
        `<` -- a write completing in the same instant the lock closed still
        raced it. Added after Step 5's own break-verification found that
        this suite's other fixture (stamp 90.0 against [100.0, 160.0]) sits
        too far from either edge for a `<=` -> `<` swap to move (see the
        task-10 report); this one sits exactly on it."""
        calls = []
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0),
                                             ("u-edge", 100.0, "12" * 32))
            agent.clipboard_became_ready()
        self.assertEqual(calls, ["u-edge"])

    def test_the_reassert_logs_uuid_and_why(self):
        logged = []
        with mock.patch.object(clipwire_agent, "gpaste_select", return_value=True), \
             mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0),
                                             ("u-9", 130.0, "ab" * 32))
            agent.clipboard_became_ready()
        self.assertIn(
            "re-selected u-9: tier write completed inside a locked interval",
            logged)

    def test_a_write_before_the_lock_is_left_alone(self):
        """record stamp 90.0, interval (100.0, 160.0) -> gpaste_select never
        called, record stays (it can never match a later interval, but
        consuming only on a fire keeps the condition the spec's own
        sentence)."""
        calls = []
        record = ("u-old", 90.0, "cd" * 32)
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0), record)
            agent.clipboard_became_ready()
        self.assertEqual(calls, [])
        self.assertEqual(agent._last_tier_write, record)

    def test_no_record_no_reassert_no_crash(self):
        """_last_tier_write None -> nothing: also pins the `record is not
        None and ...` short-circuit -- swap the operand order and this
        indexes None[1]."""
        calls = []
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((1.0, "unlocked", 500.0, 501.0), None)
            agent.clipboard_became_ready()   # must not raise
        self.assertEqual(calls, [])
        self.assertIsNone(agent._last_tier_write)

    def test_a_failed_select_stays_silent_and_arms_nothing(self):
        """gpaste_select -> False (v3.5 §5.3's measured shape: a dead/stale
        uuid fails rc=1): no log line, _last_seen untouched beyond the seed
        read, and _last_tier_write is STILL consumed -- a dead uuid is not
        retried on the next unlock."""
        logged = []
        with mock.patch.object(clipwire_agent, "gpaste_select", return_value=False), \
             mock.patch("clipwire_agent.log", side_effect=lambda m: logged.append(m)), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0),
                                             ("u-9", 130.0, "ab" * 32))
            agent.clipboard_became_ready()
        self.assertFalse(any("re-selected" in m for m in logged))
        self.assertIsNone(agent._last_tier_write)
        with agent._echo_lock:
            self.assertIsNone(agent._last_seen)     # the seed, FakeClipboard reads None

    def test_a_fell_open_stretch_reasserts_too(self):
        """ended_by "fell-open": the interval still ended; Select is
        idempotent (the reassert never inspects ended_by)."""
        calls = []
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((7.0, "fell-open", 300.0, 307.0),
                                             ("u-fell", 303.0, "ef" * 32))
            agent.clipboard_became_ready()
        self.assertEqual(calls, ["u-fell"])

    def test_the_reassert_runs_before_the_pending_flush(self):
        """Ordering, not just presence: the reassert must run BEFORE
        pending_clip's own flush. Sharp discriminator -- this double has no
        tier, so the flush's _write_clip takes the fallback path, which
        unconditionally clears _last_tier_write (Task 3's rule); a
        below-the-flush reassert would find the record already gone.
        Both effects must show: the reassert fired, and the newer clip
        still landed after it."""
        calls = []
        with mock.patch.object(clipwire_agent, "gpaste_select",
                               side_effect=lambda uuid: calls.append(uuid) or True), \
             mock.patch("clipwire_agent.log"), \
             mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=_NoOpWatcher()):
            agent = self._agent_with_stretch((60.0, "unlocked", 100.0, 160.0),
                                             ("u-9", 130.0, "ab" * 32))
            agent._on_clip(encode_clip_payload(1755500000.0, b"newer"))  # not ready -> pending_clip
            agent.clipboard_became_ready()
        self.assertEqual(calls, ["u-9"])
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"newer")])


class TestDescribeDuration(unittest.TestCase):
    def test_the_incident_reads_as_hours_not_raw_seconds(self):
        """17.5h logged as '63000s' fails §6's 'readable from this log
        alone' on the exact incident the gate exists for."""
        self.assertEqual(clipwire_agent._describe_duration(63000), "17h 30m")

    def test_small_and_boundary_values(self):
        for seconds, expect in ((0.4, "0s"), (5.0, "5s"), (59.9, "59s"),
                                (60.0, "1m"), (63.0, "1m 3s"), (3600.0, "1h"),
                                (3661.0, "1h 1m 1s")):
            with self.subTest(seconds=seconds):
                self.assertEqual(clipwire_agent._describe_duration(seconds), expect)


if __name__ == "__main__":
    unittest.main()
