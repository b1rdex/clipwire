# agent/tests/test_watcher.py
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

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to patch make_watcher the same way
# test_mainloop.py patches log(), and to locate the source file for the
# module-ordering check at the bottom of this file.
import clipwire_agent

AGENT = pathlib.Path(__file__).resolve().parents[1] / "clipwire-agent.py"
JOIN_TIMEOUT = 2  # generous relative to the millisecond-scale intervals below
# A verbatim line captured from the target machine -- see
# TestGPasteSignalParsing.test_accepts_the_real_captured_signal.
GPASTE_UPDATE_LINE = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                      "('REPLACE', 'ALL', uint64 0)")

# 64 lowercase hex characters: the only shape decode_clip_state accepts,
# and the only shape hashlib.sha256(...).hexdigest() -- hence the wire --
# ever produces. Obviously fake, but well-formed, so these tests exercise
# the same path a real digest does instead of one the protocol forbids.
# See _is_sha256_hex on why that validation exists at all. HASH_A sorts
# below HASH_B, which resolve_freshness's hash tie-break depends on.
HASH_A = "aa" * 32
HASH_B = "bb" * 32


class FakeGPasteProcess:
    """Stands in for the subprocess.Popen object GPasteWatcher.start()
    creates. Backed by a real pipe, so the reader thread genuinely blocks
    waiting for a line the way it would against a real gdbus monitor
    process; terminate() closes the write end, which is what lets the
    thread's `for line in stdout` end through EOF -- exactly as it would
    once a killed real process's pipe closes."""

    def __init__(self):
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "r")
        self._writer = os.fdopen(write_fd, "w")
        self.terminated = False

    def emit(self, line):
        self._writer.write(line + "\n")
        self._writer.flush()

    def terminate(self):
        self.terminated = True
        if not self._writer.closed:
            self._writer.close()

    def close(self):
        if not self.stdout.closed:
            self.stdout.close()
        if not self._writer.closed:
            self._writer.close()


def _reject_image_shaped(script):
    """Guards the `probe = read` aliases below, at construction, on the
    test's own thread.

    The poll loop asks its clipboard for a change TOKEN, not content -- see
    WaylandClipboard.probe -- and those doubles answer probe() with their
    read() because their scripts are opaque comparable values with no image
    body behind them. Script one with a real (KIND_IMAGE, bytes) pair and the
    equivalence silently becomes a LIE: the loop would compare image bodies,
    which production never does, and every test built on that double would
    pass for behaviour the agent does not have. ProbeOnlyClipboard catches
    the loop reverting to read(); nothing catches this.

    Checked here rather than inside probe() because the poll loop wraps every
    tick in _handle_observer_error, which would swallow an AssertionError
    raised on that thread into a log line nobody asserts on.
    """
    for value in script:
        if isinstance(value, tuple) and value and value[0] == KIND_IMAGE:
            raise AssertionError(
                "%r is image-shaped: this double answers probe() with read(), so "
                "an image BODY here would be compared as a token. Give it a "
                "probe() of its own instead." % (value,))
    return list(script)


class ScriptedReadClipboard:
    """read() replays a fixed script, then repeats its last value forever --
    so a poll tick that lands after the test stops watching cannot raise
    IndexError. `last` records the most recent value returned, so a test
    callback can observe what the watcher just saw without threading the
    value through on_change() itself (the real interface takes none).

    `paced` is what makes that observation DETERMINISTIC, and every test
    asserting on which values were seen needs it. Since the watchers stopped
    calling the handler on their reader threads, a change is signalled and the
    handler runs later, on the worker: an unpaced script can advance `last`
    between the two, and two changes inside one worker turnaround coalesce into
    a single observation. Both are correct in production -- the real handler
    re-reads current state -- and both make "which values were observed" depend
    on the scheduler.

    Paced, read() refuses to hand out a NEW value until the previous change has
    been taken through take(). That is the same "block inside read() until the
    other thread has caught up" handshake SignallingClipboard uses below, and
    it removes the race structurally rather than making it unlikely. Bounded by
    JOIN_TIMEOUT so a change nobody observes fails an assertion instead of
    hanging the suite."""

    def __init__(self, script, paced=False):
        self._script = _reject_image_shaped(script)
        self.calls = 0
        self.last = None
        self._paced = paced
        # Set means "nothing is waiting to be observed", so the baseline read
        # is free.
        self._taken = threading.Event()
        self._taken.set()

    def take(self):
        """For a test's on_change: the value the watcher just observed, and the
        release that lets a paced script move on. Safe to call on an unpaced
        clipboard, so one callback shape works for both."""
        value = self.last
        self._taken.set()
        return value

    def read(self):
        if self._paced:
            self._taken.wait(JOIN_TIMEOUT)
        index = min(self.calls, len(self._script) - 1)
        self.calls += 1
        value = self._script[index]
        # calls > 1 because the poll's FIRST read is its baseline, which it
        # never reports: pending an observation on it would park the next read
        # for the whole timeout.
        if self._paced and self.calls > 1 and value != self.last:
            self._taken.clear()
        self.last = value
        return self.last

    # PollingWatcher's loop asks for a change TOKEN, not content -- see
    # WaylandClipboard.probe. This double's script is already a cheap
    # in-memory value with no image body behind it, so the token and the
    # read are literally the same call here. TestPollingWatcherUsesProbe
    # is what pins that the loop actually asks for the token; an alias
    # cannot, by construction.
    probe = read


class SignallingClipboard:
    """A clipboard whose read() DRIVES the signal source: on the tick that
    first sees new content it emits a real GPaste Update line and blocks until
    the pump has delivered it, only then returning the changed value. So "the
    signal for this very change has already been delivered" is a fact by the
    time the safety net compares, not a timing hope -- the same
    deterministic-side-effect-inside-read() trick RacyClipboard uses further
    down this file, instead of racing two real threads and hoping.

    Deliberately waits on the CALLBACK having run rather than on the watcher's
    private signal counter, so the test is evidence about observable behaviour
    rather than about an attribute.

    What it does NOT prove: that the pump counts a signal BEFORE dispatching
    it. In a count-after-dispatch variant the increment and the dispatch are
    adjacent bytecodes on the pump thread while the released poll thread still
    has to be rescheduled, so this test would normally pass anyway. That
    ordering is argued in the pump's own comment and is deliberately left
    untested rather than pinned by a test that would pass most of the time."""

    def __init__(self, before, after):
        _reject_image_shaped((before, after))
        self.process = None       # set by start_watcher, before anything reads
        self.delivered = threading.Event()
        self.reports = 0
        self._before = before
        self._after = after
        self.calls = 0
        self.last = None

    def observe(self):
        """The on_change both paths share: counts reports and releases the
        read() waiting for the signal to have been delivered."""
        self.reports += 1
        self.delivered.set()

    def read(self):
        self.calls += 1
        if self.calls == 1:
            self.last = self._before   # the poll loop's own baseline
            return self.last
        if not self.delivered.is_set():
            self.process.emit(GPASTE_UPDATE_LINE)
            self.delivered.wait(JOIN_TIMEOUT)
        self.last = self._after
        return self.last

    # PollingWatcher's loop asks for a change TOKEN, not content -- see
    # WaylandClipboard.probe. This double's script is already a cheap
    # in-memory value with no image body behind it, so the token and the
    # read are literally the same call here. TestPollingWatcherUsesProbe
    # is what pins that the loop actually asks for the token; an alias
    # cannot, by construction.
    probe = read


class ArmThenSignalClipboard:
    """Drives the exact interleaving the two-tick verdict exists to absorb: the
    content changes, the tick that sees it arms the verdict, and only THEN is
    the Update signal for that very change counted -- before the tick that
    would confirm. A healthy source whose signal was merely in flight looks
    precisely like this, and it is reachable now that the poll loop no longer
    spends a wl-paste round trip inside on_change.

    The counter is moved from inside the read belonging to the confirming tick,
    on the poll thread itself, so it is provably in place before that tick's
    comparison -- the same deterministic-side-effect-inside-read() trick
    SignallingClipboard uses above, rather than racing two real threads and
    hoping. It is bumped directly rather than through a real gdbus line because
    WHICH reader moved the counter is irrelevant to the rule under test, and a
    real line would have to beat the confirming tick through the pump thread to
    count."""

    def __init__(self, before, after):
        _reject_image_shaped((before, after))
        self.watcher = None       # set by start_watcher, before anything reads
        self.calls = 0
        self.last = None
        self._before = before
        self._after = after

    def take(self):
        return self.last

    def read(self):
        self.calls += 1
        if self.calls == 1:
            self.last = self._before   # the poll loop's own baseline
        elif self.calls == 2:
            self.last = self._after    # the change: this tick arms the verdict
        elif self.calls == 3:
            self.watcher._signals += 1
        return self.last

    # PollingWatcher's loop asks for a change TOKEN, not content -- see
    # WaylandClipboard.probe. This double's script is already a cheap
    # in-memory value with no image body behind it, so the token and the
    # read are literally the same call here. TestPollingWatcherUsesProbe
    # is what pins that the loop actually asks for the token; an alias
    # cannot, by construction.
    probe = read


class QueueClipboard:
    """A clipboard double whose read() replays a queue of scripted values --
    lets a test dictate exactly what Agent._local_change observes on each
    call, independent of any subprocess or timing. write() records what the
    agent wrote locally.

    queue_read(value) queues a TEXT read -- the shape every test in this
    file that predates Task 7 already exercises -- auto-wrapped here as
    (KIND_TEXT, value) to match WaylandClipboard.read()'s (kind, bytes)
    contract, so none of those existing call sites needed to change.
    None still queues a "nothing there" read. queue_image_read(value)
    queues an image read directly, for the tests that need one."""

    def __init__(self, ready=True):
        self._ready = ready
        self._queue = []
        self.written = []

    def queue_read(self, value):
        self._queue.append(None if value is None else (KIND_TEXT, value))

    def queue_image_read(self, value):
        self._queue.append(None if value is None else (KIND_IMAGE, value))

    def ready(self):
        return self._ready

    def read(self):
        return self._queue.pop(0) if self._queue else None

    def write(self, kind, data):
        self.written.append((kind, data))


class SpyWatcher:
    """A watcher double for tests that only care whether Agent wired
    start()/stop() correctly -- it never spawns a thread or a subprocess."""

    def __init__(self):
        self.started_with = None
        self.start_count = 0
        self.stopped = False

    def start(self, on_change):
        self.started_with = on_change
        self.start_count += 1

    def stop(self):
        self.stopped = True


class TestEchoBookkeeping(unittest.TestCase):
    """_write_clip is the single funnel for local writes; _local_change is
    the single gate deciding whether an observed change is our own echo.
    Task 8's tests only exercise the immediate write path -- several of
    these cover the pending-clip path too, which is easy to forget."""

    def setUp(self):
        # _write_clip, _local_change and clipboard_became_ready's new
        # announce step all persist through save_clip_state/load_clip_state,
        # which default to the real production path
        # (~/.local/state/clipwire/clip-state.json) when clip_state_path is
        # None. Every Agent built in this class gets its own temp path so no
        # test in this file ever touches that real location.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def build(self, ready=False):
        return Agent(
            stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=ready),
            clip_state_path=self.clip_state_path,
        )

    def become_ready_without_a_real_watcher(self, agent):
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=SpyWatcher()):
            agent.clipboard_became_ready()

    def test_write_clip_writes_and_arms_the_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"hello")])
        self.assertEqual(agent._last_written, b"hello")

    def test_immediate_clip_delivery_arms_the_suppression(self):
        agent = self.build(ready=True)
        self.become_ready_without_a_real_watcher(agent)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"now"))
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"now")])
        self.assertEqual(agent._last_written, b"now")

    def test_pending_clip_delivery_arms_the_same_suppression(self):
        """If clipboard_became_ready() wrote pending_clip directly instead of
        through _write_clip, delivery would leave no suppression armed, and
        the very next poll tick would bounce our own delivered clip back to
        the peer as if the user had copied it."""
        agent = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, b"queued while pending"))
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)
        self.assertEqual(agent.clipboard.written, [(KIND_TEXT, b"queued while pending")])
        self.assertEqual(agent._last_written, b"queued while pending")

    def test_matching_echo_is_suppressed_and_consumed(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(b"hello")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])
        self.assertIsNone(
            agent._last_written, "the suppression must be consumed even on a match"
        )

    def test_mismatched_echo_is_still_consumed(self):
        """Consume-on-mismatch, pinned directly. This was a real defect
        once (see EchoGuard's doc comment and its Swift mirror,
        shouldSend): a suppression that only clears on a MATCH stays
        armed forever once the watcher's one poll interval misses the
        write's own change event and observes something else first --
        silently swallowing the very next deliberate re-copy of the
        original text. The suppression must be consumed by the first
        observed change, whatever it is, not only by a matching one."""
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(b"something else")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1234.5):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1234.5, b"something else"))])
        self.assertIsNone(
            agent._last_written,
            "the suppression must be consumed on a mismatch too, not only on a match",
        )

    def test_fresh_agent_sends_a_genuine_local_change(self):
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1234.5):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1234.5, b"typed by the user"))])

    def test_deliberate_recopy_still_syncs_after_a_missed_echo(self):
        """Mirrors EchoGuardTests.testDeliberateRecopyStillSyncsAfterAMissedEcho
        on the Swift side, so the two implementations cannot drift on it."""
        agent = self.build(ready=True)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        our_write = b"our own write"
        something_else = b"a different clip the user made"

        agent._write_clip(encode_clip_payload(1.0, our_write))
        agent.clipboard.queue_read(something_else)
        with mock.patch("time.time", return_value=10.0):
            agent._local_change()  # the poll missed our echo, saw the user's clip instead

        agent.clipboard.queue_read(our_write)
        with mock.patch("time.time", return_value=20.0):
            agent._local_change()  # a deliberate re-copy of our own text, later

        self.assertEqual(
            sent,
            [(TYPE_CLIP, encode_clip_payload(10.0, something_else)),
             (TYPE_CLIP, encode_clip_payload(20.0, our_write))],
            "a later deliberate re-copy of our own text must still sync",
        )

    def test_deliberate_recopy_still_syncs_after_a_pending_delivery_misses_its_echo(self):
        """Same scenario, but the write that gets echoed-past is the
        pending-clip delivery, not an immediate one -- the path Task 8 never
        exercised."""
        agent = self.build(ready=False)
        delivered = b"queued while pending"
        agent.on_frame(TYPE_CLIP, encode_clip_payload(1.0, delivered))
        agent.clipboard._ready = True
        self.become_ready_without_a_real_watcher(agent)

        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        something_else = b"a different clip the user made"

        agent.clipboard.queue_read(something_else)
        with mock.patch("time.time", return_value=10.0):
            agent._local_change()

        agent.clipboard.queue_read(delivered)
        with mock.patch("time.time", return_value=20.0):
            agent._local_change()

        self.assertEqual(
            sent,
            [(TYPE_CLIP, encode_clip_payload(10.0, something_else)),
             (TYPE_CLIP, encode_clip_payload(20.0, delivered))],
        )

    def test_empty_read_does_not_consume_an_armed_suppression(self):
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"hello"))
        agent.clipboard.queue_read(None)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])
        self.assertEqual(
            agent._last_written, b"hello",
            "an empty read must not consume an armed suppression",
        )

    def test_oversized_local_change_is_skipped_not_sent(self):
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"x" * (MAX_TEXT_BYTES + 1))
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [])

    def test_text_at_exactly_the_cap_is_skipped_because_the_encoded_frame_would_exceed_it(self):
        """_local_change wraps the observed text in encode_clip_payload
        before it reaches the wire, adding an 8-byte timestamp prefix -- so
        text at exactly MAX_TEXT_BYTES would encode to a payload 8 bytes
        over the TEXT limit. The pre-existing guard (len(text) >
        MAX_TEXT_BYTES) cannot see this boundary: it only rejects text
        already over the cap, one byte too late for content exactly AT it.
        Since Task 4, MAX_TEXT_BYTES (this guard) and MAX_PAYLOAD_BYTES (the
        wire's frame cap, enforced only by decode_frame) are separate
        constants -- text here is checked against the text-content limit,
        not the larger frame cap that exists to leave room for images.
        Mirrors
        PasteboardTests.testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt
        on the Swift side."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"x" * MAX_TEXT_BYTES)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(sent, [], "text at exactly the cap would encode to a payload 8 bytes over it")

    def test_text_leaving_exact_room_for_the_timestamp_prefix_still_sends(self):
        """The other half of the boundary: content that leaves exact room
        for the 8-byte timestamp prefix must still be sent -- an
        over-trimmed fix would silently refuse to sync content the wire
        format actually supports."""
        agent = self.build(ready=True)
        text = b"x" * (MAX_TEXT_BYTES - TIMESTAMP_BYTES)
        agent.clipboard.queue_read(text)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, text))])

    def test_local_change_stamps_the_send_with_the_moment_of_observation(self):
        """The timestamp half of the new send path: the frame must carry
        the moment _local_change observed the change, not some later
        moment. Mirrors
        AgentWiringTests.testAGenuineLocalChangeStoresItsHashAndObservationTimestamp
        on the Swift side, but pins it against the SENT FRAME here rather
        than the store (a separate test below pins the store)."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        before = time.time()
        agent._local_change()
        after = time.time()
        self.assertEqual(len(sent), 1)
        ts, text = decode_clip_payload(sent[0][1])
        self.assertEqual(text, b"typed by the user")
        self.assertTrue(before <= ts <= after, "expected %r to fall within [%r, %r]" % (ts, before, after))

    def test_local_change_persists_the_observed_hash_and_timestamp(self):
        """Mirrors AgentWiringTests.testAGenuineLocalChangeStoresItsHashAndObservationTimestamp:
        a genuine local change must hash the new content and persist it
        alongside the moment it was OBSERVED, so resolve_startup_state has
        something accurate to compare against on the next connection."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"typed by the user")
        agent.send = lambda t, p: None
        before = time.time()
        agent._local_change()
        after = time.time()

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored[0], sha256_hex(b"typed by the user"))
        self.assertTrue(before <= stored[1] <= after,
                        "expected %r to fall within [%r, %r]" % (stored[1], before, after))

    def test_local_change_stores_the_literal_known_hash_for_a_pinned_vector(self):
        """The outgoing-path twin of
        TestWriteClipDecodesTheWirePayload.test_stores_the_literal_known_hash_for_a_pinned_vector --
        the same call-site-hashing bug could hide on either side of the
        wire independently."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"hi")
        agent.send = lambda t, p: None
        agent._local_change()

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(
            stored[0],
            "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4",
        )

    def test_local_change_survives_a_real_disk_failure_when_persisting_clip_state(self):
        """A local disk failure must not prevent the genuine local change
        from being sent to the peer -- the peer did nothing wrong. Forces a
        real save failure rather than mocking it, mirroring
        TestWriteClipDecodesTheWirePayload's own version of this test."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable_path = os.path.join(blocker, "clip-state.json")
        with self.assertRaises(OSError):
            save_clip_state(HASH_A, 1, KIND_TEXT, path=unsaveable_path)

        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(ready=True),
                      clip_state_path=unsaveable_path)
        agent.clipboard.queue_read(b"typed by the user")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent._local_change()

        self.assertEqual(len(sent), 1, "a local disk failure must not prevent the send")
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"typed by the user")

    # MARK: - _last_seen: persistent memory of what was last synced
    #
    # Final-review Finding 1: _local_change() used to decide "the user
    # changed the clipboard" from *a signal fired* plus *content differs
    # from the one-shot echo value* (_last_written/expected). With no
    # memory of what was last synced, any signal that is not a real change
    # -- a non-change GPaste Update (e.g. a history deletion), or a
    # transient probe() glitch in polling mode -- re-sends the current
    # content even though nothing actually changed, and can race a real
    # incoming write and clobber it. _last_seen is set in _write_clip
    # (content arriving from the peer), after a successful send (content
    # leaving to the peer, on both _observe_local_change's path and
    # _resolve_clip_state's), by clipboard_became_ready's connect-time
    # seed, and -- since Task 10 -- in _consume_image_reoffer (the peer's
    # own image as this clipboard re-encoded it), and _local_change returns
    # early whenever the freshly read content already matches it.

    def test_non_change_signal_after_receiving_a_clip_produces_no_send(self):
        """Failure A from the final review, receive side: "A" arrives from
        the Mac (_write_clip). The watcher's own poll observes the echo of
        that write (consuming _last_written) -- and then a LATER,
        unrelated, non-change signal fires with the clipboard still
        reading "A". _last_written is already spent, so only _last_seen
        can recognize this as not a genuine change."""
        agent = self.build(ready=True)
        agent._write_clip(encode_clip_payload(1.0, b"A"))  # "A" arrived from the Mac
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()  # the echo of our own write: consumed, no send
        self.assertEqual(sent, [])

        agent.clipboard.queue_read(b"A")  # a later, non-change signal
        agent._local_change()
        self.assertEqual(
            sent, [],
            "a non-change signal after the echo is already consumed must not "
            "resend content already known to be in sync -- Failure A from the "
            "final review",
        )

    def test_non_change_signal_after_a_send_produces_no_send(self):
        """Failure A from the final review, send side: the PC itself sent
        "A" earlier (a genuine local change). A later non-change signal
        fires with the clipboard still reading "A" and nothing armed in
        _last_written (never armed by an outgoing send in the first
        place) -- only _last_seen, set after that earlier successful
        send, can recognize this as not a genuine change."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()  # a genuine first sync of "A"
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))])

        agent.clipboard.queue_read(b"A")  # unchanged content, spurious signal
        agent._local_change()
        self.assertEqual(
            sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))],
            "a non-change signal must not resend content already known to be in sync",
        )

    def test_transient_read_glitch_does_not_cause_a_duplicate_send(self):
        """Failure B from the final review: PollingWatcher's own
        previous/current tracking is fooled by a transient read() that
        returns None (e.g. a wl-paste timeout), which makes it treat the
        next successful read of the SAME content as a change and fire
        on_change() -- potentially on more than one tick. Without
        _last_seen, _local_change has no way to recognize that content as
        already synced and resends it every time it fires."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"A")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        with mock.patch("time.time", return_value=1.0):
            agent._local_change()  # "A" is now the known-synced value
        self.assertEqual(sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))])

        # PollingWatcher's read() returned None on a transient timeout, so it
        # treats this tick's previous(A)->current(None) transition as a
        # change and fires on_change() -- but _local_change's OWN read()
        # call recovers immediately and sees "A" again, not None.
        agent.clipboard.queue_read(b"A")
        agent._local_change()
        # The very next tick, the same recovery repeats.
        agent.clipboard.queue_read(b"A")
        agent._local_change()

        self.assertEqual(
            sent, [(TYPE_CLIP, encode_clip_payload(1.0, b"A"))],
            "a transient read() glitch that recovers to the same content must not resend it",
        )

    def test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send(self):
        """Second-round final review, Finding 1: _last_seen starts None, and
        this agent lives exactly one connection (sshd spawns a fresh
        process per SSH connection). Without a seed, ANY reconnect -- a
        Mac sleep/wake or a network blip, not only a PC reboot, since
        those also spawn a brand-new agent while the Wayland session is
        already up -- lets the first spurious signal (GPasteWatcher's pump
        has no content baseline of its own, unlike PollingWatcher) send
        whatever the PC's clipboard already held, and the Mac applies it
        unconditionally: destroying a copy made on the Mac while the
        channel was down. clipboard_became_ready() must seed _last_seen
        from the clipboard before the watcher can observe anything."""
        agent = self.build(ready=True)
        # Two reads happen inside clipboard_became_ready() now: the seed
        # read below, and a second read from the new announce-clip-state
        # step's own resolve_current_clip_state call. QueueClipboard.read()
        # pops one value per call, so both must be queued or the second
        # (the announce's) would see an empty queue and read as None --
        # not a bug in the code under test, just this queue-based double
        # needing to reflect the extra call.
        agent.clipboard.queue_read(b"already on the pc")  # the seed read
        agent.clipboard.queue_read(b"already on the pc")  # the announce step's own read
        self.become_ready_without_a_real_watcher(agent)
        self.assertEqual(agent._last_seen, (KIND_TEXT, sha256_hex(b"already on the pc")))

        agent.clipboard.queue_read(b"already on the pc")  # the spurious signal's read
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(
            sent, [],
            "a spurious signal right after connect must not resend content the PC "
            "already held before anything synced",
        )

    def test_an_image_only_clipboard_at_connect_seeds_its_baseline_and_announces_its_kind(self):
        """The Agent-level close of the loop resolve_startup_state's fix
        opens, one frame up from TestResolveCurrentClipState's direct-call
        coverage in test_clip_state_store.py: clipboard_became_ready's own
        seed read AND announce_clip_state's read (called from inside it)
        both go through this same kind-aware clipboard.read() now.

        The seed covers both kinds since Task 12, and had to: a
        locally-observed image is SENT now, so an image-only clipboard with
        no baseline lets the first spurious signal after connect push
        whatever the PC already held at the Mac, which applies it
        unconditionally -- destroying a copy made there while the channel
        was down. That is exactly the clobber
        test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send
        just above pins for text, arriving through the image door. Before
        Task 12 the text-only seed was a scope boundary that cost nothing,
        because an image observation had nowhere to go.

        The CLIP_STATE announcement that goes out in the same call is a
        separate matter, unchanged: that frame is what gets PERSISTED to the
        store and put on the wire, and a fabricated KIND_TEXT there is
        exactly the regression resolve_startup_state's docstring warns about
        -- not just wrong in memory for one connection, but wrong on disk
        for every connection after it too."""
        agent = self.build(ready=True)
        png = b"\x89PNG-the-only-thing-on-the-clipboard"
        # Same double-read shape as
        # test_a_spurious_signal_at_connect_with_content_already_present_produces_no_send
        # just above: the seed read, and announce_clip_state's own read.
        agent.clipboard.queue_image_read(png)  # the seed read
        agent.clipboard.queue_image_read(png)  # the announce step's own read
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        self.become_ready_without_a_real_watcher(agent)

        self.assertEqual(
            agent._last_seen, (KIND_IMAGE, sha256_hex(png)),
            "the connect-time seed covers both kinds -- an image the PC already "
            "held is not a change the peer needs to hear about",
        )
        announced = [f for f in sent if f[0] == TYPE_CLIP_STATE]
        self.assertEqual(len(announced), 1, "the one-shot announcement must still go out")
        decoded = decode_clip_state(announced[0][1])
        self.assertEqual(
            decoded, (sha256_hex(png), decoded[1], KIND_IMAGE, None),
            "the announced kind must be the real one, not a fabricated KIND_TEXT",
        )

    def test_a_spurious_signal_at_connect_with_an_image_already_present_produces_no_send(self):
        """The other half of the seed's purpose, and the one that only became
        reachable with Task 12: the seed exists to stop the FIRST observation
        after connect from being read as a local change. Asserting the seed's
        value alone would leave that unproven for images -- the send path
        could still ignore it."""
        agent = self.build(ready=True)
        png = b"\x89PNG-already-on-the-pc"
        agent.clipboard.queue_image_read(png)  # the seed read
        agent.clipboard.queue_image_read(png)  # the announce step's own read
        self.become_ready_without_a_real_watcher(agent)

        agent.clipboard.queue_image_read(png)  # the spurious signal's read
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(
            sent, [],
            "a spurious signal right after connect must not resend an image the PC "
            "already held before anything synced",
        )

    def test_last_seen_holds_a_kind_and_a_hash_for_text_too(self):
        """Not text-by-value and images-by-hash: one rule. A second
        comparison branch is how the two sides drift."""
        agent = self.build(ready=True)
        agent.clipboard.queue_read(b"hello")
        agent.send = lambda t, p: None
        agent._local_change()
        self.assertEqual(agent._last_seen, (KIND_TEXT, sha256_hex(b"hello")))

    def test_the_same_bytes_under_a_different_kind_are_a_change(self):
        """Not text-by-value and images-by-hash: one rule. A second
        comparison branch is how the two sides drift. _last_seen is seeded
        directly under KIND_IMAGE here rather than reached through scripted
        reads of a real image: the identity rule under test lives in the
        comparison itself, not in how _last_seen came to hold that value,
        and the shortest route there keeps the test about one thing."""
        agent = self.build(ready=True)
        agent._last_seen = (KIND_IMAGE, sha256_hex(b"x"))
        agent.clipboard.queue_read(b"x")
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        agent._local_change()
        self.assertEqual(len(sent), 1, "kind is part of identity, not decoration")


class OrderRecordingClipboard:
    """write() snapshots agent._last_written at the exact moment it is
    called, so a test can pin the ORDER of arm-then-write, not merely that
    both happened. A version of _write_clip that armed the suppression
    AFTER writing would still make an occurrence-only assertion
    (clipboard.written == [...] and agent._last_written == ...) pass, since
    both would still be true by the time the test looks -- only checking
    what was armed AT WRITE TIME can tell the two orderings apart. Mirrors
    HandleFrameTests.swift's RecordingPasteboard.onWrite callback.

    _last_written specifically, so this pins the arm-then-write ORDER only
    for a TEXT write: since Task 10 an image write deliberately leaves
    _last_written None and arms _expect_reoffer and _last_seen instead (see
    _write_clip), so a test that drove an image through this double would
    record None and prove nothing. Every test here writes text."""

    def __init__(self):
        self.agent = None  # set after construction, once the real agent exists
        self.written = []
        self.armed_at_write_time = []

    def ready(self):
        return True

    def read(self):
        return None

    def write(self, kind, data):
        self.written.append((kind, data))
        self.armed_at_write_time.append(self.agent._last_written if self.agent else None)


class TestWriteClipDecodesTheWirePayload(unittest.TestCase):
    """_write_clip's contract changed with this task: its argument is now
    the wire-format [ts][text] payload encode_clip_payload produces, not
    bare text -- so it must decode before touching the clipboard, arm the
    suppression with the TEXT (not the ts-prefixed payload -- Contract 2 in
    HandleFrameTests.swift), and persist the PEER's timestamp, never now."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def test_arms_suppression_strictly_before_writing_to_the_clipboard(self):
        """The ordering contract, pinned as a SEQUENCE: reversed, the
        watcher's next poll would observe the new content before the
        suppression exists and bounce our own applied clip back out."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(1.0, b"hello"))

        self.assertEqual(clipboard.written, [(KIND_TEXT, b"hello")])
        self.assertEqual(
            clipboard.armed_at_write_time, [b"hello"],
            "the suppression must already be armed with the decoded text at "
            "the moment write() runs -- arm-after-write would leave this None",
        )

    def test_arms_suppression_with_plain_text_not_the_timestamp_prefixed_payload(self):
        """Distinct from the ordering test above: this pins WHAT is armed.
        _local_change's own clipboard.read() returns plain text, never a
        timestamp prefix -- arming with the raw wire payload instead would
        make the suppression never match a later poll's read, silently
        disabling echo suppression entirely."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        wire_payload = encode_clip_payload(1.0, b"hello")

        agent._write_clip(wire_payload)

        self.assertEqual(agent._last_written, b"hello")
        self.assertNotEqual(agent._last_written, wire_payload)

    def test_stores_the_peers_timestamp_not_now(self):
        """The assertion the persistent store's whole design exists to make
        possible: stamping applied content with `now` instead of the peer's
        ts would make it look freshly copied here and win the next
        reconciliation against the machine it actually came from. 424242.0
        is picked far from wall-clock time specifically so "came from the
        frame" and "came from now" cannot be confused by coincidence."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        peers_ts = 424242.0

        agent._write_clip(encode_clip_payload(peers_ts, b"peer's clip"))

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(stored, (sha256_hex(b"peer's clip"), peers_ts, KIND_TEXT, None),
                         "must store the PEER's ts, never now")

    def test_stores_the_literal_known_hash_for_a_pinned_vector(self):
        """Closes a gap a self-referential assertion cannot: comparing the
        stored hash against sha256_hex(b"hi") itself would still pass even
        if _write_clip hashed the wrong bytes (the ts-prefixed wire
        payload, or a str re-decoded/re-encoded differently), as long as it
        did so consistently with sha256_hex's own behavior. This pins the
        LITERAL, independently-verified digest instead -- see
        fixtures/hashes.json, read by both suites."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)

        agent._write_clip(encode_clip_payload(1.0, b"hi"))

        stored = load_clip_state(path=self.clip_state_path)
        self.assertEqual(
            stored,
            ("8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4", 1.0, KIND_TEXT, None),
        )

    def test_a_malformed_too_short_payload_touches_neither_suppression_nor_the_clipboard(self):
        """Mirrors Sources/clipwire/main.swift's handleFrame .clip case:
        `guard let decoded = try? ClipPayload.decode(...) else { return }`.
        A payload shorter than the 8-byte timestamp prefix cannot decode at
        all; _write_clip must swallow that quietly, exactly as an
        undecodable or empty clip frame already did before this task."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(b"\x00\x01\x02")  # 3 bytes: shorter than TIMESTAMP_BYTES

        self.assertEqual(clipboard.written, [])
        self.assertIsNone(agent._last_written)
        self.assertIsNone(load_clip_state(path=self.clip_state_path),
                          "a payload that never decoded must not be persisted")

    def test_a_decodable_but_empty_text_payload_touches_neither_suppression_nor_the_clipboard(self):
        """Distinct failure mode from the too-short case above: this payload
        DECODES fine (a valid 8-byte ts prefix, no text) but carries empty
        text. Mirrors
        HandleFrameTests.testDecodableButEmptyTextClipTouchesNeitherSuppressionNorThePasteboard."""
        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(5.0, b""))

        self.assertEqual(clipboard.written, [])
        self.assertIsNone(agent._last_written)

    def test_survives_a_real_disk_failure_when_persisting_clip_state(self):
        """Self-review: a local disk failure is not the peer's fault, and
        must not silently disable applying the clip. Forces a REAL save
        failure (a plain file occupying the directory the store needs to
        create), not a mock -- a mock would only prove a try/except exists
        syntactically, not that the clipboard write survives an actual
        failure. Mirrors HandleFrameTests.testAnnounceClipStateStillSendsWhenTheStoreCannotBeSaved."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable_path = os.path.join(blocker, "clip-state.json")
        # Confirm the setup actually forces a failure, or this test proves nothing.
        with self.assertRaises(OSError):
            save_clip_state(HASH_A, 1, KIND_TEXT, path=unsaveable_path)

        clipboard = OrderRecordingClipboard()
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=unsaveable_path)
        clipboard.agent = agent

        agent._write_clip(encode_clip_payload(1.0, b"still write this"))

        self.assertEqual(clipboard.written, [(KIND_TEXT, b"still write this")],
                         "a local disk failure must not prevent the clipboard write")
        self.assertEqual(agent._last_written, b"still write this",
                         "the suppression must still be armed despite the disk failure")


class TestModuleDefinitionOrder(unittest.TestCase):
    """The file ends with `if __name__ == "__main__": sys.exit(main(...))`.
    main() is only CALLED on that line, so any def/class placed AFTER it in
    the file has not executed yet at that point -- referencing it from
    inside Agent.clipboard_became_ready() would raise NameError the first
    time the clipboard becomes ready for real.

    No behavioural test catches a regression here: agent_under_test.py
    loads the module with exec_module under the name "clipwire_agent", so
    the guard's `if` is False there and the whole file runs regardless of
    definition order; the subprocess tests in test_mainloop.py do run the
    file as __main__, but all of them use
    CLIPWIRE_FAKE_CLIPBOARD=never-ready, so clipboard_became_ready() -- and
    therefore this defect -- is never reached. Pin the ordering directly
    against the source instead."""

    def test_watcher_definitions_precede_the_main_guard(self):
        source = AGENT.read_text()
        guard_index = source.index('if __name__ == "__main__"')
        for needle in (
            # Task 4: the three-way cap split and the new image-clip type,
            # all defined at the top of the file alongside MAX_PAYLOAD_BYTES.
            "MAX_TEXT_BYTES = 4194304",
            "MAX_IMAGE_BYTES = 4194304",
            "TYPE_IMAGE_CLIP = 0x03",
            "def make_watcher",
            # Final fix wave: the tie-break nudge a consumed image re-offer
            # is stored with, defined alongside the other Agent-scoped
            # constants above log().
            "REOFFER_TS_NUDGE_SECONDS = 0.001",
            "import ctypes",
            "import signal",
            "PR_SET_PDEATHSIG = 1",
            "def _load_libc",
            "_LIBC = _load_libc()",
            # Final fix wave: the prctl symbol, bound eagerly next to _LIBC
            # so no dlsym happens inside a forked child.
            "_PRCTL = _LIBC.prctl if _LIBC is not None else None",
            "def _pdeathsig_preexec",
            "import traceback",
            "def _handle_observer_error",
            "def _start_observer",
            "class GPasteWatcher",
            "class PollingWatcher",
            "def parse_gpaste_line",
            "SAFETY_NET_POLL_SECONDS = 30.0",
            "DEGRADED_POLL_SECONDS = 1.0",
            "TIMESTAMP_BYTES = 8",
            "class ClipPayloadError",
            "def encode_clip_payload",
            "def decode_clip_payload",
            # Task 5: the image-clip codec, defined immediately after the
            # text one it mirrors.
            "def encode_image_payload",
            "def decode_image_payload",
            "TYPE_CLIP_STATE = 0x02",
            # Task 6: the clip-state kind constants, defined alongside
            # SEND_MINE/WAIT_FOR_PEER/DO_NOTHING, just above ClipStateError.
            'KIND_TEXT = "text"',
            'KIND_IMAGE = "image"',
            "_KNOWN_KINDS = (KIND_TEXT, KIND_IMAGE)",
            "class ClipStateError",
            "def encode_clip_state",
            "def _is_sha256_hex",
            "def decode_clip_state",
            "def resolve_freshness",
            # v3.2: the provenance rule, defined immediately after the
            # freshness one it runs before. A top-level definition that
            # drifted below the guard would still import fine for every
            # test in this suite and NameError only in production, which
            # is the whole reason this check exists.
            "def resolve_provenance",
            'SEND_MINE = "sendMine"',
            'WAIT_FOR_PEER = "waitForPeer"',
            'DO_NOTHING = "doNothing"',
            "def _xdg_dir",
            "def clip_state_path",
            "def load_clip_state",
            "_clip_state_write_lock = threading.Lock()",
            "def save_clip_state",
            "def resolve_startup_state",
            "def sha256_hex",
            "def resolve_current_clip_state",
            "def announce_clip_state",
            "SKEW_WARN_SECONDS = 5.0",
            "def skew_log_line",
            # Task 7: the one canonical clipboard read, and its longer
            # image timeout, defined alongside SUBPROCESS_TIMEOUT and
            # WaylandClipboard.
            "IMAGE_SUBPROCESS_TIMEOUT = 10",
            "def choose_kind",
            # Task 7, Fix round 1: the duration-logging gate's two
            # thresholds, defined alongside IMAGE_SUBPROCESS_TIMEOUT.
            "SLOW_READ_SECONDS = 1.0",
            "SLOW_IMAGE_READ_SECONDS = 3.0",
        ):
            with self.subTest(needle=needle):
                self.assertLess(
                    source.index(needle), guard_index,
                    "%r must be defined before the __main__ guard, or it "
                    "never executes when the agent is run for real" % needle,
                )


if __name__ == "__main__":
    unittest.main()
