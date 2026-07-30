# agent/tests/test_clipboard.py
import os
import subprocess
import unittest
from unittest import mock

from agent_under_test import (
    SUBPROCESS_TIMEOUT,
    WaylandClipboard,
    clipboard_env,
    runtime_dir,
    wayland_socket_path,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to swap out log() for an in-memory capture,
# the same pattern test_mainloop.py uses.
import clipwire_agent


class TestEnvironment(unittest.TestCase):
    def test_uses_xdg_runtime_dir_when_present(self):
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/4242")
        self.assertEqual(runtime_dir(env), "/run/user/4242")

    def test_falls_back_to_uid_when_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        self.assertEqual(runtime_dir(env), "/run/user/%d" % os.getuid())

    def test_builds_wayland_display_and_bus_address(self):
        env = clipboard_env({"XDG_RUNTIME_DIR": "/run/user/4242"})
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/4242/bus"
        )
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")

    def test_socket_path(self):
        self.assertEqual(
            wayland_socket_path({"XDG_RUNTIME_DIR": "/run/user/4242"}),
            "/run/user/4242/wayland-0",
        )


class TestWriteSubprocessFailures(unittest.TestCase):
    """write() must survive wl-copy misbehaving, both at spawn time and at
    close time -- both are normal desktop conditions (facts 2/3 in the task
    brief), not agent bugs. Every case here patches subprocess.Popen, so no
    real process -- and therefore no real hang -- is ever possible."""

    def setUp(self):
        self.log_lines = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def test_missing_binary_returns_without_touching_process(self):
        """FileNotFoundError at Popen() must be logged and must return
        immediately -- never falling through to the write/close block below,
        which references a `process` that in this branch was never bound."""
        with mock.patch("subprocess.Popen", side_effect=FileNotFoundError()):
            result = WaylandClipboard().write(b"hello")
        self.assertIsNone(result)
        self.assertTrue(
            any("not installed" in line for line in self.log_lines),
            "must log when wl-copy is missing: %r" % self.log_lines,
        )

    def test_broken_pipe_on_close_is_logged_not_raised(self):
        """close() flushes a buffered writer. If wl-copy died right after
        spawn, that flush can raise BrokenPipeError -- and it must be
        handled at the point it happens, not left to crash the caller during
        a clipboard write, which is the operation the user is watching."""
        fake_process = mock.Mock()
        fake_process.stdin.close.side_effect = BrokenPipeError(
            "simulated: wl-copy died before the write was flushed"
        )
        with mock.patch("subprocess.Popen", return_value=fake_process):
            WaylandClipboard().write(b"hello")  # must not raise
        self.assertTrue(
            any("wl-copy" in line for line in self.log_lines),
            "a close-time BrokenPipeError must be logged: %r" % self.log_lines,
        )

    def test_permission_denied_at_spawn_is_logged_not_raised(self):
        """wl-copy present but not executable (wrong permissions, an AppArmor
        denial) makes Popen raise PermissionError -- an OSError, but not a
        FileNotFoundError. The spawn guard must catch OSError generally, not
        just the missing-binary case, or this escapes write() uncaught."""
        with mock.patch("subprocess.Popen", side_effect=PermissionError("denied")):
            result = WaylandClipboard().write(b"hello")  # must not raise
        self.assertIsNone(result)
        self.assertTrue(
            any("wl-copy" in line for line in self.log_lines),
            "a PermissionError at spawn must be logged: %r" % self.log_lines,
        )


class TestReadSubprocessBehavior(unittest.TestCase):
    """read()'s branching was previously verified only by reading the line.
    Every case here patches subprocess.run, so no real process -- and
    therefore no real hang -- is ever possible."""

    def setUp(self):
        self.log_lines = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def _completed(self, returncode, stdout=b""):
        return subprocess.CompletedProcess(
            args=["wl-paste"], returncode=returncode, stdout=stdout, stderr=b""
        )

    def test_exit_zero_with_content_is_returned(self):
        with mock.patch("subprocess.run", return_value=self._completed(0, b"clip contents")):
            self.assertEqual(WaylandClipboard().read(), b"clip contents")

    def test_nonzero_exit_is_none_not_an_exception(self):
        """The brief's own framing: a non-zero exit means an empty or
        non-text selection, not an error -- and it must be treated as such
        even if wl-paste wrote something to stdout before failing. Non-empty
        stdout here is deliberate: it is what makes this test sensitive to a
        broken returncode check, rather than passing by coincidence."""
        with mock.patch("subprocess.run", return_value=self._completed(1, b"stale data")):
            self.assertIsNone(WaylandClipboard().read())

    def test_exit_zero_with_empty_stdout_is_none(self):
        with mock.patch("subprocess.run", return_value=self._completed(0, b"")):
            self.assertIsNone(WaylandClipboard().read())

    def test_timeout_returns_none_and_logs(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("wl-paste", SUBPROCESS_TIMEOUT)
        ):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("wl-paste" in line for line in self.log_lines),
            "a timeout must be logged: %r" % self.log_lines,
        )

    def test_permission_denied_at_spawn_returns_none_and_logs(self):
        """wl-paste present but not executable raises PermissionError, an
        OSError that is not a FileNotFoundError -- the same latent escape
        as write()'s spawn guard, just not yet reachable since nothing
        calls read() until a later task wires up the watcher."""
        with mock.patch("subprocess.run", side_effect=PermissionError("denied")):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("wl-paste" in line for line in self.log_lines),
            "a PermissionError at spawn must be logged: %r" % self.log_lines,
        )


if __name__ == "__main__":
    unittest.main()
