# agent/tests/test_lifecycle.py
import io
import os
import tempfile
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    DEGRADED_POLL_SECONDS,
    KIND_IMAGE,
    KIND_TEXT,
    MAX_IMAGE_BYTES,
    PROTOCOL_VERSION,
    REOFFER_TS_NUDGE_SECONDS,
    SAFETY_NET_POLL_SECONDS,
    SEND_MINE,
    TIMESTAMP_BYTES,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
    TYPE_IMAGE_CLIP,
    WAIT_FOR_PEER,
    decode_clip_state,
    decode_frame,
    decode_image_payload,
    encode_clip_payload,
    encode_image_payload,
    load_clip_state,
    resolve_freshness,
    sha256_hex,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to patch make_watcher the same way
# test_watcher.py does, so these lifecycle tests never spawn a real gdbus
# subprocess or leave a polling thread running after the test ends.
import clipwire_agent


class _NoOpWatcher:
    """A watcher double for lifecycle tests that only care about the phase
    machine (clipboard_became_ready()/clipboard_lost()), not the watcher
    itself. Without this, calling clipboard_became_ready() unpatched runs
    the real make_watcher(), which either spawns a real `gdbus introspect`
    (up to 3s if it hangs) or falls back to a PollingWatcher whose
    background thread is never stopped unless the test also calls
    clipboard_lost() -- a leaked daemon thread polling forever."""

    def start(self, on_change):
        pass

    def stop(self):
        pass


class FakeClipboard:
    def __init__(self, ready=False):
        self._ready = ready
        self.written = []

    def ready(self):
        return self._ready

    def become_ready(self):
        self._ready = True

    def read(self):
        return None

    def write(self, kind, data):
        self.written.append((kind, data))


class AsyncWriteClipboard:
    """Models WaylandClipboard's real write() precisely: it spawns wl-copy
    detached (Popen(..., start_new_session=True)) and returns as soon as
    its stdin is closed -- a hand-off, not a confirmation that wl-copy has
    actually registered as the Wayland selection owner yet. So write()
    does NOT change what a subsequent read() returns; the two are
    decoupled in time, exactly like the real asynchronous pair.

    FakeClipboard above genuinely cannot express this: its read() is
    hardcoded to always return None, regardless of what write() was just
    called with, so it has no "stale content" state to return at all.
    test_watcher.py's QueueClipboard actually CAN express the same
    decoupling already -- its read() only ever pops from an explicitly
    queued sequence, never derived from write(), which is exactly this
    same shape (see TestIncomingClipState's own version of this bug in
    test_watcher.py, reproduced by simply not queuing the applied text as
    a read value). AsyncWriteClipboard exists here only because this file
    has no queue-based double to reuse, and a name that says what the
    race is beats "queue nothing and rely on the empty-queue path."
    """

    def __init__(self, read_value, ready=True):
        self._read_value = read_value
        self._ready = ready
        self.written = []

    def ready(self):
        return self._ready

    def read(self):
        # Kind-aware since Task 7 -- always KIND_TEXT here, the only kind
        # any test in this file constructs one of these with.
        return (KIND_TEXT, self._read_value) if self._read_value else None

    def write(self, kind, data):
        self.written.append((kind, data))


class TestLifecycle(unittest.TestCase):
    def setUp(self):
        # clipboard_became_ready()'s new announce-clip-state step persists
        # through save_clip_state/load_clip_state, which touch the real
        # production path (~/.local/state/clipwire/clip-state.json) when
        # clip_state_path is None. Every Agent built in this class gets its
        # own temp path so no test here ever touches that real location.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, ready=False):
        clipboard = FakeClipboard(ready=ready)
        out = io.BytesIO()
        return (
            Agent(stdin=io.BytesIO(), stdout=out, clipboard=clipboard,
                  clip_state_path=self.clip_state_path),
            clipboard,
            out,
        )

    def test_hello_is_sent_before_clipboard_is_ready(self):
        agent, _, out = self.build(ready=False)
        agent.send_hello()
        buffer = bytearray(out.getvalue())
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_HELLO)
        self.assertEqual(json_of(payload)["protocol"], PROTOCOL_VERSION)

    def test_starts_in_clipboard_pending(self):
        agent, _, _ = self.build(ready=False)
        self.assertEqual(agent.phase, "clipboard-pending")

    def test_clip_arriving_while_pending_is_not_written(self):
        agent, clipboard, _ = self.build(ready=False)
        wire_payload = encode_clip_payload(1.0, b"early")
        agent.on_frame(TYPE_CLIP, wire_payload)
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.pending_clip, wire_payload,
                         "pending_clip holds the raw wire payload, not yet decoded")

    def test_only_the_newest_pending_clip_survives(self):
        agent, clipboard, _ = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"first"))
        agent.on_frame(TYPE_CLIP, encode_clip_payload(2.0, b"second"))
        agent.on_frame(TYPE_CLIP, encode_clip_payload(3.0, b"third"))
        clipboard.become_ready()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [(KIND_TEXT, b"third")])

    def test_a_non_finite_timestamp_in_a_pending_clip_does_not_tear_down_the_channel(self):
        """Fix round 1, Finding 2: struct.unpack(">d", ...) inside
        decode_clip_payload used to accept ANY 8-byte pattern, including the
        bit patterns for inf/-inf/nan. Before that was fixed, a clip with a
        non-finite ts queued while pending would get WRITTEN to the
        clipboard by _write_clip (decode succeeded), the persistence
        attempt inside _write_clip would fail and be logged (caught there),
        but clipboard_became_ready's own follow-up
        encode_clip_state(*applied_pending) call -- reusing that same
        non-finite ts to build the announcement frame -- had no local
        try/except, so ClipStateError escaped uncaught and (in the real
        agent) exited main() via `except FrameError`, tearing down the
        whole connection over a clip that had ALREADY been applied.

        A malformed clip payload is deliberately swallowed everywhere else
        (test_watcher.py's TestWriteClipDecodesTheWirePayload) -- the fix
        makes a non-finite ts join that same family at the source
        (decode_clip_payload itself), so this is the same "swallowed
        quietly" outcome: nothing is written, nothing is announced, and
        crucially no exception of any kind reaches this call."""
        agent, clipboard, out = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(float("inf"), b"B"))
        clipboard.become_ready()

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()  # must not raise

        self.assertEqual(
            clipboard.written, [],
            "a non-finite ts must be rejected before ever reaching the clipboard write",
        )
        stored = load_clip_state(path=self.clip_state_path)
        self.assertTrue(
            stored is None or stored[0] is None,
            "nothing about the rejected clip may be persisted",
        )

    def test_becoming_ready_without_a_pending_clip_writes_nothing(self):
        agent, clipboard, _ = self.build(ready=False)
        clipboard.become_ready()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.phase, "ready")

    def test_clip_when_ready_is_written_immediately(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"now"))
        self.assertEqual(clipboard.written, [(KIND_TEXT, b"now")])
        self.assertIsNone(agent.pending_clip)

    def test_empty_clip_is_never_written(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, b"")
        self.assertEqual(clipboard.written, [])

    def test_decodable_but_empty_text_clip_is_never_written(self):
        """Distinct failure mode from the totally-empty payload above: this
        payload DECODES fine (a valid 8-byte ts prefix, no text) but
        carries empty text. decode_clip_payload cannot reject this on its
        own -- an empty bytes object is a valid decode -- so _write_clip
        must still refuse to apply it. Mirrors
        HandleFrameTests.testDecodableButEmptyTextClipTouchesNeitherSuppressionNorThePasteboard."""
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, encode_clip_payload(5.0, b""))
        self.assertEqual(clipboard.written, [])

    def test_losing_the_clipboard_returns_to_pending(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertEqual(agent.phase, "clipboard-pending")
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"during outage"))
        self.assertEqual(clipboard.written, [], "must not write while the session is gone")

    # --- clip-state announcement: sent once, inside clipboard_became_ready ---
    #
    # Per the design spec: clip-state is sent exactly once per connection,
    # inside clipboard_became_ready, after the store has been consulted --
    # not on every frame. This agent process IS one connection (sshd spawns
    # a fresh process per SSH connection), so "once per connection" here
    # means once per process: unlike the Mac side's ClipStateAnnouncement,
    # there is no reconnect-within-the-same-process to re-arm for, so the
    # gate below is never reset.

    def _sent_frame_types(self, out):
        buffer = bytearray(out.getvalue())
        types = []
        while True:
            frame = decode_frame(buffer)
            if frame is None:
                return types
            types.append(frame)

    def test_clipboard_became_ready_sends_clip_state_exactly_once(self):
        agent, clipboard, out = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()

        frames = self._sent_frame_types(out)
        clip_state_frames = [f for f in frames if f[0] == TYPE_CLIP_STATE]
        self.assertEqual(len(clip_state_frames), 1,
                         "clipboard_became_ready must announce our clip-state exactly once")
        # Proves the wiring reaches all the way to a well-formed wire frame,
        # not merely that some bytes typed TYPE_CLIP_STATE were written.
        decode_clip_state(clip_state_frames[0][1])

    def test_a_second_clipboard_became_ready_in_the_same_process_does_not_announce_again(self):
        """The strongest form of "sent once, not again": clipboard_became_ready
        runs twice on the SAME agent (the Wayland session flapping while the
        SSH connection itself stays up) -- the ordinary protocol never does
        this, but the gate, not that assumption, is what must prevent a
        second announcement. Mirrors
        HandleFrameTests.testASecondMatchedHelloInTheSameConnectionDoesNotAnnounceAgain."""
        agent, clipboard, out = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
            agent.clipboard_lost()
            agent.clipboard_became_ready()

        frames = self._sent_frame_types(out)
        clip_state_frames = [f for f in frames if f[0] == TYPE_CLIP_STATE]
        self.assertEqual(len(clip_state_frames), 1,
                         "a second clipboard_became_ready in the same process must not announce again")

    def test_clipboard_became_ready_persists_the_resolved_clip_state(self):
        """"after the store has been consulted" has a second half: the
        reconciled value must also be PERSISTED, so a later .clipState
        comparison (or a crash immediately afterward) sees it, not whatever
        was on disk before this connection began."""
        agent, clipboard, out = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()

        # FakeClipboard.read() always returns None -> resolve_startup_state's
        # None-hash branch -> (None, now).
        stored = load_clip_state(path=self.clip_state_path)
        self.assertIsNotNone(stored, "the resolved clip-state must be persisted, not only sent")
        self.assertIsNone(stored[0])

    def test_clipboard_became_ready_announces_the_just_applied_pending_clip_without_a_racy_reread(self):
        """wl-copy's write() is asynchronous and detached: WaylandClipboard.write()
        spawns it via Popen(..., start_new_session=True) and returns as soon
        as its OWN stdin pipe is closed -- a hand-off, not a guarantee that
        wl-copy has actually registered as the Wayland selection owner yet.

        _write_clip(pending_clip) already does the right thing: it writes,
        then persists (sha256_hex(text), ts) -- the correct, authoritative
        entry. But if clipboard_became_ready's announce step re-reads the
        clipboard through resolve_current_clip_state/announce_clip_state
        immediately afterward, that read can race wl-copy and see stale
        (pre-write) content, or none at all -- and resolve_startup_state
        would then see a hash that does not match what was just stored,
        take the "hashes differ" branch, and announce_clip_state would
        PERSIST that wrong (stale-content, now-stamped) state OVER the
        correct entry _write_clip just saved. That silent clobber is
        exactly the defect this whole design exists to prevent, arriving
        through the async-write door instead of the multi-process one.

        AsyncWriteClipboard models this precisely: its read() always
        returns stale content that predates this connection, regardless of
        what write() was just called with -- the honest shape of the race,
        which FakeClipboard/QueueClipboard (synchronous by construction)
        cannot represent."""
        clipboard = AsyncWriteClipboard(read_value=b"stale content predating this connection")
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        peers_ts = 424242.0
        agent.on_frame(TYPE_CLIP, encode_clip_payload(peers_ts, b"the peer's pending clip"))

        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()

        expected = (sha256_hex(b"the peer's pending clip"), peers_ts, KIND_TEXT, None)
        self.assertEqual(
            load_clip_state(path=self.clip_state_path), expected,
            "the pending clip's own correct, just-persisted state must survive "
            "the announce step, not be overwritten by a racy re-read",
        )


class ReofferingClipboard:
    """A clipboard double whose read() replays a queue of (kind, bytes)
    values that are DELIBERATELY unrelated to whatever write() was handed --
    the shape of the GPaste takeover this class of test exists for.

    Measured on the live PC: a 105,700-byte PNG written to the clipboard
    read back identical at t+1s, then as a DIFFERENT 180,287-byte PNG at
    t+4s and t+7s, because GPaste took over the selection and re-encoded
    the image. So "what write() was handed" and "what read() then offers"
    are two different values, and only a double that can express that
    difference can test the rule.

    Not named ScriptedClipboard even though the plan's sample code calls it
    that: test_mainloop.py already has a ScriptedClipboard, and it is an
    entirely different thing (a ready()/not-ready script that drives
    Agent.run() to its EOF exit). Two same-named doubles with different
    contracts is the mirrored drift this project has already been bitten by
    twice. AsyncWriteClipboard above models the same write/read decoupling
    but with one fixed text read; this one is queue-based because the real
    event sequence is a SEQUENCE -- our own write is observed first, and
    the re-offer only afterwards.

    read() returns None once the queue empties, exactly like
    test_watcher.py's QueueClipboard: no test here observes more changes
    than it scripts, and a double that silently repeated its last value
    would hide a test that fired one observation too many."""

    def __init__(self, reads, ready=True):
        self._reads = list(reads)
        self._ready = ready
        self.written = []

    def ready(self):
        return self._ready

    def read(self):
        return self._reads.pop(0) if self._reads else None

    def write(self, kind, data):
        self.written.append((kind, data))


class ImageAgentTestCase(unittest.TestCase):
    """The harness both image classes below need: a temp clip-state path, a
    captured log, and an Agent whose send() records instead of writing to a
    BytesIO nobody reads.

    Shared rather than duplicated. The two classes ask different questions
    of the same wiring -- TestImageReofferIsOurOwnWrite about the image the
    clipboard hands BACK after our own write, TestImagesEndToEndOnThePC
    about images crossing the wire in either direction -- and a second copy
    of a fifteen-line harness is exactly the mirrored drift this project has
    already been bitten by twice. Deliberately holds no test methods of its
    own, so `unittest discover` collects nothing from it directly."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")
        self.sent = []
        # log() is called by its bare module-level name from every thread, so
        # replacing the module attribute captures every line without touching
        # process-wide sys.stderr. Same idiom as test_mainloop.py's own
        # transition-logging test.
        self.logged = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.logged.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def build(self, clipboard, clip_state_path=None):
        built = Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
            clip_state_path=self.clip_state_path if clip_state_path is None
            else clip_state_path,
        )
        built.send = lambda frame_type, payload: self.sent.append((frame_type, payload))
        return built

    def become_ready(self, agent_obj):
        """make_watcher patched for the same reason _NoOpWatcher exists: an
        unpatched clipboard_became_ready() spawns a real `gdbus introspect`
        or leaks a PollingWatcher thread that never stops."""
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent_obj.clipboard_became_ready()


class TestImageReofferIsOurOwnWrite(ImageAgentTestCase):
    """The PC must hash what the clipboard OFFERS BACK, never what it handed
    the write tool. For text the two coincide; for images on the PC they
    provably do not (see ReofferingClipboard for the measurement).

    Hashing what was written costs two things. One: a guaranteed extra
    round trip on every screenshot Mac->PC, since the re-offer raises an
    Update the agent would not recognise as its own and would send straight
    back. Two, and worse: the store would hold a hash the clipboard does
    not offer, so EVERY reconnect -- that is, every Mac wake -- would
    resolve "clipboard changed while apart", stamp ts=now, and let a stale
    image win reconciliation against fresher content on the Mac.

    No sleeps anywhere here, and none in the implementation either: the
    takeover was measured between one and four seconds, on one machine, on
    one day, so a fixed wait would be a race dressed as a constant. The
    agent consumes the first DIFFERING image observation as its own
    re-offer instead."""

    def test_a_gpaste_re_encode_after_our_write_is_not_sent_back(self):
        """GPaste re-encodes an image when it takes over the selection, so the
        bytes we wrote and the bytes the clipboard then offers differ -- always,
        and by a lot: measured 105,700 in and 180,287 out. Hashing what we wrote
        makes the PC send the re-encoded copy straight back."""
        written = b"\x89PNG-original"
        reoffered = b"\x89PNG-reencoded-by-gpaste-and-larger"
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, reoffered)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, written), kind=KIND_IMAGE)
        self.sent.clear()
        agent_obj._local_change()          # the Update GPaste's takeover raises
        self.assertEqual(self.sent, [], "the re-offer is our own write, not a new clip")
        self.assertEqual(agent_obj._last_seen, (KIND_IMAGE, sha256_hex(reoffered)),
                         "and the hash we keep must be the one the clipboard reports")

    def test_the_stored_hash_is_the_re_offered_one(self):
        """Otherwise every reconnect sees a mismatch and stamps ts=now, letting a
        stale image beat fresher content on the peer -- a false 'clipboard changed
        while apart' on every single Mac wake."""
        written = b"\x89PNG-original"
        reoffered = b"\x89PNG-reencoded-by-gpaste-and-larger"
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "clip-state.json")
            clip = ReofferingClipboard(reads=[(KIND_IMAGE, reoffered)])
            agent_obj = self.build(clipboard=clip, clip_state_path=path)
            agent_obj._write_clip(encode_image_payload(1000.0, written), kind=KIND_IMAGE)
            agent_obj._local_change()
            stored = load_clip_state(path=path)
        self.assertEqual(stored[0], sha256_hex(reoffered))
        self.assertEqual(stored[2], KIND_IMAGE)
        self.assertNotEqual(stored[0], sha256_hex(written),
                            "storing the written hash is what breaks every reconnect")

    def test_the_stored_timestamp_is_the_peers_own_plus_one_millisecond(self):
        """The other half of the store entry, and the one the final review
        found deciding a reconnect by coin flip.

        The age recorded here is the PEER's, not observed_at -- the re-offer
        is not a new clip, it is our own applied one re-encoded, and
        stamping the moment we observed it would make an image the Mac sent
        us look freshly copied ON THE PC, beat anything the Mac copied in
        between, and push the re-encoded copy back over the original.

        But it cannot be EXACTLY the peer's either, and that is what this
        test now pins. The two sides genuinely hold different bytes for the
        same picture -- the Mac has the PNG it sent, this side has GPaste's
        re-encode -- so at an identical ts the shared formula falls through
        to its hex tie-break, and the winner is decided by hash bytes. Half
        the time the Mac wins, re-sends the original, GPaste re-encodes it
        back, the store is restored unchanged, and the next reconnect
        repeats it identically: a loop with no exit, forever.

        REOFFER_TS_NUDGE_SECONDS breaks that tie in the one direction that
        terminates. See the constant's own comment for why a millisecond,
        and _consume_image_reoffer for the cost this buys."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, b"\x89PNG-reencoded")])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-original"),
                              kind=KIND_IMAGE)
        agent_obj._local_change()
        stored_ts = load_clip_state(path=self.clip_state_path)[1]
        self.assertEqual(stored_ts, 1000.0 + REOFFER_TS_NUDGE_SECONDS)
        self.assertGreater(stored_ts, 1000.0,
                           "an equal ts hands the reconnect to the hex tie-break")
        self.assertLess(stored_ts, 1000.5,
                        "and observed_at would beat everything the peer copied in between")

    def test_the_reconnect_no_longer_turns_on_a_hash_coin_flip(self):
        """The failure that made this a blocker, stated as the decision both
        sides actually run.

        HASH_FLOOR and HASH_CEILING bracket every possible sha256 hex
        digest, so asserting against both covers BOTH orderings of the real
        pair -- which is the whole point: the outcome must not depend on
        which way the re-encode's digest happens to sort against the
        original's. Before the nudge, one of these two resolved the other
        way, and that half never converged.

        Both directions of the same comparison are asserted, because the
        two machines run the identical formula on swapped arguments: the PC
        pushing once is only an exit if the Mac agrees to wait for it."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, b"\x89PNG-reencoded")])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-original"),
                              kind=KIND_IMAGE)
        agent_obj._local_change()
        mine = load_clip_state(path=self.clip_state_path)[:2]

        for peer_hash in ("0" * 64, "f" * 64):
            with self.subTest(peer_hash=peer_hash[:4]):
                peer = (peer_hash, 1000.0)
                self.assertEqual(resolve_freshness(mine, peer), SEND_MINE,
                                 "this side must deterministically carry the re-encode")
                self.assertEqual(resolve_freshness(peer, mine), WAIT_FOR_PEER,
                                 "and the Mac must deterministically wait for it")

    def test_our_own_write_observed_before_the_re_offer_does_not_spend_the_expectation(self):
        """The sequence the live machine actually produces, which the plan's
        two sample tests skip: our wl-copy write raises an Update of its own,
        and only SECONDS later does GPaste take over and raise a second one.
        So the first image observation is the bytes we wrote, and the re-offer
        is the one after it.

        An expectation consumed by the first image observation whatever it is
        -- the echo guard's own rule, correct there -- would be spent on our
        own bytes here and leave the real re-offer to be read as a fresh local
        clip. Consumed by the first DIFFERING one instead: an observation that
        matches _last_seen changed nothing, so there is nothing to consume."""
        written = b"\x89PNG-original"
        reoffered = b"\x89PNG-reencoded-by-gpaste-and-larger"
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, written), (KIND_IMAGE, reoffered)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, written), kind=KIND_IMAGE)
        self.sent.clear()

        agent_obj._local_change()          # our own write, observed
        self.assertEqual(agent_obj._last_seen, (KIND_IMAGE, sha256_hex(written)))

        agent_obj._local_change()          # GPaste's takeover, seconds later
        self.assertEqual(self.sent, [], "the re-offer is still our own write")
        self.assertEqual(agent_obj._last_seen, (KIND_IMAGE, sha256_hex(reoffered)),
                         "the expectation must survive an observation of our own bytes")

    def test_a_text_copy_clears_the_expectation(self):
        """A text copy means the user moved on and the re-offer will never
        come. Left armed, the expectation would swallow the next unrelated
        image the user copies -- days later, silently -- which is the
        lingering-guard defect the echo guard's own one-shot rule exists to
        avoid.

        Since Task 12 that swallowing is directly observable rather than
        inferred: an image with no expectation armed is a genuine local clip
        and goes out as a TYPE_IMAGE_CLIP frame, so "the expectation was
        cleared" and "the next image was sent" are the same statement. That
        is a strictly stronger discriminator than the _last_seen check this
        test used before -- a lingering expectation now shows up as a clip
        the peer never receives, not merely as a field left unadvanced."""
        png = b"\x89PNG-a-genuinely-new-image"
        clip = ReofferingClipboard(reads=[(KIND_TEXT, b"the user moved on"),
                                          (KIND_IMAGE, png)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-original"),
                              kind=KIND_IMAGE)

        agent_obj._local_change()          # the text copy
        self.assertIsNone(agent_obj._expect_reoffer,
                          "a text observation spends the expectation")
        self.sent.clear()

        agent_obj._local_change()          # a genuinely new image, much later
        self.assertEqual(
            [frame_type for frame_type, _ in self.sent], [TYPE_IMAGE_CLIP],
            "an image arriving with no expectation armed is not our own re-offer -- "
            "it is the user's clip, and it must reach the peer",
        )
        self.assertEqual(decode_image_payload(self.sent[0][1])[1], png)

    def test_an_image_with_no_expectation_armed_is_not_treated_as_a_re_offer(self):
        """The positive control that stops the re-offer rule from being
        'silently absorb every image observation'. Nothing was written here,
        so nothing is expected, and the observation is a clip the user made
        on the PC: it must be sent, and remembered as content the peer now
        holds.

        Before Task 12 this asserted the opposite half of the same rule --
        nothing sent, _last_seen and the store both left untouched -- because
        a locally observed image had nowhere to go yet. The rule under test
        is unchanged; what changed is where an image the re-offer path
        DECLINES ends up."""
        png = b"\x89PNG-copied-by-the-user"
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, png)])
        agent_obj = self.build(clipboard=clip)
        with mock.patch("time.time", return_value=4242.0):
            agent_obj._local_change()
        self.assertEqual([frame_type for frame_type, _ in self.sent], [TYPE_IMAGE_CLIP])
        self.assertEqual(decode_image_payload(self.sent[0][1]), (4242.0, png))
        self.assertEqual(agent_obj._last_seen, (KIND_IMAGE, sha256_hex(png)),
                         "an image the peer now holds is recorded as synced")
        self.assertEqual(
            load_clip_state(path=self.clip_state_path),
            (sha256_hex(png), 4242.0, KIND_IMAGE, None),
            "stamped with the moment it was observed here, not a peer timestamp: "
            "this clip was born on the PC",
        )

    def test_an_oversized_re_offer_is_still_recorded_and_says_so(self):
        """Re-encoding INFLATES -- 105 KB became 180 KB -- so an image
        comfortably under the limit going in can exceed it coming out, and
        the limit has to be applied to the read-back body too.

        Applied as a log line, not as a refusal to record: the re-offer path
        sends nothing either way, and NOT recording it is what would leave
        the store holding a hash the clipboard no longer offers -- the false
        'clipboard changed while apart' on every wake this whole task exists
        to close, reappearing for exactly the images least able to afford a
        4 MiB resend. The size is logged because an image this side can never
        forward is otherwise a silent skip, and a silent skip is how a user
        concludes the tool is broken."""
        huge = b"\x89" + b"P" * MAX_IMAGE_BYTES
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, huge)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-small-original"),
                              kind=KIND_IMAGE)
        self.sent.clear()
        agent_obj._local_change()
        self.assertEqual(self.sent, [])
        self.assertEqual(agent_obj._last_seen, (KIND_IMAGE, sha256_hex(huge)))
        self.assertEqual(load_clip_state(path=self.clip_state_path)[0], sha256_hex(huge))
        self.assertIn(str(len(huge)), "\n".join(self.logged),
                      "a silent skip is how a user concludes the tool is broken")

    def test_a_text_clip_still_applies_exactly_as_before(self):
        """_write_clip grew a `kind` parameter for this task; its default and
        its whole text path must be untouched by that. Both existing callers
        pass a TYPE_CLIP payload positionally and must keep decoding through
        decode_clip_payload, writing KIND_TEXT, and storing the peer's ts."""
        clip = ReofferingClipboard(reads=[])
        agent_obj = self.build(clipboard=clip)
        applied = agent_obj._write_clip(encode_clip_payload(1000.0, b"plain text"))
        self.assertEqual(applied, (sha256_hex(b"plain text"), 1000.0, KIND_TEXT, None),
                         "four elements since v3.2, with a null origin: this value "
                         "travels to _resolve_clip_state as `mine`, where "
                         "resolve_provenance refuses a short record outright")
        self.assertEqual(clip.written, [(KIND_TEXT, b"plain text")])
        self.assertIsNone(agent_obj._expect_reoffer,
                          "a text write expects no re-offer -- text reads back unchanged")


class TestTheReofferRecordsWhereTheBytesCameFrom(ImageAgentTestCase):
    """v3.2, Task 3. The PC is the only witness to the substitution -- it
    wrote the Mac's bytes and read different ones back with nobody touching
    the clipboard in between -- and until now it threw that testimony away
    and left both machines to infer provenance from content that provably
    no longer matches (measured: 398,267 of 614,400 samples differ).

    So the re-offer's store entry names THE HASH IT WAS GIVEN. Recorded on
    the branch that consumes the expectation and on no other: the other two
    return True without consuming -- a read that predates a newer write,
    and an observation that changed nothing -- and an origin stamped on
    either would label a clip THE USER COPIED as a derivative of the peer's
    content. Under v3.2 such a clip is not sent one connection late; it is
    never sent at all."""

    WRITTEN = b"\x89PNG-original-from-the-mac"
    REOFFERED = b"\x89PNG-reencoded-by-gpaste-and-larger"

    def test_the_consumed_re_offer_records_the_peers_own_hash(self):
        """The peer's hash is never plumbed in from anywhere: the bytes
        handed to the clipboard are the bytes the Mac hashed and announced,
        so their digest IS the peer's canonical hash, and _write_clip has
        it in hand at the moment it arms the expectation."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, self.REOFFERED)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, self.WRITTEN), kind=KIND_IMAGE)
        agent_obj._local_change()

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored[0], sha256_hex(self.REOFFERED),
                         "the hash is still what the clipboard offers")
        self.assertEqual(stored[3], sha256_hex(self.WRITTEN),
                         "and the origin is what the peer sent us, which is the one "
                         "fact no comparison of the two clipboards could recover")

    def test_an_observation_of_our_own_bytes_records_no_origin(self):
        """The second of the three True branches. Our own wl-copy write
        raises an Update seconds before GPaste's takeover raises the real
        one, so the first image observation after a write is normally the
        bytes we wrote. Nothing changed, so nothing is consumed -- and
        nothing may be labelled either."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, self.WRITTEN)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, self.WRITTEN), kind=KIND_IMAGE)
        agent_obj._local_change()

        self.assertIsNone(
            load_clip_state(path=self.clip_state_path)[3],
            "an observation that changed nothing has no ancestry to declare",
        )

    def test_a_stale_read_racing_a_newer_write_records_no_origin(self):
        """The third. The read could predate the newer write entirely, so
        the bytes in hand are not the re-offer of anything -- and the newer
        write has already persisted its own entry, which this must not
        overwrite with an ancestry claim about the previous one."""
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[]))
        agent_obj.clipboard = RacyImageClipboard(
            agent_obj,
            value_read=b"\x89PNG-captured-before-the-newer-write",
            interleaved_write=encode_image_payload(2.0, b"\x89PNG-newer-from-the-peer"),
        )
        agent_obj._local_change()

        self.assertIsNone(
            load_clip_state(path=self.clip_state_path)[3],
            "a read that predates a newer write is nobody's descendant",
        )

    def test_the_origin_survives_the_agent_that_recorded_it(self):
        """The point of putting it in the store at all, stated as the
        failure it prevents: sshd spawns a NEW agent per connection, so the
        process that watched the substitution is already dead by the
        announce that needs to describe it. An origin living only in
        _expect_reoffer would be a fix that changes nothing on the machine
        -- for the third release running.

        TWO reconnects, not one, and the second is the one that bites. Each
        announce persists what it resolved, so a resolve_startup_state that
        rebuilt the matched record around `current_hash` -- dropping the
        origin while looking correct on all three fields anyone thinks to
        check -- would write the stripped record back and take the SECOND
        connection down while the first still passed. That is the shape
        this pair exists to catch."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, self.REOFFERED)])
        first = self.build(clipboard=clip)
        first._write_clip(encode_image_payload(1000.0, self.WRITTEN), kind=KIND_IMAGE)
        first._local_change()

        for attempt in ("first reconnect", "second reconnect"):
            with self.subTest(attempt=attempt):
                self.sent.clear()
                # Two reads because a connection makes two: the connect-time
                # _last_seen seed, then the announce's own reconciliation.
                reconnected = self.build(clipboard=ReofferingClipboard(
                    reads=[(KIND_IMAGE, self.REOFFERED)] * 2))
                self.become_ready(reconnected)

                announced = [decode_clip_state(payload)
                             for frame_type, payload in self.sent
                             if frame_type == TYPE_CLIP_STATE]
                self.assertEqual(len(announced), 1)
                self.assertEqual(
                    announced[0][3], sha256_hex(self.WRITTEN),
                    "the connection that has to say where the bytes came from is "
                    "never the connection that found out",
                )
                self.assertEqual(
                    load_clip_state(path=self.clip_state_path)[3],
                    sha256_hex(self.WRITTEN),
                    "and the announce must persist it again, or the fix survives "
                    "exactly one reconnect",
                )


class TestTheExpectationIsDisarmedByObservation(ImageAgentTestCase):
    """v3.2, Task 5. On an installation with no GPaste the re-offer never
    comes, so an expectation armed by an applied image used to stay armed
    until the user's NEXT image copy, which was absorbed as though it were
    the re-offer. That cost one clip arriving a connection late -- the
    store carried it at the peer's ts plus the nudge, the PC won the
    reconnect, and it went across. With an origin stamped on it the Mac
    matches the ancestor, provenance stands both sides down, and the clip
    is gone. v3.2 turns a late clip into a lost one, so the expectation has
    to be disarmed when the re-offer does not come.

    NOT BY A CLOCK, and the two mines are both in the phrase "30 seconds":

      * the takeover happens at one to four seconds, but the window must
        cover when it is OBSERVED, and with the event source dead only the
        safety-net poll sees it -- up to a full interval later. A wall-clock
        deadline and the tick carrying the observation then race, and the
        phase decides: 29.9s absorbs correctly, 30.1s ships the degraded
        copy live with no origin recorded.
      * after the two-tick verdict the poll runs at DEGRADED_POLL_SECONDS,
        SHORTER than the takeover, so a window meaning "the current
        interval" would disarm deterministically before the re-offer.

    So one read decides both, and the branches are mutually exclusive by
    CONTENT: differing bytes are the re-offer whatever the clock says, and
    our own bytes past the constant mean there is nothing here that
    re-encodes clipboards."""

    WRITTEN = b"\x89PNG-applied-from-the-mac"
    LATER = b"\x89PNG-the-user-copied-this-days-later"

    def armed(self, at=1000.0):
        """An agent holding an image applied from the peer, written at a
        known instant so the tests below can place an observation either
        side of the threshold."""
        clip = ReofferingClipboard(reads=[])
        agent_obj = self.build(clipboard=clip)
        with mock.patch("time.time", return_value=at):
            agent_obj._write_clip(encode_image_payload(1000.0, self.WRITTEN),
                                  kind=KIND_IMAGE)
        self.sent.clear()
        return agent_obj, clip

    def observe(self, agent_obj, clip, value, at):
        clip._reads.append(value)
        with mock.patch("time.time", return_value=at):
            agent_obj._local_change()

    def test_our_own_bytes_past_the_threshold_disarm_it(self):
        agent_obj, clip = self.armed()
        self.observe(agent_obj, clip, (KIND_IMAGE, self.WRITTEN),
                     at=1000.0 + SAFETY_NET_POLL_SECONDS)

        self.assertIsNone(agent_obj._expect_reoffer,
                          "a clipboard still offering our own bytes a whole detection "
                          "budget later is a machine with nothing re-encoding it")
        self.assertEqual(self.sent, [],
                         "and disarming sends nothing: the peer already holds this")
        self.assertTrue(any("no re-offer" in line for line in self.logged),
                        "a suppression that leaves no trace is indistinguishable from "
                        "a bug: %r" % self.logged)

    def test_our_own_bytes_before_the_threshold_leave_it_armed(self):
        """The control. GPaste's takeover was measured at one to four
        seconds, and the observation of our own write lands before it every
        time -- disarming there would ship the re-encode to the Mac live,
        as a genuine local change, with no origin recorded. That is the
        original bug, delivered faster."""
        agent_obj, clip = self.armed()
        self.observe(agent_obj, clip, (KIND_IMAGE, self.WRITTEN),
                     at=1000.0 + SAFETY_NET_POLL_SECONDS - 0.1)

        self.assertIsNotNone(agent_obj._expect_reoffer,
                             "the takeover happens seconds after the write, and our own "
                             "write's echo is observed before it")

    def test_a_re_offer_first_observed_after_the_threshold_is_consumed_not_disarmed(self):
        """THE PHASE CASE, and the one a wall-clock deadline gets wrong. With
        the event source dead the substitution is seen only by the
        safety-net poll, up to a full interval after it happened -- so the
        observation legitimately arrives past the threshold. It is still
        the re-offer, and content says so: these bytes are not the ones we
        wrote. Consumed, whatever the clock says."""
        agent_obj, clip = self.armed()
        reoffered = b"\x89PNG-reencoded-by-gpaste"
        self.observe(agent_obj, clip, (KIND_IMAGE, reoffered),
                     at=1000.0 + SAFETY_NET_POLL_SECONDS * 1.5)

        self.assertEqual(self.sent, [],
                         "the re-offer is our own write coming back, not a local clip")
        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored[0], sha256_hex(reoffered))
        self.assertEqual(stored[3], sha256_hex(self.WRITTEN),
                         "and it must still record where the bytes came from -- a "
                         "disarm here is what delivers the degraded copy live")

    def test_the_window_is_the_constant_never_the_interval_in_force(self):
        """The second mine. After the two-tick dead-source verdict the poll
        runs at DEGRADED_POLL_SECONDS for the rest of the connection --
        ONE second, shorter than the measured takeover -- so a window
        defined as "the current interval", or as any multiple of it, would
        disarm deterministically before the re-offer could arrive, in the
        mode the machine stays in for the whole connection once the verdict
        ever fires.

        Pinned as the inequality rather than as a number, so it stays true
        if either constant is tuned."""
        self.assertLess(DEGRADED_POLL_SECONDS, SAFETY_NET_POLL_SECONDS,
                        "if these ever meet, this test proves nothing")
        agent_obj, clip = self.armed()
        self.observe(agent_obj, clip, (KIND_IMAGE, self.WRITTEN),
                     at=1000.0 + DEGRADED_POLL_SECONDS * 2)

        self.assertIsNotNone(
            agent_obj._expect_reoffer,
            "two ticks of the degraded interval is not the detection budget: the "
            "takeover has not even happened yet",
        )

    def test_after_a_disarm_the_next_image_is_the_users_own_clip(self):
        """What the disarm is FOR, end to end. The image the user copies
        after a disarm goes to the peer as a genuine local clip, stamped
        with the moment it was observed and carrying no ancestry -- rather
        than being absorbed as a re-offer that never came, stored under the
        peer's timestamp with an origin the Mac still matches, and
        therefore never sent at all."""
        agent_obj, clip = self.armed()
        self.observe(agent_obj, clip, (KIND_IMAGE, self.WRITTEN),
                     at=1000.0 + SAFETY_NET_POLL_SECONDS)
        self.sent.clear()

        self.observe(agent_obj, clip, (KIND_IMAGE, self.LATER), at=5000.0)

        self.assertEqual([frame_type for frame_type, _ in self.sent], [TYPE_IMAGE_CLIP])
        self.assertEqual(decode_image_payload(self.sent[0][1]), (5000.0, self.LATER))
        self.assertEqual(
            load_clip_state(path=self.clip_state_path),
            (sha256_hex(self.LATER), 5000.0, KIND_IMAGE, None),
            "a clip born on the PC carries the PC's timestamp and no ancestry",
        )

    def test_the_poll_only_looks_once_the_expectation_is_overdue(self):
        """The predicate the poll's no-change branch asks, and the only
        thing that gets the disarm observed at all on a machine whose
        clipboard never moves again. It reads no clipboard and decides
        nothing, so the file keeps one place that decides what a local
        change means and one thread inside it.

        OVERDUE, not merely armed. Every True here costs the worker a full
        read -- --list-types plus the entire image body -- and in degraded
        mode the ticks are a second apart, so "armed" would pipe up to
        MAX_IMAGE_BYTES thirty times per applied image, which is exactly
        the expense probe() exists to have deleted. Waiting costs nothing:
        a real re-offer changes the offered type list and arrives through
        the poll's CHANGE branch, and the disarm needs one look."""
        agent_obj, clip = self.armed()

        with mock.patch("time.time", return_value=1000.0 + 1):
            self.assertFalse(agent_obj._reoffer_pending(),
                             "a re-offer that is merely awaited is not worth a "
                             "4 MiB read every second")
        with mock.patch("time.time", return_value=1000.0 + SAFETY_NET_POLL_SECONDS):
            self.assertTrue(agent_obj._reoffer_pending(),
                            "and one that is overdue must be looked at, or the disarm "
                            "is a branch nothing ever reaches")

        self.observe(agent_obj, clip, (KIND_IMAGE, self.WRITTEN),
                     at=1000.0 + SAFETY_NET_POLL_SECONDS)
        with mock.patch("time.time", return_value=9999.0):
            self.assertFalse(agent_obj._reoffer_pending(),
                             "and it must stop asking once there is nothing to wait "
                             "for, or the poll observes a static clipboard forever")

    def poll(self, agent_obj, ticks):
        """The poll's no-change branch, driven exactly as PollingWatcher runs
        it: ask the predicate, and read ONLY when it says True. Calling
        _local_change() unconditionally would prove nothing about the thing
        under test, which is how often the predicate provokes a read."""
        for at in ticks:
            with mock.patch("time.time", return_value=at):
                if agent_obj._reoffer_pending():
                    agent_obj._local_change()

    # The moments the regression was measured at: the first tick past the
    # budget, the three after it, and two far enough out to show it never
    # stops. In degraded mode these are one second apart, so "every tick" is
    # a wl-paste PAIR PER SECOND for the life of the connection.
    LOOKS = [1000.0 + offset for offset in (30, 31, 32, 33, 90, 630)]

    def test_a_clipboard_that_will_not_answer_is_looked_at_once(self):
        """THE REGRESSION on_idle_tick introduced, and the one shape the
        predicate cannot survive: nothing stops it asking except the
        expectation going away, and a read that never reaches
        _consume_image_reoffer never takes it away. Every tick past the
        budget then provokes a fresh full clipboard read -- --list-types
        plus the whole image body -- forever, which is precisely the expense
        probe() was introduced to delete, reintroduced through the one
        branch that bypasses it.

        Bounded (one process per SSH connection) and lossless, so it is a
        cost rather than a corruption -- but _reoffer_pending's own
        docstring asserts the disarm needs exactly ONE look, and a comment
        that denies a measured behaviour is worse than the behaviour."""
        agent_obj, _ = self.armed()
        clip = SilentClipboard()
        agent_obj.clipboard = clip

        self.poll(agent_obj, self.LOOKS)

        self.assertEqual(clip.reads, 1,
                         "a clipboard that will not answer is worth exactly one "
                         "look; after that the poll is paying a full image read "
                         "per tick to be told nothing, for the whole connection")
        self.assertIsNone(agent_obj._expect_reoffer,
                          "and the only thing that stops the asking is the "
                          "expectation going away")
        self.assertTrue(any("giving up on the re-offer" in line for line in self.logged),
                        "giving up is a decision about the machine and must leave a "
                        "trace, like the disarm beside it: %r" % self.logged)

    def test_an_image_offered_with_no_bytes_is_looked_at_once(self):
        """The same hole through the other early return: a type on offer
        with nothing behind it. read() itself collapses an empty body to
        None on the real clipboard (WaylandClipboard._read_body), so this
        arrives only from a double or from whatever a later wl-paste does
        -- pinned anyway, because the defect was never about WHICH read
        failed. It was about a look that cannot reach a content decision
        leaving the expectation armed."""
        agent_obj, _ = self.armed()
        clip = SilentClipboard(value=(KIND_IMAGE, b""))
        agent_obj.clipboard = clip

        self.poll(agent_obj, self.LOOKS)

        self.assertEqual(clip.reads, 1)
        self.assertIsNone(agent_obj._expect_reoffer)

    def test_a_body_less_read_of_another_kind_is_looked_at_once(self):
        """The THIRD early return, and the one no brief named: the kind
        guard on the text path, reached by an empty text body today and by
        any kind read() learns to return later. Covered because the rule is
        about the INVARIANT -- no path out of _observe_local_change may
        leave an overdue expectation armed -- not about the two paths that
        happened to be measured. A fourth early return added later without
        this call is the same defect again."""
        agent_obj, _ = self.armed()
        clip = SilentClipboard(value=(KIND_TEXT, b""))
        agent_obj.clipboard = clip

        self.poll(agent_obj, self.LOOKS)

        self.assertEqual(clip.reads, 1)
        self.assertIsNone(agent_obj._expect_reoffer)

    def test_a_look_that_fails_before_the_budget_leaves_it_armed(self):
        """The control, and the reason giving up is gated on OVERDUE rather
        than on the read having failed. GPaste's takeover was measured
        between one and four seconds; a transient wl-paste failure inside
        that window is not evidence about the machine, and disarming on it
        would ship the re-encode to the Mac live with no origin recorded --
        the original bug, for that image.

        Driven through _local_change directly rather than through the poll,
        because the predicate would not ask this early: the CHANGE branch is
        what delivers an observation before the budget."""
        agent_obj, _ = self.armed()
        agent_obj.clipboard = SilentClipboard()

        with mock.patch("time.time", return_value=1000.0 + 1):
            agent_obj._local_change()

        self.assertIsNotNone(agent_obj._expect_reoffer,
                             "a failed read inside the takeover window says nothing "
                             "about whether a re-offer is coming")

    def test_a_newer_write_is_not_disarmed_by_an_older_reads_failure(self):
        """The generation check, which is this file's recorded defect shape
        rather than a hypothetical: the read is in flight for two wl-paste
        round trips, and a _write_clip on the main thread inside that window
        arms a BRAND-NEW expectation. Giving up on it would disarm an
        image applied microseconds ago, and the re-offer it is still waiting
        for would then go to the Mac as a local change."""
        agent_obj, _ = self.armed()
        agent_obj.clipboard = RacyImageClipboard(
            agent_obj,
            value_read=b"",
            interleaved_write=encode_image_payload(2.0, b"\x89PNG-newer-from-the-peer"),
        )

        with mock.patch("time.time", return_value=1000.0 + SAFETY_NET_POLL_SECONDS):
            agent_obj._local_change()

        self.assertIsNotNone(agent_obj._expect_reoffer,
                             "the expectation this read could have judged is not the "
                             "one that is armed by the time it finishes")
        self.assertEqual(agent_obj._expect_reoffer[0], 2.0,
                         "and the armed one is the newer write's")


class SilentClipboard(ReofferingClipboard):
    """A clipboard whose read() always gives the same unjudgeable answer, and
    COUNTS how many times it was asked. The count is the whole point: the
    regression this pins is not a wrong value anywhere, it is how much work
    the poll provokes to learn nothing.

    Separate from ReofferingClipboard's own empty-queue None, which means
    "this test scripted no further observations" -- a different claim, and
    one that would make the count meaningless if the two shared a double."""

    def __init__(self, value=None):
        super().__init__(reads=[])
        self._value = value
        self.reads = 0

    def read(self):
        self.reads += 1
        return self._value


class RacyImageClipboard:
    """The image twin of test_watcher.py's RacyClipboard: read() drives a
    _write_clip on the way past, so a newer write lands "on the main thread"
    while this read is still in flight -- deterministically, from one thread,
    rather than by racing two real ones. The real window is TWO wl-paste round
    trips, and on an image clipboard they are SUBPROCESS_TIMEOUT=3s
    (--list-types) followed by IMAGE_SUBPROCESS_TIMEOUT=10s (the body) -- 13
    seconds in total, not either one alone, during which any number of frames
    can arrive and be applied."""

    def __init__(self, agent, value_read, interleaved_write):
        self._agent = agent
        self._value_read = value_read
        self._interleaved_write = interleaved_write

    def ready(self):
        return True

    def read(self):
        self._agent._write_clip(self._interleaved_write, kind=KIND_IMAGE)
        return (KIND_IMAGE, self._value_read)

    def write(self, kind, data):
        pass


class TestImagesEndToEndOnThePC(ImageAgentTestCase):
    """Images cross the wire in both directions on this side now: a
    TYPE_IMAGE_CLIP frame from the peer is applied to the local clipboard
    (queueing first if the Wayland session has not appeared yet), and an
    image the watcher observes goes out as a TYPE_IMAGE_CLIP frame of our
    own.

    The outbound half sits AFTER the re-offer consumer in
    _observe_local_change, and that ordering is the whole of Task 10: an
    image observation is our own applied clip coming back re-encoded far
    more often than it is a new one, and a send placed before the consumer
    would bounce every single Mac->PC image straight back. See
    TestImageReofferIsOurOwnWrite above for the rule; the tests there fail
    if this send is hoisted above it."""

    def test_an_image_frame_is_applied_to_the_clipboard(self):
        png = b"\x89PNG-from-the-peer"
        clip = ReofferingClipboard(reads=[])
        agent_obj = self.build(clipboard=clip)
        self.become_ready(agent_obj)
        agent_obj.on_frame(TYPE_IMAGE_CLIP, encode_image_payload(1000.0, png))
        self.assertEqual(clip.written, [(KIND_IMAGE, png)])
        self.assertEqual(
            load_clip_state(path=self.clip_state_path),
            (sha256_hex(png), 1000.0, KIND_IMAGE, None),
            "the PEER's timestamp, never now: applied content that looked freshly "
            "copied here would win the next reconciliation against the machine it "
            "came from",
        )

    def test_an_image_arriving_before_the_clipboard_is_ready_is_queued_and_applied_later(self):
        """The pre-login window, which is the ordinary case rather than an
        edge one: the Mac reconnects the moment it wakes, and sshd spawns
        this agent whether or not anyone has logged into the Wayland session
        yet. An image must queue exactly as text does."""
        png = b"\x89PNG-arrived-before-login"
        payload = encode_image_payload(1000.0, png)
        clip = ReofferingClipboard(reads=[], ready=False)
        agent_obj = self.build(clipboard=clip)

        agent_obj.on_frame(TYPE_IMAGE_CLIP, payload)
        self.assertEqual(clip.written, [])
        self.assertEqual(agent_obj.pending_clip, payload,
                         "pending_clip holds the raw wire payload, not yet decoded")
        self.assertEqual(agent_obj.pending_clip_kind, KIND_IMAGE)

        clip._ready = True
        self.become_ready(agent_obj)
        self.assertEqual(clip.written, [(KIND_IMAGE, png)])
        self.assertIsNone(agent_obj.pending_clip)

    def test_a_text_clip_superseding_a_queued_image_is_applied_as_text(self):
        """"Keep only the newest" has a second half once two kinds can queue:
        the remembered KIND must be superseded along with the payload, and
        nothing about the payload itself would reveal a failure to do so. An
        image payload and a text payload are the SAME shape on the wire --
        [f64 BE ts][body] -- so decoding a text payload as an image succeeds
        and writes the user's text to the clipboard as image/png. A wrong
        kind here is silent at every layer below it."""
        clip = ReofferingClipboard(reads=[], ready=False)
        agent_obj = self.build(clipboard=clip)
        agent_obj.on_frame(TYPE_IMAGE_CLIP, encode_image_payload(1.0, b"\x89PNG-superseded"))
        agent_obj.on_frame(TYPE_CLIP, encode_clip_payload(2.0, b"the newer text"))
        clip._ready = True
        self.become_ready(agent_obj)
        self.assertEqual(clip.written, [(KIND_TEXT, b"the newer text")])

    def test_an_image_superseding_a_queued_text_clip_is_applied_as_an_image(self):
        """The same rule in the other direction, and the one a
        KIND_TEXT-defaulting apply would fail: an image decoded through the
        text codec writes PNG bytes to the clipboard as text/plain."""
        png = b"\x89PNG-the-newer-one"
        clip = ReofferingClipboard(reads=[], ready=False)
        agent_obj = self.build(clipboard=clip)
        agent_obj.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"superseded text"))
        agent_obj.on_frame(TYPE_IMAGE_CLIP, encode_image_payload(2.0, png))
        clip._ready = True
        self.become_ready(agent_obj)
        self.assertEqual(clip.written, [(KIND_IMAGE, png)])

    def test_a_pending_image_applied_on_readiness_still_expects_its_re_offer(self):
        """Ordering, pinned. clipboard_became_ready clears any stale re-offer
        expectation alongside the connect-time _last_seen seed -- BEFORE the
        queued clip is applied -- so a pending IMAGE re-arms it on its way
        through _write_clip. Clearing it after the apply instead would wipe
        the expectation that apply had just armed, and GPaste's re-encode of
        the peer's own image would go straight back to the peer."""
        png = b"\x89PNG-queued-before-login"
        clip = ReofferingClipboard(reads=[], ready=False)
        agent_obj = self.build(clipboard=clip)
        agent_obj.on_frame(TYPE_IMAGE_CLIP, encode_image_payload(1000.0, png))
        clip._ready = True
        self.become_ready(agent_obj)
        self.assertEqual(agent_obj._expect_reoffer[:2], (1000.0, sha256_hex(png)),
                         "the peer's timestamp AND the peer's hash: the digest of the "
                         "bytes just written is the peer's own, and this process dies "
                         "with the connection, so the store write it authorises is the "
                         "only place that fact survives")

    def test_a_wayland_flap_does_not_leave_a_re_offer_expectation_armed(self):
        """_expect_reoffer is cleared only by an image or a text OBSERVATION,
        and a Wayland session that went away and came back cannot re-offer a
        write made into the session before it. Left armed across the flap, it
        would be spent on the first image the user copies afterwards:
        absorbed as our own re-offer, stored under the PEER's timestamp --
        making a clip born here look as old as the applied one -- and never
        sent. Harmless before Task 12, when a locally observed image was
        never sent anyway; a silent loss now."""
        clip = ReofferingClipboard(reads=[], ready=True)
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-applied-before-the-flap"),
                              kind=KIND_IMAGE)
        self.assertEqual(agent_obj._expect_reoffer[0], 1000.0)

        agent_obj.clipboard_lost()
        self.become_ready(agent_obj)

        self.assertIsNone(agent_obj._expect_reoffer,
                          "a session that went away cannot re-offer our write")

    def test_an_image_observed_locally_is_sent_as_type_3(self):
        """The frame type and the codec, pinned together: a TYPE_CLIP frame
        carrying PNG bytes would decode on the Mac as text and be applied as
        mojibake, and a TYPE_IMAGE_CLIP frame built with encode_clip_payload
        would differ only in that it skips the finite-ts guard."""
        png = b"\x89PNG-copied-here"
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[(KIND_IMAGE, png)]))
        with mock.patch("time.time", return_value=1234.5):
            agent_obj._local_change()
        self.assertEqual(len(self.sent), 1)
        frame_type, payload = self.sent[0]
        self.assertEqual(frame_type, TYPE_IMAGE_CLIP)
        self.assertEqual(decode_image_payload(payload), (1234.5, png))

    def test_an_oversized_image_is_skipped_with_its_size_in_the_log(self):
        huge = b"\x89" * (MAX_IMAGE_BYTES + 1)
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[(KIND_IMAGE, huge)]))
        agent_obj._local_change()
        self.assertEqual(self.sent, [])
        self.assertIn(str(len(huge)), "\n".join(self.logged),
                      "a silent skip is how a user concludes the tool is broken")
        self.assertIn(
            "over the image limit", "\n".join(self.logged),
            "one verdict clause for one limit: byte-identical to the re-offer path's "
            "own oversize line, so the two cannot drift apart in wording",
        )
        self.assertIsNone(
            load_clip_state(path=self.clip_state_path),
            "an image that was never sent must not be recorded as though it had been",
        )

    def test_an_image_at_exactly_the_limit_is_sent(self):
        """The boundary the separated caps exist for: this body plus its
        8-byte timestamp exceeds the OLD single 4 MiB cap and must still go
        out. MAX_IMAGE_BYTES bounds the IMAGE, not the frame -- the frame cap
        is MAX_PAYLOAD_BYTES, twice as large, with room for the prefix by
        construction."""
        exact = b"\x89" * MAX_IMAGE_BYTES
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[(KIND_IMAGE, exact)]))
        agent_obj._local_change()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.sent[0][1]), MAX_IMAGE_BYTES + TIMESTAMP_BYTES,
                         "the payload exceeds the image limit and is still legal")

    def test_an_image_the_peer_already_holds_is_not_resent(self):
        """GPaste emits an Update for things that are not clipboard changes
        at all -- deleting an entry from its history is one -- and _last_seen,
        unlike the one-shot echo value, never expires. An image observation
        matching it changed nothing, so nothing goes out."""
        png = b"\x89PNG-already-synced"
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, png), (KIND_IMAGE, png)])
        agent_obj = self.build(clipboard=clip)
        agent_obj._local_change()
        self.assertEqual(len(self.sent), 1)
        agent_obj._local_change()          # a spurious Update, same content
        self.assertEqual(len(self.sent), 1, "a non-change signal must not resend")

    def test_a_stale_image_read_racing_a_newer_write_is_not_sent(self):
        """The image twin of test_watcher.py's
        test_a_write_that_lands_during_the_read_is_not_echoed. Before Task 12
        a stale image read had nowhere to go, so the staleness check only
        protected the store; it now stands between a read that predates a
        newer write and sending the peer its own image straight back."""
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[]))
        agent_obj.clipboard = RacyImageClipboard(
            agent_obj,
            value_read=b"\x89PNG-captured-before-the-newer-write",
            interleaved_write=encode_image_payload(2.0, b"\x89PNG-newer-from-the-peer"),
        )
        agent_obj._local_change()
        self.assertEqual(self.sent, [],
                         "a read that predates a newer write is not a local change")
        self.assertEqual(agent_obj._expect_reoffer[0], 2.0,
                         "and the newer write's expectation must stay armed for its own re-offer")

    def test_an_empty_image_read_is_not_sent(self):
        """read() reporting KIND_IMAGE with no bytes is a wl-paste that
        offered the type and then produced nothing -- a transient failure,
        not a clip. encode_image_payload would refuse it on the far side
        anyway (decode_image_payload rejects an empty body), so sending it
        would be a frame the peer discards."""
        agent_obj = self.build(clipboard=ReofferingClipboard(reads=[(KIND_IMAGE, b"")]))
        agent_obj._local_change()
        self.assertEqual(self.sent, [])
        self.assertIsNone(agent_obj._last_seen)


def json_of(payload):
    import json
    return json.loads(payload.decode())


if __name__ == "__main__":
    unittest.main()
