# agent/tests/test_mainloop.py
import io
import os
import select
import subprocess
import sys
import threading
import time
import unittest
import pathlib

from agent_under_test import Agent, TYPE_HELLO, decode_frame, encode_frame

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
        frame = encode_frame(TYPE_HELLO, b'{"protocol":1}')
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

    def write(self, data):
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
        agent = Agent(stdin=stdin, stdout=io.BytesIO(), clipboard=clipboard)

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


if __name__ == "__main__":
    unittest.main()
