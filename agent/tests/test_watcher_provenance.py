# agent/tests/test_watcher_provenance.py
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
)
class TestProvenanceIsConsultedAtTheReconciliation(unittest.TestCase):
    """v3.2, Task 6b: the rule is actually CALLED.

    Tasks 1-6 built resolve_provenance, put `origin` on the wire, taught
    _consume_image_reoffer to record it, made it survive the agent's death
    and disarmed the expectation that could poison it -- and nothing
    invoked any of it. A capability built and never connected is this
    project's signature planning defect, and this class is what makes the
    connection observable from the Python side alone: the pairing harness
    proves it end to end, but it is a SWIFT test, so with only that test a
    break in this file's rule and a break in the Mac's are the same red.

    THE BUG, at the size a unit test can hold it. The Mac copies a retina
    screenshot; the PC applies it, GPaste re-encodes the selection, and
    _consume_image_reoffer records the re-encode at the peer's own
    timestamp PLUS REOFFER_TS_NUDGE_SECONDS -- deliberately, so the first
    reconnect resolves deterministically instead of by hex tie-break. That
    determinism is exactly what hands the Mac back its own screenshot with
    the density gone: 1 ms is enough for the PC to win freshness forever.
    Two attempts to compare image CONTENT both failed, measured, because
    GPaste applies the embedded ICC profile and the samples genuinely move.
    The PC knows the hash it was GIVEN, so neither side deduces anything."""

    ORIGINAL = b"\x89PNG-the-mac's-own-screenshot"
    REENCODED = b"\x89PNG-what-gpaste-handed-back"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")
        self.logged = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.logged.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def build(self, clipboard):
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                      clipboard=clipboard, clip_state_path=self.clip_state_path)
        agent._clip_state_sent = True
        self.sent = []
        agent.send = lambda t, p: self.sent.append((t, p))
        return agent

    def decision(self):
        line = next(l for l in self.logged if "reconciled with the peer" in l)
        return line.split("reconciled with the peer: ")[1].split(" ")[0]

    def test_our_own_derivative_is_never_sent_back_to_its_ancestor(self):
        """THE FIX, in the direction that produced the reported defect.

        We are fresher by a millisecond and we hold different bytes, so
        resolve_freshness alone says SEND_MINE -- and the Mac applies any
        incoming clip unconditionally, so the send IS the degraded paste.
        The clipboard genuinely holds the re-encode here, and the store's
        hash genuinely matches it, so a wrongly-resolved SEND_MINE would
        reach the wire rather than dying at the send branch's own
        verification and passing this for the wrong reason."""
        save_clip_state(sha256_hex(self.REENCODED), 1000.001, KIND_IMAGE,
                        origin=sha256_hex(self.ORIGINAL), path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(self.REENCODED)
        agent = self.build(clipboard)

        agent.on_frame(TYPE_CLIP_STATE,
                       encode_clip_state(sha256_hex(self.ORIGINAL), 1000.0, KIND_IMAGE))

        self.assertEqual(self.sent, [],
                         "the peer holds the ancestor of what we hold; sending our "
                         "re-encode back is the density bug, delivered")
        self.assertEqual(self.decision(), "doNothing",
                         "provenance resolves doNothing outright -- the freshness "
                         "formula is not consulted at all")
        self.assertIn("what we hold descends from the peer's clipboard: standing down",
                      self.logged,
                      "a suppression that leaves no trace is indistinguishable from a "
                      "bug, and it fires exactly when the user expects something")

    def test_a_peer_holding_our_descendant_is_not_waited_for(self):
        """The mirror, from the side that is NOT sending anyway -- so
        "nothing was sent" cannot tell the two apart and the log is the
        whole evidence. Freshness alone reads waitForPeer here: the peer is
        a millisecond newer and we would sit waiting for a clip it has
        already decided never to send. Provenance stands BOTH sides down
        together, which is the point of one symmetric rule."""
        save_clip_state(sha256_hex(self.ORIGINAL), 1000.0, KIND_IMAGE,
                        path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(self.ORIGINAL)
        agent = self.build(clipboard)

        agent.on_frame(TYPE_CLIP_STATE,
                       encode_clip_state(sha256_hex(self.REENCODED), 1000.001, KIND_IMAGE,
                                         sha256_hex(self.ORIGINAL)))

        self.assertEqual(self.sent, [])
        self.assertEqual(self.decision(), "doNothing",
                         "waitForPeer here is waiting forever: the peer's provenance "
                         "check stood it down at the same instant")
        self.assertIn("the peer's clipboard descends from what we hold: standing down",
                      self.logged)

    def test_an_empty_peer_is_still_handed_back_what_it_lost(self):
        """The nil trap, pinned AT THE CALL SITE rather than only in the
        rule. `None == None` is True in Python, so the naive spelling of
        provenance fires on our own origin-less record against a peer that
        announced nothing -- a locked PC against an ordinary Mac, which
        happens daily -- and would kill resolve_freshness's (_, None) ->
        SEND_MINE recovery for EVERY kind of content, not merely for
        images. fixtures/provenance.json's nil rows pin the rule; this pins
        that wiring it in did not reintroduce the trap one layer up."""
        save_clip_state(sha256_hex(b"the clip the peer lost"), 777, KIND_TEXT,
                        path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"the clip the peer lost")
        agent = self.build(clipboard)

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertEqual([t for t, _ in self.sent], [TYPE_CLIP],
                         "a peer with nothing gets its clipboard back; suppressing "
                         "this is v1's silent loss, restored by a rule about images")
        self.assertEqual(self.decision(), "sendMine")


