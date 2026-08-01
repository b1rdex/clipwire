# agent/tests/test_lifecycle.py
import io
import os
import tempfile
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    KIND_IMAGE,
    KIND_TEXT,
    MAX_IMAGE_BYTES,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
    decode_clip_state,
    decode_frame,
    encode_clip_payload,
    encode_image_payload,
    load_clip_state,
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

        expected = (sha256_hex(b"the peer's pending clip"), peers_ts, KIND_TEXT)
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


class TestImageReofferIsOurOwnWrite(unittest.TestCase):
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

    def test_the_stored_timestamp_stays_the_peers_own(self):
        """The other half of the store entry, and the plan's sample code does
        not pin it. The re-offer is not a new clip -- it is our own applied
        one, re-encoded -- so its recorded age is still the PEER's, exactly
        as _write_clip already stores for text. Stamping observed_at here
        would make an image the Mac sent us look freshly copied ON THE PC,
        win the next reconciliation against the machine it came from, and
        push the re-encoded copy back over the original: failure #1 arriving
        through the store instead of through an unrecognised Update."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, b"\x89PNG-reencoded")])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-original"),
                              kind=KIND_IMAGE)
        agent_obj._local_change()
        self.assertEqual(load_clip_state(path=self.clip_state_path)[1], 1000.0,
                         "the peer's timestamp, never the moment we observed the re-offer")

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
        avoid."""
        clip = ReofferingClipboard(reads=[(KIND_TEXT, b"the user moved on"),
                                          (KIND_IMAGE, b"\x89PNG-a-genuinely-new-image")])
        agent_obj = self.build(clipboard=clip)
        agent_obj._write_clip(encode_image_payload(1000.0, b"\x89PNG-original"),
                              kind=KIND_IMAGE)

        agent_obj._local_change()          # the text copy
        self.assertIsNone(agent_obj._expect_reoffer,
                          "a text observation spends the expectation")

        agent_obj._local_change()          # a genuinely new image, much later
        self.assertEqual(
            agent_obj._last_seen, (KIND_TEXT, sha256_hex(b"the user moved on")),
            "an image arriving with no expectation armed is not our own re-offer",
        )

    def test_an_image_with_no_expectation_armed_is_not_treated_as_a_re_offer(self):
        """The positive control that stops the fix from being 'silently
        absorb every image observation'. Nothing was written here, so nothing
        is expected, and the observation must fall through to the same
        not-yet-synced outcome an image already had -- sending images the
        watcher observes is a later task's job, and swallowing them into
        _last_seen here would make that task's own tests pass for the wrong
        reason."""
        clip = ReofferingClipboard(reads=[(KIND_IMAGE, b"\x89PNG-copied-by-the-user")])
        agent_obj = self.build(clipboard=clip)
        agent_obj._local_change()
        self.assertEqual(self.sent, [])
        self.assertIsNone(agent_obj._last_seen,
                          "an unexpected image is not recorded as already-synced")
        self.assertIsNone(load_clip_state(path=self.clip_state_path))

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
        self.assertEqual(applied, (sha256_hex(b"plain text"), 1000.0, KIND_TEXT))
        self.assertEqual(clip.written, [(KIND_TEXT, b"plain text")])
        self.assertIsNone(agent_obj._expect_reoffer,
                          "a text write expects no re-offer -- text reads back unchanged")


def json_of(payload):
    import json
    return json.loads(payload.decode())


if __name__ == "__main__":
    unittest.main()
