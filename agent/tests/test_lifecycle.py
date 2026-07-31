# agent/tests/test_lifecycle.py
import io
import os
import tempfile
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
    decode_clip_state,
    decode_frame,
    encode_clip_payload,
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

    def write(self, data):
        self.written.append(data)


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
        return self._read_value  # never reflects write() below -- the real race

    def write(self, data):
        self.written.append(data)


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
        self.assertEqual(clipboard.written, [b"third"])

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
        self.assertEqual(clipboard.written, [b"now"])
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

        expected = (sha256_hex(b"the peer's pending clip"), peers_ts)
        self.assertEqual(
            load_clip_state(path=self.clip_state_path), expected,
            "the pending clip's own correct, just-persisted state must survive "
            "the announce step, not be overwritten by a racy re-read",
        )


def json_of(payload):
    import json
    return json.loads(payload.decode())


if __name__ == "__main__":
    unittest.main()
