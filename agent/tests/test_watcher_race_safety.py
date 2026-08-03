# agent/tests/test_watcher_race_safety.py
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
    JOIN_TIMEOUT,
    QueueClipboard,
)
class RacyClipboard:
    """A clipboard double that simulates a write landing on the main thread
    WHILE a read() is in flight -- deterministically, via a side effect
    inside read() itself, rather than by timing two real threads. This is
    the exact shape of the real race: a wl-paste round trip can take up to
    SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT = 13 seconds on an image
    clipboard, and a new frame can arrive and be
    written locally (on the main thread) at any point during that
    window."""

    def __init__(self, agent, value_read, interleaved_write):
        self._agent = agent
        self._value_read = value_read
        self._interleaved_write = interleaved_write

    def read(self):
        self._agent._write_clip(self._interleaved_write)  # lands mid-flight
        # what the fork had already captured -- every test constructing one
        # of these passes text, so KIND_TEXT unconditionally.
        return (KIND_TEXT, self._value_read)

    def write(self, kind, data):
        pass

    def ready(self):
        return True


class GatedReadClipboard:
    """read() blocks on a gate the test controls, so one _local_change can be
    held inside its clipboard round trip while a second one runs on another
    thread. That is not hypothetical: since the safety-net poll was added, the
    gdbus pump thread and the poll thread BOTH call _local_change, and a
    clipboard read can take up to SUBPROCESS_TIMEOUT +
    IMAGE_SUBPROCESS_TIMEOUT = 13s on an image clipboard."""

    def __init__(self, text, gate):
        self._text = text
        self._gate = gate
        self._lock = threading.Lock()
        self.reads = 0

    def ready(self):
        return True

    def read(self):
        with self._lock:
            self.reads += 1
        self._gate.wait(JOIN_TIMEOUT)
        return (KIND_TEXT, self._text)

    def write(self, kind, data):
        pass


class TestLocalChangeRaceSafety(unittest.TestCase):
    """_write_clip() always runs on the main thread (driven by run()'s
    single-threaded loop); _local_change() only ever runs on watcher
    threads -- plural since the safety-net poll was added, which is what the
    last test in this class is about. clipboard.read() -- a wl-paste round
    trips -- can take up to SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT =
    13s on an image clipboard, so a new _write_clip() can
    land on the main thread at any point during that window, not just cleanly
    before or after it."""

    def setUp(self):
        # _write_clip persists through save_clip_state, which touches the
        # real production path when clip_state_path is None -- see
        # TestEchoBookkeeping.setUp.
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clip_state_path = os.path.join(self._tmp.name, "clip-state.json")

    def test_a_write_that_lands_during_the_read_is_not_echoed(self):
        """Sequence pinned here:
        1. Mac sends clip A -> _write_clip(A) arms the suppression for A.
        2. The watcher's read() begins; the underlying fork has already
           captured A, but control has not yet returned to Python.
        3. Before it returns, Mac sends clip B -> _write_clip(B) re-arms
           the suppression for B, on the main thread.
        4. read() finally returns A -- stale by the time _local_change
           gets to compare it against whatever is now armed.

        A version that snapshots _last_written only *after* read() returns
        treats A as a genuine change relative to the now-current expected
        value B, and sends A back to the Mac as if the user had copied it
        -- our own echo. It must send nothing, and must leave B's
        suppression armed so B's own echo (or a later genuine change) is
        still judged correctly."""
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=QueueClipboard(),
                      clip_state_path=self.clip_state_path)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))

        agent._write_clip(encode_clip_payload(1.0, b"A"))
        agent.clipboard = RacyClipboard(
            agent, value_read=b"A", interleaved_write=encode_clip_payload(2.0, b"B"))

        agent._local_change()

        self.assertEqual(
            sent, [], "a write observed mid-read must not be echoed back as genuine"
        )
        self.assertEqual(
            agent._last_written, b"B",
            "the newer write's suppression must stay armed for its own echo",
        )

    def test_two_watcher_threads_observing_one_copy_send_a_single_clip(self):
        """Before the safety net there was exactly ONE caller of
        _local_change -- the gdbus pump or the fallback poll, never both. Now
        both threads are live at once, and this method structurally cannot
        dedupe against a sibling: it snapshots _last_seen BEFORE its clipboard
        read and only advances it AFTER the send, so two observations of one
        copy that overlap inside that window both find `stale` false, both find
        the text different from a stale `last_seen`, and both send a TYPE_CLIP
        frame -- two frames for one copy, with different observed_at values and
        two competing clip-state writes behind them.

        Serialization must BLOCK, not skip: PollingWatcher's `previous` has
        already advanced past the change it is reporting, so a skipped
        observation is a clip lost until the next change rather than deferred.
        Hence the assertion is one send, not zero."""
        gate = threading.Event()
        self.addCleanup(gate.set)  # never leave the two threads parked
        clipboard = GatedReadClipboard(b"copied once", gate)
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=self.clip_state_path)
        sent = []
        send_lock = threading.Lock()

        def record(frame_type, payload):
            with send_lock:
                sent.append((frame_type, payload))

        agent.send = record

        pump = threading.Thread(target=agent._local_change)
        poll = threading.Thread(target=agent._local_change)
        pump.start()
        deadline = time.monotonic() + JOIN_TIMEOUT
        while clipboard.reads < 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(clipboard.reads, 1, "the first observation must be in flight")

        poll.start()
        # Bounded, and only ever consumed in full when the two observations ARE
        # serialized: unserialized, the second thread reaches its own read
        # within microseconds and this returns immediately.
        deadline = time.monotonic() + 0.02
        while clipboard.reads < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        reads_while_first_in_flight = clipboard.reads

        gate.set()
        pump.join(timeout=JOIN_TIMEOUT)
        poll.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(
            reads_while_first_in_flight, 1,
            "the second observation must not enter its own clipboard read while "
            "the first is still in flight",
        )
        self.assertEqual(
            len(sent), 1,
            "one copy must produce exactly one clip frame, whichever thread "
            "observes it; got %r" % (sent,),
        )
        self.assertEqual(decode_clip_payload(sent[0][1])[1], b"copied once")


