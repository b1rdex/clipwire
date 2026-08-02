# agent/tests/test_watcher_clip_state.py
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
    HASH_A,
    HASH_B,
    QueueClipboard,
    SpyWatcher,
)
class TestIncomingClipState(unittest.TestCase):
    """Agent._on_clip_state: resolves an incoming TYPE_CLIP_STATE frame
    against what we hold, per resolve_freshness, and sends only when we
    win. Mirrors HandleFrameTests.swift's "Contract 5" section
    (resolving a peer's clip-state announcement).

    Fix round 1, Finding 1: _on_clip_state only resolves a peer's
    announcement immediately when _clip_state_sent is already True (our own
    side has reconciled and announced at least once this connection) --
    otherwise it stashes the peer state for clipboard_became_ready to
    resolve once that reconciliation has happened, since the store can
    still be stale before it. build() below defaults to simulating that
    precondition directly (already_reconciled=True) so the tests in this
    class, which are about resolve_freshness's OUTCOMES once resolution
    actually happens, are not all forced to drive a full
    clipboard_became_ready() just to reach that state. The stash itself,
    and clipboard_became_ready resolving it, are pinned separately below
    (test_a_clip_state_arriving_before_our_own_reconciliation_is_stashed_not_resolved_immediately
    and
    test_clipboard_became_ready_resolves_a_stashed_peer_clip_state_after_reconciling);
    the full real-dispatch-ordering reproduction lives in
    test_mainloop.py::TestClipStateOrderingAcrossRealDispatch, since only
    driving the REAL run() loop can prove the ordering bug this precondition
    exists to close (calling handlers by hand in a chosen order is exactly
    what let it through undetected the first time)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, clipboard=None, already_reconciled=True):
        agent = Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(),
            clipboard=clipboard if clipboard is not None else QueueClipboard(ready=True),
            clip_state_path=self.clip_state_path,
        )
        agent._clip_state_sent = already_reconciled
        return agent

    def capture_log(self):
        """Collects clipwire_agent.log's lines for the duration of one test.
        Several tests below assert on the send branch's own log line, and
        the module-level patch plus its cleanup is the same four lines every
        time."""
        original_log = clipwire_agent.log
        lines = []
        clipwire_agent.log = lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)
        return lines

    def test_losing_clip_state_with_peer_fresher_produces_no_send(self):
        """resolve_freshness's waitForPeer outcome: the peer is fresher, so
        we wait. Conflating this with doNothing would be harmless here, but
        the point of a resend would be to CLOBBER a fresher peer -- exactly
        the defect this whole design exists to prevent."""
        # Real content on the clipboard, and a stored hash that actually
        # MATCHES it: a wrongly-resolved SEND_MINE would otherwise stop at
        # one of that branch's own guards -- since Task 11 the first of them
        # is the verification, which a placeholder hash fails -- and this
        # assertion would hold for the wrong reason. Mirrors
        # HandleFrameTests.testLosingClipStateWithPeerFresherProducesNoSend.
        save_clip_state(sha256_hex(b"something to wrongly send"), 5, KIND_TEXT,
                        path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"something to wrongly send")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 9, KIND_TEXT))  # peer fresher

        self.assertEqual(sent, [], "the peer is fresher -- we wait, we do not resend")

    def test_agreeing_clip_state_with_equal_hashes_produces_no_send(self):
        """resolve_freshness's doNothing outcome via equal hashes: "hashes
        equal" must mean "we agree", not "resend" -- conflating it with
        sendMine would ping-pong the same content back and forth forever."""
        # See the test above: without real content the clipboard actually
        # holds -- and a stored hash that matches it -- a wrongly-resolved
        # SEND_MINE stops at one of that branch's own guards and this would
        # pass regardless. The hash goes on BOTH sides here, since equal
        # hashes are what the doNothing outcome under test turns on.
        agreed = sha256_hex(b"something to wrongly send")
        save_clip_state(agreed, 5, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"something to wrongly send")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(agreed, 999, KIND_TEXT))  # same hash

        self.assertEqual(sent, [], "hashes equal means we agree, not resend")

    def test_winning_clip_state_sends_exactly_one_clip_frame_carrying_our_stored_timestamp(self):
        """resolve_freshness's sendMine outcome: a peer with no clipboard at
        all (also the fix for v1's documented loss of Mac copies made while
        the PC was off). The resulting clip must carry OUR stored ts, not
        now -- resending with now would perpetually refresh its age and let
        it win every future reconciliation regardless of what happens next.

        The stored hash is the real digest of what the clipboard double
        returns: since Task 11 the branch verifies the two against each
        other before sending, so a placeholder hash here would make this
        test prove only that the verification works."""
        save_clip_state(sha256_hex(b"current clip text"), 777, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"current clip text")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))  # peer empty

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], TYPE_CLIP)
        ts, text = decode_clip_payload(sent[0][1])
        self.assertEqual(ts, 777, "must carry OUR stored ts, not now")
        self.assertEqual(text, b"current clip text")

    def test_winning_clip_state_updates_last_seen_to_what_was_just_sent(self):
        """_local_change sets _last_seen = text after a successful send,
        deliberately: _last_seen is "what the peer already holds, for as
        long as neither side has genuinely changed it" (see its own doc
        comment in _local_change). Telling the peer "I hold X" through THIS
        path is no different -- after this send, the peer does (soon) hold
        X too, exactly as after a _local_change send. Without this update, a
        later spurious GPaste signal (a history deletion emits Update too,
        not only a real change) would see clipboard.read() == X but
        last_seen still stale or None, wrongly conclude a genuine local
        change happened, and resend X to the peer -- wastefully at best,
        and destructively if the Mac had meanwhile been changed to some Y:
        the Mac's handleFrame applies ANY incoming .clip frame
        unconditionally, so a stale resend of X arriving after the user's
        own Y would silently clobber it.

        This is the Swift reference's own .clipState case NOT doing this
        either -- but harmlessly there, since the Mac's PasteboardWatcher is
        changeCount-driven and never fires on a non-change. The PC's GPaste
        watcher does fire on non-changes (that is the entire reason
        _last_seen exists on this side at all), so the omission that is
        inert on the Mac is a real defect here."""
        save_clip_state(sha256_hex(b"current clip text"), 777, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"current clip text")
        agent = self.build(clipboard=clipboard)
        agent.send = lambda t, p: None

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))  # peer empty -> sendMine

        self.assertEqual(
            agent._last_seen, (KIND_TEXT, sha256_hex(b"current clip text")),
            "_last_seen must advance to what was just sent, exactly as "
            "_local_change's own send path already does",
        )

    def test_winning_clip_state_stays_silent_when_a_racy_reread_disagrees_with_the_applied_clip(self):
        """The same class of bug clipboard_became_ready's announce step was
        fixed for, one door over: a TYPE_CLIP frame applied via _write_clip
        (the immediate _on_clip path, once READY) followed closely by a
        TYPE_CLIP_STATE frame that wins the reconciliation -- both
        plausible on the same connection, e.g. the Mac's watcher pushing a
        fresh local change around the time of its own one-time clip-state
        announcement. _write_clip already wrote to and correctly persisted
        state for that applied clip; if _on_clip_state re-reads the
        clipboard to find the content to send, that read can race wl-copy's
        asynchronous, detached write (see _write_clip's own comment) and
        see stale content -- sending WRONG text stamped with the CORRECT
        mine[1] timestamp, which looks like a valid, fresh reconciliation
        response to the peer, and which the peer cannot tell from a real
        one.

        The defect this pins is unchanged; the remedy is Task 11's. The
        branch no longer trusts remembered bytes (_last_seen_text, deleted
        with this task -- see _resolve_clip_state's own comment on why the
        two are mutually exclusive): it reads, hashes, compares against
        mine's own (kind, hash), and on a disagreement sends NOTHING and
        logs. Nothing wrong reaches the peer either way. What is lost, and
        accepted deliberately, is the correct send this scenario used to
        produce: the applied clip is not re-offered from anywhere else
        afterwards, since the watcher's own eventual observation of our
        write is (correctly) suppressed as an echo. See
        _resolve_clip_state's comment for why that exposure is narrower
        than sending unverified bytes.

        This is also the one test in the file where _last_seen agrees with
        mine on both kind and hash at the moment the branch runs, which is
        the COMMON case for a send resolution (a just-applied clip, or a
        just-sent local change). Restoring a "trust _last_seen_text and skip
        the read" fast path would send `applied_text` here and turn this
        red -- which is exactly what it is for.

        QueueClipboard is the right double here, unmodified: its write()
        already never affects what a subsequently-queued read() returns --
        precisely the "write and read are decoupled in time" shape of the
        real asynchronous wl-copy, achieved here simply by not queuing the
        applied text as a read value.

        already_reconciled=False, unlike every other test in this class:
        this is the one test that drives the REAL clipboard_became_ready()
        transition (not a simulated shortcut) before the clip-state frame
        arrives -- it is the "clip-state after readiness" half of the
        ordering space, deliberately kept distinct from
        TestClipStateOrderingAcrossRealDispatch's "clip-state BEFORE
        readiness" reproduction in test_mainloop.py."""
        applied_text = b"the peer's own recently applied clip"
        applied_ts = 555.0
        clipboard = QueueClipboard(ready=True)
        agent = self.build(clipboard=clipboard, already_reconciled=False)

        # Get into READY phase first (an empty queue -> the connect-time
        # seed and the initial announce both read None, which is fine and
        # irrelevant to what this test actually checks).
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        agent.on_frame(TYPE_CLIP, encode_clip_payload(applied_ts, applied_text))
        self.assertEqual(
            load_clip_state(path=self.clip_state_path), (sha256_hex(applied_text), applied_ts, KIND_TEXT, None),
            "test setup must actually apply and persist the clip, or this test proves nothing",
        )
        self.assertEqual(
            agent._last_seen, (KIND_TEXT, sha256_hex(applied_text)),
            "the applied clip must really be what this side remembers holding, or the "
            "fast path this test exists to keep deleted was never reachable here",
        )

        # Queued for the send branch's OWN read -- stale content that
        # predates the clip just applied above, modeling wl-copy not yet
        # having taken over.
        clipboard.queue_read(b"stale content predating this connection")

        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))  # peer empty -> sendMine

        self.assertEqual(
            sent, [],
            "a read that disagrees with what we announced must send nothing -- neither the "
            "stale bytes it returned nor the applied clip it contradicts",
        )
        self.assertIn("clipboard changed before the send", "\n".join(log_lines))

    # MARK: - Task 11: the send branch verifies before it sends

    def test_the_send_branch_stays_silent_when_the_clipboard_moved_on(self):
        """mine says text with hash A; by the time we send, the clipboard
        holds an image. Sending it under A's timestamp is a clobber the peer
        cannot detect: a well-formed text frame carrying a mojibake
        transliteration of a PNG, at an age that was never that content's.

        Both halves of the verification are wrong here at once (the kind and
        the hash), which is the honest shape of the race: whatever replaced
        the announced content is not required to be of the same kind."""
        save_clip_state(HASH_A, 5000.0, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(b"\x89PNG-something-else")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        # Older than ours, so we resolve SEND_MINE.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

        self.assertEqual(sent, [], "the clipboard no longer holds what we announced")
        self.assertIn("clipboard changed before the send", "\n".join(log_lines))

    def test_the_send_branch_stays_silent_when_only_the_hash_moved_on(self):
        """The kind still matches and only the content changed -- the
        ordinary shape of the race, a second text copy landing between the
        announcement and this frame. A verification that compared only the
        kind would pass this and send the wrong text under the announced
        timestamp."""
        save_clip_state(HASH_A, 5000.0, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"whatever the user copied since")
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

        self.assertEqual(sent, [], "the announced hash is not what the clipboard offers")
        self.assertIn("clipboard changed before the send", "\n".join(log_lines))

    def test_the_send_branch_sends_when_the_clipboard_still_matches(self):
        """The positive half: without it the two tests above pass against a
        branch that never sends anything at all."""
        body = b"still here"
        save_clip_state(sha256_hex(body), 5000.0, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(body)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], TYPE_CLIP)
        self.assertEqual(decode_clip_payload(sent[0][1]), (5000.0, body))
        self.assertNotIn("clipboard changed before the send", "\n".join(log_lines))

    def test_the_send_branch_stays_silent_when_the_clipboard_emptied(self):
        """A clipboard that now reads back as nothing at all is the same
        class of failure as one holding different content: it does not hold
        what we announced. read() returning None is the shape a Wayland
        session with no selection owner takes -- and the shape a transient
        wl-paste timeout takes too, which is why staying quiet (rather than
        sending the announced hash's presumed bytes) is the only safe
        reading of it."""
        save_clip_state(HASH_A, 5000.0, KIND_TEXT, path=self.clip_state_path)
        agent = self.build(clipboard=QueueClipboard(ready=True))  # empty queue -> read() is None
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

        self.assertEqual(sent, [])
        self.assertIn("clipboard changed before the send", "\n".join(log_lines))

    def test_the_send_branch_sends_a_verified_image_as_an_image_clip(self):
        """The branch a reconnect takes when the PC's image is the fresher of
        the two states. It used to fall out silently here: the only frame it
        could build was TYPE_CLIP, the TEXT codec, and putting PNG bytes
        through that is worse than sending nothing. Task 12 fix round 1
        gives it the image codec instead, so an image that wins a
        reconciliation actually reaches the peer.

        Left unclosed, the asymmetry ships: Task 13 gives the MAC this same
        send, and a PC that verifies its image and then says nothing means
        every reconnect where the PC's screenshot is the newer one silently
        keeps it on the PC -- with no log line saying why, since the silence
        was deliberate.

        The frame carries mine[1] -- the ANNOUNCED timestamp -- not a fresh
        reading. The content did not change, it was only re-announced, and
        stamping it with now would refresh its age on every reconnect and let
        it win every future reconciliation regardless of what happens next.
        That is the property the verification exists to make safe, and it is
        pinned here rather than left to the text path alone."""
        png = b"\x89PNG\r\n\x1a\n" + b"pixels"
        save_clip_state(sha256_hex(png), 5000.0, KIND_IMAGE, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(png)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

        self.assertEqual([frame_type for frame_type, _ in sent], [TYPE_IMAGE_CLIP],
                         "an image must go out through the image codec, not the text one")
        self.assertEqual(decode_image_payload(sent[0][1]), (5000.0, png),
                         "the announced timestamp, and the bytes the clipboard verified")
        self.assertNotIn(
            "clipboard changed before the send", "\n".join(log_lines),
            "the clipboard holds exactly what we announced, so this send is the "
            "verification passing -- not it being skipped",
        )
        self.assertEqual(
            agent._last_seen, (KIND_IMAGE, sha256_hex(png)),
            "the peer holds it now, so a later spurious GPaste Update must not read "
            "it as a fresh local change and send it again",
        )

    def test_a_verified_but_empty_body_is_not_sent_under_either_kind(self):
        """The guard that used to ride along on the text path's
        `kind != KIND_TEXT or not text` and now stands on its own, because
        the branch below it can build two different frames.

        Constructible only by writing the empty string's digest into the
        store directly -- resolve_current_clip_state records a None hash for
        an empty clipboard, so nothing in the agent produces this state. But
        the store is a file that outlives the process, and an empty body
        reaching encode_image_payload would put a frame on the wire that
        decode_image_payload refuses at the far end ("image payload carries
        no image"): a send that cannot succeed, from a branch whose whole
        purpose is that it verified first.

        Found by mutation: deleting the guard failed no test."""
        for kind, queue in ((KIND_TEXT, "queue_read"), (KIND_IMAGE, "queue_image_read")):
            with self.subTest(kind=kind):
                path = os.path.join(self._tmp.name, "empty-%s.json" % kind)
                save_clip_state(sha256_hex(b""), 5000.0, kind, path=path)
                clipboard = QueueClipboard(ready=True)
                getattr(clipboard, queue)(b"")
                agent = Agent(
                    stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                    clip_state_path=path,
                )
                agent._clip_state_sent = True
                sent = []
                agent.send = lambda t, p: sent.append((t, p))
                log_lines = self.capture_log()

                agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1000.0, KIND_TEXT))

                self.assertEqual(sent, [], "an empty body is not a clip of any kind")
                self.assertNotIn(
                    "clipboard changed before the send", "\n".join(log_lines),
                    "the verification PASSED here -- the clipboard really does hold "
                    "the (empty) thing we announced -- so this silence must be the "
                    "empty-body guard's, not the verification's",
                )

    def test_winning_clip_state_with_an_oversized_image_is_logged_with_its_size(self):
        """The image twin of the text cap below, and the reason the two are
        separate constants: this body is over MAX_IMAGE_BYTES, not over the
        (larger) MAX_PAYLOAD_BYTES frame cap, so it is refused as a matter of
        the policy images are held to rather than wire safety.

        An image can only get onto the store at this size through the
        re-offer path, which deliberately records an oversized read-back
        rather than dropping it (GPaste's re-encode INFLATES -- 105 KB in,
        180 KB out) -- so this is reachable in production, not a synthetic
        case. The size is logged because a user whose screenshot wins a
        reconciliation and still does not arrive has nothing else to look
        at."""
        oversized = b"\x89" * (MAX_IMAGE_BYTES + 1)
        save_clip_state(sha256_hex(oversized), 777.0, KIND_IMAGE, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(oversized)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertEqual(sent, [])
        self.assertIn(str(len(oversized)), "\n".join(log_lines))
        self.assertIn(
            "over the image limit", "\n".join(log_lines),
            "one verdict clause for one limit: the same one the re-offer path and "
            "the local-observation path already use, so three sites cannot drift",
        )

    def test_winning_clip_state_with_an_image_at_exactly_the_limit_still_sends(self):
        """The boundary the three separated caps exist for. This body plus
        its 8-byte timestamp exceeds the OLD single 4 MiB cap, so a guard
        written as `len(body) + TIMESTAMP_BYTES > MAX_IMAGE_BYTES` -- correct
        for TEXT one branch down -- would refuse a legal maximum-size image
        here. MAX_IMAGE_BYTES bounds the image; MAX_PAYLOAD_BYTES bounds the
        frame, with room for the prefix by construction."""
        exact = b"\x89" * MAX_IMAGE_BYTES
        save_clip_state(sha256_hex(exact), 777.0, KIND_IMAGE, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(exact)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertEqual([frame_type for frame_type, _ in sent], [TYPE_IMAGE_CLIP])
        self.assertEqual(len(sent[0][1]), MAX_IMAGE_BYTES + TIMESTAMP_BYTES)

    def test_winning_clip_state_with_content_at_exactly_the_cap_produces_no_send(self):
        """Unlike _local_change's own send path, this branch reads the live
        clipboard independently and, without this bound, winning a
        reconciliation over content at or beyond the TEXT limit would build
        a payload that exceeds MAX_TEXT_BYTES once wrapped in its 8-byte
        timestamp -- refused here on content-limit grounds, independently of
        whether the resulting frame would also exceed the (larger)
        MAX_PAYLOAD_BYTES wire cap.

        The stored hash is the real digest of the oversized content, not a
        placeholder: since Task 11 the branch verifies before it sends, and
        a placeholder would make this test pass on the verification's
        silence while the cap it exists for went untested."""
        save_clip_state(sha256_hex(b"x" * MAX_TEXT_BYTES), 777, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"x" * MAX_TEXT_BYTES)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertEqual(sent, [], "content of exactly the cap would encode to a payload 8 bytes over it")

    def test_winning_clip_state_with_content_leaving_exact_room_for_the_timestamp_prefix_still_sends(self):
        text = b"x" * (MAX_TEXT_BYTES - TIMESTAMP_BYTES)
        save_clip_state(sha256_hex(text), 777, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(text)
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertEqual(len(sent), 1)
        self.assertEqual(decode_clip_payload(sent[0][1])[1], text)

    def test_winning_clip_state_with_oversized_content_is_logged_with_its_size(self):
        """A user whose large paste wins a reconciliation but can't actually
        be sent has nothing to look at otherwise -- matches the existing
        "skipping a clip of N bytes: over the text limit" line used for
        _local_change's own cap."""
        oversized = MAX_TEXT_BYTES
        save_clip_state(sha256_hex(b"x" * oversized), 777, KIND_TEXT, path=self.clip_state_path)
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"x" * oversized)
        agent = self.build(clipboard=clipboard)
        agent.send = lambda t, p: None

        log_lines = self.capture_log()

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))

        self.assertTrue(
            any("skipping a clip of %d bytes" % oversized in line for line in log_lines),
            "expected the skip to be logged with its size; got: %r" % log_lines,
        )

    def test_clip_state_fallback_when_store_is_empty_still_resolves_from_the_live_clipboard(self):
        """If the store's own load returns nothing (a disk failure on an
        earlier save, never expected in ordinary operation), _on_clip_state
        must still resolve a real state from the live clipboard rather than
        a bare None-hash placeholder. A bare None there would make BOTH
        sides resolve waitForPeer against each other's (correctly
        announced) state and silently lose the clip -- exactly v1's bug,
        reintroduced through the fallback path instead of the main one."""
        # self.clip_state_path is never written to -- load_clip_state() returns None.
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"what we actually hold")  # the fallback resolution's own read
        clipboard.queue_read(b"what we actually hold")  # the sendMine branch's own read
        agent = self.build(clipboard=clipboard)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(None, 0, None))  # peer also empty

        self.assertEqual(len(sent), 1,
                         "an empty store must not silently resolve to waitForPeer against a "
                         "peer that also holds nothing to compare against -- that is a silent loss")
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"what we actually hold")

    def test_a_malformed_clip_state_raises_a_clip_state_error_not_a_crash(self):
        """The agent-level twin of test_freshness.py's
        test_decode_rejects_an_oversized_integer_timestamp: _on_clip_state
        must not locally swallow a decode failure. It mirrors _on_hello's
        existing bare `raise FrameError(...)` for a malformed/mismatched
        hello -- so a malformed clip-state closes the connection via
        main()'s `except FrameError`, exactly like a malformed hello does,
        rather than silently continuing (Swift's peer cannot self-close
        and so swallows this; this agent, as the child process sshd spawns,
        can and already does for hello) or crashing with an uncaught
        OverflowError."""
        agent = self.build()
        oversized = ('{"sha256": "%s", "ts": 1' % HASH_A).encode() + b"0" * 400 + b"}"

        with self.assertRaises(ClipStateError):
            agent.on_frame(TYPE_CLIP_STATE, oversized)

    # MARK: - Fix round 1, Finding 1: stash-before-reconciliation
    #
    # Fast, direct pins of the stash mechanism itself, complementing
    # test_mainloop.py::TestClipStateOrderingAcrossRealDispatch's slower but
    # higher-fidelity end-to-end reproduction (which drives the REAL run()
    # loop, per Finding 3 -- calling handlers by hand in a chosen order,
    # as these two tests do, is exactly the blind spot that let the bug
    # through the first time, so it cannot be the ONLY coverage).

    def test_a_clip_state_arriving_before_our_own_reconciliation_is_stashed_not_resolved_immediately(self):
        """not self._clip_state_sent -- our own side has not yet reconciled
        and announced this connection -- must stash the peer's state rather
        than resolve it against a store that can still be stale (content
        predating this process, the ordinary case: ANY reconnect, not only
        a reboot). Resolving here anyway is exactly how a peer's
        announcement that happens to still match the stale value would be
        judged doNothing and never reconsidered -- v1's silent loss.

        Our own local store's content ("aa", 5) is deliberately irrelevant
        here and never even loaded: this pins that the STASHED value is the
        peer's own decoded announcement ("bb", 42) -- not our local state,
        and not silently dropped -- which is a distinct assertion from
        "nothing was sent"."""
        save_clip_state(HASH_A, 5, KIND_TEXT, path=self.clip_state_path)
        agent = self.build(already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 42, KIND_TEXT))

        self.assertEqual(sent, [], "must not resolve before our own side has reconciled")
        self.assertEqual(
            agent._pending_peer_clip_state, (HASH_B, 42.0, KIND_TEXT, None),
            "the peer's own decoded state must be stashed, not silently dropped",
        )

    def test_clipboard_became_ready_resolves_a_stashed_peer_clip_state_after_reconciling(self):
        """The other half: once clipboard_became_ready has reconciled (and
        announced) our own side, a clip-state stashed before that point
        must actually be resolved -- against the NOW-current store, not a
        stale one. This is the direct-call twin of
        TestClipStateOrderingAcrossRealDispatch's full run()-loop
        reproduction in test_mainloop.py; both exist because neither alone
        is enough evidence (see this class's own docstring)."""
        clipboard = QueueClipboard(ready=True)
        # Two reads happen inside clipboard_became_ready(): the connect-time
        # seed, and a second read from announce_clip_state's own
        # resolve_current_clip_state call -- see
        # TestEchoBookkeeping.test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send
        # for the same double-read shape.
        clipboard.queue_read(b"B")  # the seed read
        clipboard.queue_read(b"B")  # the announce step's own read
        # And the send branch's own read, which since Task 11 verifies what
        # the clipboard actually holds against what was just announced
        # before it sends anything.
        clipboard.queue_read(b"B")
        agent = self.build(clipboard=clipboard, already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # Stale on disk: both sides last synced on "A" a long time ago.
        save_clip_state(sha256_hex(b"A"), 100.0, KIND_TEXT, path=self.clip_state_path)
        # The peer's own announcement, also still describing "A" -- it
        # has not changed either.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(sha256_hex(b"A"), 100.0, KIND_TEXT))
        self.assertEqual(sent, [], "must not resolve yet -- stashed")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        self.assertIsNone(
            agent._pending_peer_clip_state, "the stash must be cleared once resolved"
        )
        clip_frames = [f for f in sent if f[0] == TYPE_CLIP]
        self.assertEqual(
            len(clip_frames), 1,
            "the reconciled store now says B, fresher than the peer's stale "
            "A announcement -- B must be sent, not silently dropped",
        )
        self.assertEqual(decode_clip_payload(clip_frames[0][1])[1], b"B")

    def test_a_stashed_clip_state_resolves_against_the_pair_just_computed(self):
        """_resolve_clip_state re-loaded the store even when reached from
        clipboard_became_ready, which had JUST computed the authoritative
        pair one line earlier and announced it to this very peer.

        That only diverges when the store cannot be read back -- every
        save_clip_state call site swallows its failure, so an unwritable
        state directory is silent and this is the shape it takes. The
        fallback then re-derives from a FRESH clipboard read and stamps
        time.time(), so the value we reconcile with is not the value we
        just announced to the peer: an age we invented, inflated past the
        one on the wire, able to win a comparison it should have lost.

        Pinned by the TIMESTAMP the clip we send carries: it must be the
        exact one we announced, not one a re-derivation would stamp from a
        later clock reading. The clipboard's own content is deliberately
        the same "A" on every read, so the two implementations differ ONLY
        in the timestamp -- which is the whole disagreement.

        A read count is deliberately NOT asserted, and the difference
        matters. Since Task 11 the send branch reads once of its own,
        verifying that the clipboard still holds what we announced before
        sending it -- a verification AGAINST the pair, not a re-derivation
        OF it. Counting reads cannot tell those two apart, so an assertion
        on the count would read as "the pair must never be re-derived" and
        push a later reader straight back into skipping the verification."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable = os.path.join(blocker, "clip-state.json")
        with self.assertRaises(OSError):
            save_clip_state(HASH_A, 1, KIND_TEXT, path=unsaveable)

        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"A")  # the connect-time seed
        clipboard.queue_read(b"A")  # announce_clip_state's own read
        clipboard.queue_read(b"A")  # the send branch's own verification read
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(),
                      clipboard=clipboard, clip_state_path=unsaveable)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # The peer holds something else and is OLDER, so we win and send.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(HASH_B, 1.0, KIND_TEXT))
        self.assertEqual(sent, [], "must not resolve before our own side has reconciled")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        announced = [f for f in sent if f[0] == TYPE_CLIP_STATE]
        clips = [f for f in sent if f[0] == TYPE_CLIP]
        self.assertEqual(len(announced), 1)
        self.assertEqual(len(clips), 1, "we are fresher than the peer, so we send")
        self.assertEqual(decode_clip_payload(clips[0][1])[1], b"A")
        self.assertEqual(
            decode_clip_payload(clips[0][1])[0], decode_clip_state(announced[0][1])[1],
            "the clip we send must carry the timestamp we just announced to this "
            "same peer, not one re-derived from a later clock reading",
        )

    def test_every_reconciliation_outcome_is_logged(self):
        """No reconciliation decision was logged at all, on either side.
        Acceptance item 2 -- "the PC's copy must win, and the conflict must
        appear in the log" -- is unpassable without this, and the design's
        one accepted trade-off ("the side whose agent was born more recently
        wins") is justified on the grounds of being visible in the log
        rather than mysterious, which was never implemented.

        The decision word itself is the shared vocabulary: SEND_MINE /
        WAIT_FOR_PEER / DO_NOTHING are the exact strings Swift's
        FreshnessDecision raw values use, so the two sides' lines are
        byte-identical for free -- the same convention the frame-cap and
        skew lines already follow. Both sides' lines land in the SAME file
        in production: Channel.attempt pipes this agent's stderr into the
        Mac's log with a `remote: ` prefix.

        The line also carries a `(mine=... peer=...)` kind suffix since
        Task 14 -- checked with `in` below rather than `==` for exactly that
        reason, so this test does not have to know its shape. See
        test_the_reconciliation_line_names_both_kinds for the suffix
        itself."""
        # The stored hash is the real digest of what the clipboard double
        # returns, as in every other sendMine fixture in this class: the
        # decision line under test is logged BEFORE Task 11's verification,
        # so a placeholder would not break this test -- it would merely make
        # the sendMine case emit a stray "clipboard changed before the send"
        # and diverge from its siblings for no reason.
        held = b"whatever we hold"
        held_hash = sha256_hex(held)
        cases = [
            # (stored ts, peer state, expected decision)
            (5, (HASH_B, 9, KIND_TEXT), "waitForPeer"),     # peer fresher
            (5, (held_hash, 999, KIND_TEXT), "doNothing"),  # same hash
            (777, (None, 0, None), "sendMine"),             # peer has nothing
        ]
        for stored_ts, peer, expected in cases:
            with self.subTest(expected):
                save_clip_state(held_hash, stored_ts, KIND_TEXT, path=self.clip_state_path)
                clipboard = QueueClipboard(ready=True)
                clipboard.queue_read(held)
                agent = self.build(clipboard=clipboard)
                agent.send = lambda t, p: None

                original_log = clipwire_agent.log
                log_lines = []
                clipwire_agent.log = log_lines.append
                try:
                    agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(*peer))
                finally:
                    clipwire_agent.log = original_log

                self.assertTrue(
                    any(("reconciled with the peer: %s" % expected) in line for line in log_lines),
                    "expected a line naming %s; got: %r" % (expected, log_lines),
                )

    def test_the_reconciliation_line_names_both_kinds(self):
        """'why did a picture overwrite my text' must have an answer in the
        log. The decision word alone cannot say it -- see this class's other
        reconciliation tests, which never once ask what kind either side
        held. Mirrors HandleFrameTests.swift's
        testTheReconciliationLineNamesBothKinds.

        FOUR elements in each hand-built record since v3.2, when this method
        began asking resolve_provenance first -- which takes WHOLE records
        and raises ValueError on a short one rather than silently comparing
        a timestamp against a hash. Widening two synthetic tuples is not
        defeating that guard: every production producer of `mine`
        (load_clip_state, resolve_current_clip_state, announce_clip_state's
        return, _write_clip's) was already a quadruple, and these two
        literals were the only records in the suite that were not."""
        log_lines = self.capture_log()
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_image_read(b"\x89P")  # the send branch's own verification read
        agent = self.build(clipboard=clipboard)
        mine = (sha256_hex(b"\x89P"), 5000.0, KIND_IMAGE, None)
        peer = (HASH_B, 1000.0, KIND_TEXT, None)

        agent._resolve_clip_state(peer, mine=mine)

        line = next(l for l in log_lines if "reconciled with the peer" in l)
        # One literal, not two independent substrings: pins the separator and
        # the spacing too, the same shape as the "over the image limit" lines
        # elsewhere in this suite, so a change that reordered the pair or
        # dropped the space still goes red here. The leading space is part of
        # the literal deliberately, not decorative: the Swift twin of this
        # line is built from two concatenated string literals with the space
        # on the FIRST one, so a literal starting at "(" would miss a dropped
        # space there. Python's own line is one literal today, but asserting
        # the same leading space here keeps the two tests -- and what they
        # actually pin -- symmetric.
        self.assertIn(" (mine=image peer=text)", line)

    def test_the_line_says_none_when_a_side_holds_nothing(self):
        """The complement: neither side's hash implies neither side's kind,
        and the line must say so rather than omit it or print "None".

        Also the shape provenance must NOT fire on, which is why the two
        null origins here are load-bearing rather than padding: `None ==
        None` is True in Python, so a rule that compared without checking
        presence would stand both sides down on two empty clipboards and
        kill resolve_freshness's (_, None) -> SEND_MINE recovery. Asserting
        a doNothing line here would not catch that (both spellings agree on
        this row); the kinds suffix is what this test is for, and
        test_freshness.py's nil rows are what pin the rule."""
        log_lines = self.capture_log()
        agent = self.build()

        agent._resolve_clip_state((None, 1000.0, None, None), mine=(None, 5000.0, None, None))

        line = next(l for l in log_lines if "reconciled with the peer" in l)
        self.assertIn(" (mine=none peer=none)", line)

    def test_an_applied_pending_clip_supersedes_the_peers_stashed_announcement(self):
        """The reboot flow (acceptance item 5), where the stash is stale by
        construction. The Mac announces its clip-state, then copies again
        and sends the newer clip -- both while this side is still
        PHASE_PENDING, so the announcement is stashed and the clip queued.
        clipboard_became_ready then APPLIES the queued clip and only
        afterwards drains the stash, so it resolves the Mac's superseded
        announcement (ts 1000) against the clip it just applied (ts 3000),
        gets SEND_MINE, and sends the Mac its own clip straight back.

        A clip frame from a peer is strictly NEWER information than that
        same peer's earlier announcement -- the announcement describes what
        the peer held before it sent the clip -- so once the clip has been
        applied there is nothing left in the stash worth resolving.

        The harm is bounded (noteWrittenLocally is armed before the write,
        EchoGuard suppresses, content converges), which is exactly why it
        needs a test: nothing about the end state is wrong, so only the
        redundant frame itself is observable.

        Which makes the SECOND queued read load-bearing rather than
        housekeeping. With the supersede-drop removed, the drain resolves
        SEND_MINE and reaches Task 11's verification read; an exhausted
        QueueClipboard returns None there, which reads as "the clipboard
        changed" and produces exactly the silence this test asserts. The
        defect would pass. Queuing what the applied clip actually put on
        the clipboard lets the verification succeed, so the bug sends the
        redundant frame and is caught -- the discriminator the fast path
        used to supply for free, when _write_clip's remembered bytes were
        what this branch sent."""
        clipboard = QueueClipboard(ready=True)
        clipboard.queue_read(b"whatever the PC held")  # the connect-time seed
        # What the clipboard holds AFTER the pending clip below is applied --
        # consumed only by the send branch's verification read, and only if
        # the supersede-drop is missing. See the docstring.
        clipboard.queue_read(b"the mac's newer clip")
        agent = self.build(clipboard=clipboard, already_reconciled=False)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        # The Mac's announcement, describing what IT held at the time.
        agent.on_frame(TYPE_CLIP_STATE, encode_clip_state(sha256_hex(b"the mac's older clip"), 1000.0, KIND_TEXT))
        # ... then the Mac copies something else and sends it. Still pending
        # here, so it is queued rather than applied.
        agent.on_frame(TYPE_CLIP, encode_clip_payload(3000.0, b"the mac's newer clip"))
        self.assertEqual(sent, [], "nothing may go out before the clipboard is ready")

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

        self.assertEqual(
            [f for f in sent if f[0] == TYPE_CLIP], [],
            "the Mac's own clip must not be sent back to the Mac: its earlier "
            "announcement was superseded by the very clip we just applied",
        )
        self.assertEqual(
            len([f for f in sent if f[0] == TYPE_CLIP_STATE]), 1,
            "our own one-shot announcement must still go out",
        )
        self.assertIsNone(
            agent._pending_peer_clip_state,
            "a superseded stash must be dropped, not left for a later drain",
        )


