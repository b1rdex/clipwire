# agent/tests/test_mainloop.py
import io
import os
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import pathlib
from unittest import mock

from agent_under_test import (
    Agent,
    KIND_TEXT,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
    decode_clip_payload,
    decode_frame,
    encode_clip_state,
    encode_frame,
    load_clip_state,
    save_clip_state,
    sha256_hex,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to reach a module-level tuning constant
# (CLIPBOARD_RECHECK_SECONDS) for one test below.
import clipwire_agent

AGENT = pathlib.Path(__file__).resolve().parents[1] / "clipwire-agent.py"


def _read_exact(pipe, size, timeout):
    """Read exactly `size` bytes from a pipe, bounded by `timeout` seconds total.

    process.stdout.read(n) blocks until n bytes arrive or EOF. If a future
    regression ever stopped the agent from writing hello promptly, that bare
    read() would hang this test -- and the whole suite -- instead of failing
    it. Bound it the same way the agent bounds its own stdin wait: select().
    """
    fd = pipe.fileno()
    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AssertionError(
                "timed out after %ss waiting for %d bytes (got %d)"
                % (timeout, size, len(data))
            )
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            raise AssertionError(
                "timed out after %ss waiting for %d bytes (got %d)"
                % (timeout, size, len(data))
            )
        chunk = os.read(fd, size - len(data))
        if not chunk:
            raise AssertionError(
                "peer closed stdout after %d of %d bytes" % (len(data), size)
            )
        data += chunk
    return bytes(data)


class TestMainLoop(unittest.TestCase):
    def test_agent_exits_when_stdin_closes_before_clipboard_is_ready(self):
        """The pre-login window: no Wayland session, peer hangs up."""
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        process.stdin.close()
        self.assertEqual(process.wait(timeout=10), 0,
                         "a sleeping agent must still notice stdin EOF")

    def test_agent_sends_hello_immediately_even_when_not_ready(self):
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        header = _read_exact(process.stdout, 5, timeout=10)
        length = int.from_bytes(header[:4], "big")
        payload = _read_exact(process.stdout, length, timeout=10)
        buffer = bytearray(header + payload)
        frame_type, _ = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_HELLO)
        process.stdin.close()
        process.wait(timeout=10)

    def test_agent_exits_on_protocol_mismatch(self):
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        process.stdin.write(encode_frame(TYPE_HELLO, b'{"protocol":999}'))
        process.stdin.flush()
        self.assertNotEqual(process.wait(timeout=10), 0,
                            "a version mismatch must fail loudly, not flap silently")

    def test_agent_exits_cleanly_on_non_object_hello_payload(self):
        """A syntactically valid but non-object hello payload (a bare JSON
        number, null, or array) must not fall through to peer.get(...) on a
        non-dict -- that raises AttributeError, which main() does not catch,
        so it would exit 1 with a raw traceback instead of a clean protocol
        error. Exit code 2 is the discriminator: it is only reachable through
        main()'s `except FrameError`, so it proves the intended path fired."""
        for payload in (b"123", b"null", b"[1,2]"):
            with self.subTest(payload=payload):
                env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
                process = subprocess.Popen(
                    [sys.executable, str(AGENT)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    env=env,
                )
                self.addCleanup(process.kill)
                self.addCleanup(process.stdout.close)
                self.addCleanup(process.stdin.close)
                process.stdin.write(encode_frame(TYPE_HELLO, payload))
                process.stdin.flush()
                self.assertEqual(
                    process.wait(timeout=10), 2,
                    "a non-object hello payload must fail via FrameError (exit 2), "
                    "not an uncaught AttributeError",
                )

    def test_agent_exits_cleanly_on_an_oversized_integer_timestamp_in_a_clip_state_frame(self):
        """The real deployed agent's twin of test_freshness.py's
        test_decode_rejects_an_oversized_integer_timestamp and
        test_watcher.py's TestIncomingClipState.
        test_a_malformed_clip_state_raises_a_clip_state_error_not_a_crash.

        json.loads parses a 400-digit integer ts as arbitrary-precision
        int; math.isfinite's int-to-float conversion then raises a bare
        OverflowError, which (before this task's fix) is not a FrameError
        and so is not caught by main()'s `except FrameError` -- an
        uncaught exception exits 1 with a raw traceback on stderr. Exit
        code 2 is the discriminator, exactly as
        test_agent_exits_cleanly_on_non_object_hello_payload's own comment
        explains: it is only reachable through main()'s `except
        FrameError`, so it proves decode_clip_state raised the intended
        ClipStateError rather than crashing."""
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        # 64 lowercase hex: decode_clip_state rejects any other shape, and
        # this case is about the oversized TS, so the hash must be valid or
        # the test would pass on the wrong rejection.
        oversized_ts_payload = ('{"sha256": "%s", "ts": 1' % ("aa" * 32)).encode() + b"0" * 400 + b"}"
        process.stdin.write(encode_frame(TYPE_CLIP_STATE, oversized_ts_payload))
        process.stdin.flush()
        self.assertEqual(
            process.wait(timeout=10), 2,
            "an oversized-integer ts in a clip-state frame must fail via "
            "ClipStateError/FrameError (exit 2), not an uncaught OverflowError",
        )

    def test_partial_hello_frame_split_across_two_writes_is_reassembled(self):
        """A frame split across two stdin reads (a slow or chunked SSH
        channel) must still be assembled into one frame. Sends a
        protocol-matching hello, so a correctly reassembling agent stays
        alive and exits 0 on a later EOF; an agent that discarded the partial
        buffer between reads would instead misparse the raw tail of the
        frame as a bogus header and exit non-zero via FrameError."""
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        self.addCleanup(process.kill)
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.stdin.close)
        # Built from PROTOCOL_VERSION rather than a hardcoded literal: a
        # hardcoded "1" would silently become a MISMATCHED hello once the
        # agent's own version bumps, flipping this test's outcome for a
        # reason unrelated to what it actually checks (reassembly).
        frame = encode_frame(TYPE_HELLO, ('{"protocol":%d}' % PROTOCOL_VERSION).encode())
        split = len(frame) // 2
        process.stdin.write(frame[:split])
        process.stdin.flush()
        time.sleep(0.1)  # let the agent drain the first half before the rest arrives
        process.stdin.write(frame[split:])
        process.stdin.flush()
        time.sleep(0.1)  # let a correct agent finish processing before EOF
        process.stdin.close()
        self.assertEqual(
            process.wait(timeout=10), 0,
            "a hello frame split across two stdin reads must still be reassembled and accepted",
        )


class ScriptedClipboard:
    """Feeds a fixed ready()/not-ready script to Agent.run(), then closes the
    stdin pipe's write end so run() exits through the ordinary EOF path once
    the script is exhausted -- no threads, no real Wayland session needed."""

    def __init__(self, script, write_fd):
        self._script = list(script)
        self._write_fd = write_fd
        self.calls = 0

    def ready(self):
        value = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if self.calls > len(self._script):
            self.close_write_end()
        return value

    def close_write_end(self):
        """Idempotent by design: the scripted end-of-run close (above) and
        the test's own addCleanup both call this. Closing the same fd
        *number* twice, from two places, is exactly how an unrelated fd
        that the OS recycled in between gets closed by accident -- so this
        tracks whether it already ran instead of relying on a caller to
        know."""
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    def read(self):
        return None

    def write(self, kind, data):
        pass


class TestClipboardTransitionLogging(unittest.TestCase):
    """Agent.run() drives clipboard_became_ready()/clipboard_lost() straight
    off clipboard.ready(). Exercise that wiring directly (no subprocess, no
    real Wayland session) and check each transition logs exactly once, not
    once per tick."""

    def test_appear_and_disappear_are_each_logged_once(self):
        original_interval = clipwire_agent.CLIPBOARD_RECHECK_SECONDS
        clipwire_agent.CLIPBOARD_RECHECK_SECONDS = 0.01
        self.addCleanup(
            setattr, clipwire_agent, "CLIPBOARD_RECHECK_SECONDS", original_interval
        )

        # This test's script drives the clipboard into READY, which now
        # (since the clip-state wiring landed) runs clipboard_became_ready's
        # announce step -- save_clip_state/load_clip_state touch the real
        # production path (~/.local/state/clipwire/clip-state.json) when
        # clip_state_path is None. Missed when that wiring first landed;
        # caught here while touching this same file for a related fix.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clip_state_path = os.path.join(tmp.name, "clip-state.json")

        # run() calls the module-level log() by its bare name, resolved from
        # clipwire_agent's globals at call time -- so replacing the module
        # attribute captures every call, in whichever thread makes it,
        # without touching the process-wide sys.stderr.
        original_log = clipwire_agent.log
        log_lines = []
        clipwire_agent.log = log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, "rb", buffering=0)
        self.addCleanup(stdin.close)

        # False, False, True, True, True, False, False: a multi-tick "ready"
        # run followed by a multi-tick "not ready" run, so a naive
        # log-on-every-tick implementation would fail this immediately.
        clipboard = ScriptedClipboard(
            [False, False, True, True, True, False, False], write_fd
        )
        self.addCleanup(clipboard.close_write_end)
        agent = Agent(stdin=stdin, stdout=io.BytesIO(), clipboard=clipboard,
                      clip_state_path=clip_state_path)

        # This calls run() directly, not through a subprocess -- nothing
        # external bounds it the way process.wait(timeout=...) bounds every
        # test in TestMainLoop. A regression that dropped run()'s select()
        # timeout would make clipboard.ready() (and the scripted
        # close_write_end() that ends this test) unreachable: the loop would
        # block in select() forever, stdin would never see EOF, and a bare
        # `agent.run()` call here would hang this test -- and the whole
        # suite -- rather than fail it. Run it in a daemon thread and bound
        # the wait with join(timeout=...) so that regression fails fast
        # instead. (The thread is left running, blocked, if this ever times
        # out; being a daemon thread, it does not block interpreter exit.)
        outcome = {}

        def _call_run():
            try:
                outcome["result"] = agent.run()
            except BaseException as error:  # pragma: no cover - surfaced below
                outcome["error"] = error

        runner = threading.Thread(target=_call_run, daemon=True)
        runner.start()
        runner.join(timeout=5)
        self.assertFalse(
            runner.is_alive(),
            "run() did not return within 5s -- a regression likely dropped "
            "the select() timeout, so the loop never reaches "
            "clipboard.ready() and never notices stdin EOF",
        )
        if "error" in outcome:
            raise outcome["error"]

        self.assertEqual(
            outcome.get("result"), 0,
            "run() must exit cleanly once the script ends and stdin closes",
        )
        self.assertEqual(
            log_lines.count("clipboard is available"), 1,
            "becoming ready must log once, not once per tick: %r" % log_lines,
        )
        self.assertEqual(
            log_lines.count("clipboard went away, waiting for it to come back"), 1,
            "losing the clipboard must log once, not once per tick: %r" % log_lines,
        )
        self.assertEqual(agent.phase, clipwire_agent.PHASE_PENDING)


class _NoOpWatcher:
    """Same shape as test_lifecycle.py's/test_watcher.py's own doubles of
    this name: avoids a real gdbus probe or a leaked PollingWatcher thread
    for tests that do not care about the watcher itself."""

    def start(self, on_change):
        pass

    def stop(self):
        pass


class ScriptedClipboardWithContent:
    """Like ScriptedClipboard above, but read() returns real content and
    write() records what was written, instead of both being no-ops --
    needed to reproduce Finding 1's exact scenario: the PC's actual
    clipboard already holds new content (the user copied it locally while
    disconnected), predating this connection, while the on-disk store
    still describes older, stale content (nothing observed the change --
    no agent process was running to watch it)."""

    def __init__(self, script, write_fd, read_value):
        self._script = list(script)
        self._write_fd = write_fd
        self._read_value = read_value
        self.calls = 0
        self.written = []

    def ready(self):
        value = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if self.calls > len(self._script):
            self.close_write_end()
        return value

    def close_write_end(self):
        if self._write_fd is not None:
            os.close(self._write_fd)
            self._write_fd = None

    def read(self):
        # Kind-aware since Task 7 -- always KIND_TEXT here, the only kind
        # this file's own TestClipStateOrderingAcrossRealDispatch constructs
        # one of these with.
        return (KIND_TEXT, self._read_value) if self._read_value else None

    def write(self, kind, data):
        self.written.append((kind, data))


class TestClipStateOrderingAcrossRealDispatch(unittest.TestCase):
    """Fix round 1, Finding 1 (Critical): clipboard_became_ready is not the
    ordering-equivalent of Swift's matched-hello. run()'s real loop drains
    and dispatches every complete frame on stdin BEFORE it ever checks
    clipboard.ready() in that same iteration -- so a peer's TYPE_CLIP_STATE
    frame can reach _on_clip_state while this agent is still PHASE_PENDING,
    before clipboard_became_ready has ever reconciled the (possibly stale)
    on-disk store against what the clipboard actually holds right now.

    Reproduced end to end here, driving the REAL run() loop (not by calling
    _on_clip_state/clipboard_became_ready by hand in a chosen order, which
    is the inverse of production and the reason this defect went unnoticed):

    1. Both sides were last synced on "A"; the on-disk store holds
       (sha256("A"), t_A).
    2. This agent process starts fresh (as it always does -- sshd spawns
       one per SSH connection). The user copied "B" locally while
       disconnected; nothing observed it, so the store still says "A".
    3. The peer's TYPE_CLIP_STATE frame -- announcing its own last-known
       state, (sha256("A"), t_A), since the peer hasn't changed either --
       arrives and is dispatched before the clipboard is ever checked for
       readiness in run()'s loop.
    4. The clipboard becomes ready on a later loop iteration.

    Before the fix: step 3's _on_clip_state loads the STALE store, sees
    hashes match the peer's announcement, resolves DO_NOTHING, and never
    reconsiders. Step 4's own announce step reconciles the store correctly
    (B) -- but only AFTER the decision not to send B was already made and
    discarded. Neither side ever transmits B: the peer resolves
    waitForPeer against the PC's newly-correct announcement, and the PC
    already said nothing. B is silently lost -- v1's exact defect.

    After the fix: the peer's announcement is stashed while
    _clip_state_sent is still False, and resolved immediately after step
    4's reconciliation -- by which point the store correctly says B, so
    resolve_freshness returns SEND_MINE and B goes out.
    """

    def test_peer_clip_state_arriving_before_readiness_is_resolved_correctly_once_ready(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        clip_state_path = os.path.join(tmp.name, "clip-state.json")

        hash_a, ts_a = sha256_hex(b"A"), 100.0
        save_clip_state(hash_a, ts_a, KIND_TEXT, path=clip_state_path)

        original_interval = clipwire_agent.CLIPBOARD_RECHECK_SECONDS
        clipwire_agent.CLIPBOARD_RECHECK_SECONDS = 0.01
        self.addCleanup(setattr, clipwire_agent, "CLIPBOARD_RECHECK_SECONDS", original_interval)

        read_fd, write_fd = os.pipe()
        stdin = os.fdopen(read_fd, "rb", buffering=0)
        self.addCleanup(stdin.close)

        # The peer's stale-but-still-matching-each-other announcement,
        # already sitting in the pipe before run() ever starts -- so it is
        # read and dispatched on the very first loop iteration, well before
        # the clipboard is ever checked. False on the first ready() call
        # guarantees _on_clip_state runs while still PHASE_PENDING.
        peer_announcement = encode_frame(TYPE_CLIP_STATE, encode_clip_state(hash_a, ts_a, KIND_TEXT))
        os.write(write_fd, peer_announcement)

        clipboard = ScriptedClipboardWithContent(
            script=[False, True], write_fd=write_fd, read_value=b"B",
        )
        self.addCleanup(clipboard.close_write_end)
        stdout = io.BytesIO()
        agent = Agent(stdin=stdin, stdout=stdout, clipboard=clipboard,
                      clip_state_path=clip_state_path)

        outcome = {}

        def _call_run():
            try:
                with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
                    outcome["result"] = agent.run()
            except BaseException as error:  # pragma: no cover - surfaced below
                outcome["error"] = error

        runner = threading.Thread(target=_call_run, daemon=True)
        runner.start()
        runner.join(timeout=5)
        self.assertFalse(
            runner.is_alive(),
            "run() did not return within 5s -- likely blocked in select() "
            "forever, the same regression guarded against elsewhere in "
            "this file",
        )
        if "error" in outcome:
            raise outcome["error"]
        self.assertEqual(outcome.get("result"), 0,
                         "run() must exit cleanly once the script ends and stdin closes")

        frames = []
        buffer = bytearray(stdout.getvalue())
        while True:
            frame = decode_frame(buffer)
            if frame is None:
                break
            frames.append(frame)

        clip_frames = [f for f in frames if f[0] == TYPE_CLIP]
        self.assertEqual(
            len(clip_frames), 1,
            "expected exactly one outgoing clip frame carrying B; got frame "
            "types %r -- B was silently lost, the exact defect this fix "
            "closes" % [f[0] for f in frames],
        )
        ts, text = decode_clip_payload(clip_frames[0][1])
        self.assertEqual(text, b"B", "must send B, not stale content or nothing at all")
        self.assertGreater(
            ts, ts_a,
            "must carry the reconciled (fresh) timestamp, not the stale t_A "
            "the peer's announcement described",
        )

        # The store itself must also have been correctly reconciled to B,
        # not left holding stale A -- announce_clip_state's own job,
        # unaffected by this fix, but worth confirming end to end here too.
        stored = load_clip_state(path=clip_state_path)
        self.assertEqual(stored[0], sha256_hex(b"B"))


if __name__ == "__main__":
    unittest.main()
