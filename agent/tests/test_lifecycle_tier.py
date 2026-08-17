# agent/tests/test_lifecycle_tier.py
"""The v3.4 guard clauses in Agent: one on the write path, one on the read
path. Tier present and this is text -> tier; None -> today's wl-clipboard
code, unchanged. Shard convention: doubles imported from test_lifecycle."""
import io
import os
import tempfile
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    KIND_IMAGE,
    KIND_TEXT,
    TYPE_CLIP,
    encode_clip_payload,
    sha256_hex,
)
import clipwire_agent
from test_lifecycle import AsyncWriteClipboard, FakeClipboard, _NoOpWatcher


def _ready_agent(clip_state_path, clipboard=None):
    agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                  clipboard=clipboard or FakeClipboard(ready=True),
                  clip_state_path=clip_state_path)
    with mock.patch.object(clipwire_agent, "make_watcher",
                           return_value=_NoOpWatcher()):
        agent.clipboard_became_ready()
    return agent


class TestTierPickup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def test_the_agent_takes_the_watchers_tier(self):
        tier = object()
        watcher = _NoOpWatcher()
        watcher.tier = tier
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                      clipboard=FakeClipboard(ready=True),
                      clip_state_path=self.clip_state_path)
        with mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=watcher):
            agent.clipboard_became_ready()
        self.assertIs(agent._tier, tier)

    def test_a_tierless_watcher_leaves_none(self):
        agent = _ready_agent(clip_state_path=self.clip_state_path)
        self.assertIsNone(agent._tier)

    def test_the_tier_survives_clipboard_lost(self):
        """The unlock flush runs BEFORE the new watcher is built, so it can
        only ever use the previous cycle's tier -- losing it at lock would
        put every unlock flush back on wl-copy forever."""
        watcher = _NoOpWatcher()
        watcher.tier = tier = object()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                      clipboard=FakeClipboard(ready=True),
                      clip_state_path=self.clip_state_path)
        with mock.patch.object(clipwire_agent, "make_watcher",
                               return_value=watcher):
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertIs(agent._tier, tier)


class RecordingTier:
    """write_text double: scripted answer, records bodies, models the
    mismatch-top and confirmed-write side channels the real tier keeps."""
    def __init__(self, uuid="u-1", mismatch_top=None, stamp=7.0):
        self.uuid = uuid
        self.bodies = []
        self.last_mismatch_top = mismatch_top
        self.last_confirmed_write = None
        self._stamp = stamp

    def write_text(self, body):
        self.bodies.append(body)
        if self.uuid is None:
            return None
        self.last_confirmed_write = (self.uuid, self._stamp, sha256_hex(body))
        return self.uuid


def _clip(text, ts=1000.0):
    return encode_clip_payload(ts, text)


class TestWriteGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def _agent(self, tier):
        agent = _ready_agent(self.clip_state_path)
        agent._tier = tier
        return agent

    def test_a_confirmed_tier_write_skips_wl_copy_and_records(self):
        tier = RecordingTier()
        agent = self._agent(tier)
        agent._on_clip(_clip(b"hello"))
        self.assertEqual(tier.bodies, [b"hello"])
        self.assertEqual(agent.clipboard.written, [])
        self.assertEqual(agent._last_tier_write,
                         ("u-1", 7.0, sha256_hex(b"hello")))

    def test_echo_is_armed_before_the_tier_write(self):
        """The Update raised by the Add arrives DURING the blocking call --
        arming after it would read our own write back as a local change."""
        agent = _ready_agent(self.clip_state_path)
        seen = {}

        class Peeking(RecordingTier):
            def write_text(self, body, _agent=agent, _seen=seen):
                with _agent._echo_lock:
                    _seen["written"] = _agent._last_written
                    _seen["last_seen"] = _agent._last_seen
                return super().write_text(body)

        agent._tier = Peeking()
        agent._on_clip(_clip(b"hello"))
        self.assertEqual(seen["written"], b"hello")
        self.assertEqual(seen["last_seen"], (KIND_TEXT, sha256_hex(b"hello")))

    def test_a_declined_write_falls_back_to_wl_copy(self):
        tier = RecordingTier(uuid=None)
        agent = self._agent(tier)
        with mock.patch.object(clipwire_agent, "log", lambda message: None):
            agent._on_clip(_clip(b"hello"))
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"hello")])
        self.assertIsNone(agent._last_tier_write)

    def test_a_mismatch_rearms_last_seen_with_the_top_it_read(self):
        """A failed Add can raise TWO Updates (move + rollback). This pins
        the re-arm INSIDE _tier_write, before it returns -- it closes the
        gap between the failed Add's Update and _write_clip's own fallback
        arm, so a rollback Update landing in that gap reads as an echo of
        the top we just read, not a fresh local change. The fallback arm
        then clobbers _last_seen again moments later -- that second,
        single-slot arm is the spec's own (v3.4 §3.5's two arms), parked at
        the whole-branch level, not pinned by this test."""
        tier = RecordingTier(uuid=None, mismatch_top=b"the old top")
        agent = self._agent(tier)
        with mock.patch.object(clipwire_agent, "log", lambda message: None):
            agent._tier_write(KIND_TEXT, b"the new clip", sha256_hex(b"the new clip"))
            self.assertEqual(agent._last_seen, (KIND_TEXT, sha256_hex(b"the old top")))
            agent._on_clip(_clip(b"the new clip"))
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"the new clip")])

    def test_images_never_reach_the_tier(self):
        tier = RecordingTier()
        agent = self._agent(tier)
        png = b"not a real png"
        agent._on_clip(clipwire_agent.encode_image_payload(1000.0, png), KIND_IMAGE)
        self.assertEqual(tier.bodies, [])
        self.assertEqual(agent.clipboard.written, [(KIND_IMAGE, png)])

    def test_nul_and_invalid_utf8_never_reach_the_tier(self):
        """D-Bus strings must be valid UTF-8 and gpaste-client is a C string:
        both would arrive silently corrupted -- the worse failure (§4.3)."""
        for body in (b"nul\x00inside", b"\xff\xfe not utf-8"):
            with self.subTest(body=body):
                tier = RecordingTier()
                agent = self._agent(tier)
                agent._on_clip(_clip(body))
                self.assertEqual(tier.bodies, [])
                self.assertEqual(agent.clipboard.written, [(KIND_TEXT, body)])

    def test_no_tier_is_exactly_today(self):
        agent = _ready_agent(self.clip_state_path)
        agent._on_clip(_clip(b"hello"))
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"hello")])

    def test_the_fallback_logs_only_when_the_tier_tried(self):
        logged = []
        with mock.patch.object(clipwire_agent, "log", logged.append):
            agent = self._agent(RecordingTier(uuid=None))
            agent._on_clip(_clip(b"hello"))
        self.assertTrue(any("the GPaste add was not confirmed" in m for m in logged))
        logged.clear()
        with mock.patch.object(clipwire_agent, "log", logged.append):
            agent = _ready_agent(self.clip_state_path)          # no tier at all
            agent._on_clip(_clip(b"hello"))
        self.assertFalse(any("not confirmed" in m for m in logged))

    def test_a_declined_write_after_a_confirmed_one_clears_last_tier_write(self):
        """_last_tier_write means 'the LAST completed write was this
        confirmed tier write' -- a later fallback write must retire it, or
        an unlock reassert on the stale uuid would revert this newer clip
        (v3.5 §5)."""
        agent = self._agent(RecordingTier())
        agent._on_clip(_clip(b"hello"))
        self.assertIsNotNone(agent._last_tier_write)
        agent._tier = RecordingTier(uuid=None)
        with mock.patch.object(clipwire_agent, "log", lambda message: None):
            agent._on_clip(_clip(b"world"))
        self.assertIsNone(agent._last_tier_write)

    def test_an_image_write_after_a_confirmed_one_clears_last_tier_write(self):
        agent = self._agent(RecordingTier())
        agent._on_clip(_clip(b"hello"))
        self.assertIsNotNone(agent._last_tier_write)
        png = b"not a real png"
        agent._on_clip(clipwire_agent.encode_image_payload(1000.0, png), KIND_IMAGE)
        self.assertIsNone(agent._last_tier_write)


class ReadingTier:
    """current_uuid/read_text double with scripted answers. BOTH calls are
    counted: `reads` alone cannot tell a guard that ran before the uuid fetch
    from one that ran after it, and the second shape still pays the gdbus
    round trip the guard exists to skip."""
    def __init__(self, uuid="u-1", body=b"pc copy"):
        self.uuid = uuid
        self.body = body
        self.reads = []
        self.uuid_reads = 0

    def current_uuid(self):
        self.uuid_reads += 1
        return self.uuid

    def read_text(self, uuid):
        self.reads.append(uuid)
        return self.body


class UntrackedChangeWatcher(_NoOpWatcher):
    """Stands in for GPasteWatcher's one-shot: answers the read guard with
    `answer` and counts the asks. A plain class rather than a Mock on
    purpose -- a Mock would auto-create consume_untracked_change and return
    something truthy, so every watcher double in the suite would silently
    start declining the tier."""
    def __init__(self, answer):
        self.answer = answer
        self.asks = 0

    def consume_untracked_change(self):
        self.asks += 1
        return self.answer


class NeverReadClipboard(FakeClipboard):
    """A clipboard whose read() failing loudly is the assertion: the tier
    answered, so wl-paste must never run (that fork IS the blink). Reads
    are allowed and counted until a test arms forbid_reads -- setup's own
    seed read and announce_clip_state's read both fire before the guard
    under test ever runs."""
    def __init__(self, ready=False):
        super().__init__(ready=ready)
        self.reads = 0
        self.forbid_reads = False

    def read(self):
        if self.forbid_reads:
            raise AssertionError("the wl-paste path ran although the tier answered")
        self.reads += 1
        return None


class TestReadGuard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def _agent(self, tier, clipboard=None):
        agent = _ready_agent(self.clip_state_path, clipboard=clipboard)
        agent._tier = tier
        return agent

    def _sent_clips(self, agent):
        """TYPE_CLIP frames only: setup's own TYPE_CLIP_STATE announcement
        is not what these tests are asserting about."""
        data = bytearray(agent.stdout.getvalue())
        frames = []
        while True:
            frame = clipwire_agent.decode_frame(data)
            if frame is None:
                return [f for f in frames if f[0] == TYPE_CLIP]
            frames.append(frame)

    def test_a_text_top_is_sent_without_touching_wl_paste(self):
        tier = ReadingTier(body=b"copied on the PC")
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        agent.clipboard.forbid_reads = True
        agent._local_change()
        self.assertEqual(tier.reads, ["u-1"])
        frames = self._sent_clips(agent)
        self.assertTrue(any(t == TYPE_CLIP and b"copied on the PC" in p
                            for t, p in frames))

    def test_a_declining_tier_falls_back_to_the_full_read(self):
        class DecliningTier(ReadingTier):
            def read_text(self, uuid):
                self.reads.append(uuid)
                return None

        tier = DecliningTier()
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        agent.clipboard.reads = 0      # setup's own reads don't count
        agent._local_change()          # give-up path, no crash, no send
        self.assertEqual(tier.reads, ["u-1"])
        self.assertEqual(agent.clipboard.reads, 1)
        self.assertEqual(self._sent_clips(agent), [])

    def test_no_uuid_falls_back_too(self):
        tier = ReadingTier(uuid=None)
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        agent.clipboard.reads = 0
        agent._local_change()
        self.assertEqual(tier.reads, [])
        self.assertEqual(agent.clipboard.reads, 1)

    def test_no_tier_is_exactly_today(self):
        agent = _ready_agent(self.clip_state_path)  # read() -> None -> give-up path
        agent._local_change()
        self.assertEqual(self._sent_clips(agent), [])

    def test_the_tier_read_feeds_echo_suppression(self):
        """Our own tier write's Update, read back through the tier, must be
        swallowed by the one-shot exactly as a wl-paste read-back was. Armed
        NeverReadClipboard so a silent fall-through to wl-paste (which would
        also see no armed value and send nothing) can't pass for free."""
        agent = self._agent(ReadingTier(body=b"just written"),
                            clipboard=NeverReadClipboard(ready=True))
        with agent._echo_lock:
            agent._last_written = b"just written"
            agent._write_gen += 1
        agent.clipboard.forbid_reads = True
        agent._local_change()
        self.assertEqual(self._sent_clips(agent), [])

    def test_an_empty_tier_body_is_an_answer_not_a_decline(self):
        agent = self._agent(ReadingTier(body=b""),
                            clipboard=NeverReadClipboard(ready=True))
        agent.clipboard.forbid_reads = True   # b"" must NOT fall back
        agent._local_change()
        # accepted as an answer: no wl-paste fallback ran, and the give-up
        # branch sent nothing
        self.assertEqual(self._sent_clips(agent), [])

    def test_a_probe_provoked_observation_never_reaches_the_tier(self):
        """v3.3 §4.2's excluded-clip contract, which the tier read would have
        taken away: the safety net saw the selection move while GPaste's
        history uuid stood still, so GPaste recorded nothing and the top of
        its history is an OLDER clip. Serving that top is wrong content, not
        missing content. wl-paste is the only reader that can see this one."""
        tier = ReadingTier(body=b"the old top, still in GPaste's history")
        watcher = UntrackedChangeWatcher(True)
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        # SWAPPED IN AFTER setup, not passed to it: clipboard_became_ready
        # seeds _last_seen from its own read, so a clipboard already holding
        # this clip at connect would have the observation below suppressed as
        # an echo of the seed -- and the test would pass on nothing. The copy
        # has to land AFTER the connect, which is the only ordering in which
        # it is a copy at all.
        agent.clipboard = AsyncWriteClipboard(b"copied out of a password manager")
        agent._watcher = watcher

        agent._local_change()

        self.assertEqual(tier.reads, [], "the tier answered with the old top")
        self.assertEqual(tier.uuid_reads, 0,
                         "the guard ran BELOW the uuid fetch: the gdbus round "
                         "trip it exists to skip was paid anyway")
        self.assertEqual(watcher.asks, 1)
        self.assertEqual([clipwire_agent.decode_clip_payload(payload)[1]
                          for _, payload in self._sent_clips(agent)],
                         [b"copied out of a password manager"])

    def test_a_tracked_observation_still_takes_the_tier(self):
        """The one-shot answering False is exactly today: an ordinary copy
        GPaste did record must not be pushed back onto wl-paste."""
        tier = ReadingTier(body=b"copied on the PC")
        watcher = UntrackedChangeWatcher(False)
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        agent._watcher = watcher
        agent.clipboard.forbid_reads = True

        agent._local_change()

        self.assertEqual(tier.reads, ["u-1"])
        self.assertEqual(watcher.asks, 1)

    def test_a_watcher_without_the_one_shot_takes_the_tier(self):
        """getattr's default, pinned. The polling and Upgrading watchers never
        grew the method, and neither did any watcher double written before
        this cycle -- an AttributeError here would break every one of them,
        and a truthy default would decline the tier forever."""
        tier = ReadingTier(body=b"copied on the PC")
        agent = self._agent(tier, clipboard=NeverReadClipboard(ready=True))
        self.assertFalse(hasattr(agent._watcher, "consume_untracked_change"),
                         "test precondition: this watcher has no one-shot")
        agent.clipboard.forbid_reads = True

        agent._local_change()

        self.assertEqual(tier.reads, ["u-1"])


if __name__ == "__main__":
    unittest.main()
