# agent/tests/test_clipboard.py
import json
import os
import pathlib
import subprocess
import unittest
from unittest import mock

from agent_under_test import (
    IMAGE_SUBPROCESS_TIMEOUT,
    KIND_IMAGE,
    KIND_TEXT,
    SUBPROCESS_TIMEOUT,
    WaylandClipboard,
    choose_kind,
    clipboard_env,
    runtime_dir,
    wayland_socket_path,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to swap out log() for an in-memory capture,
# the same pattern test_mainloop.py uses.
import clipwire_agent

# Note the path shape: the existing FIXTURES in test_freshness.py/
# test_fixtures.py points at a FILE, not a directory, so do not
# os.path.join onto it. tests -> agent -> repo root -> fixtures/clipkind.json.
CLIPKIND_FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "clipkind.json"


def _completed(returncode=0, stdout=b""):
    """A subprocess.CompletedProcess shaped like a real wl-paste result.
    Module-level so every class below shares one definition -- TestReadSubprocessBehavior
    used to keep its own copy as a bound method; folded into this one now that
    TestCanonicalRead needs the same shape too."""
    return subprocess.CompletedProcess(args=["wl-paste"], returncode=returncode, stdout=stdout, stderr=b"")


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
            result = WaylandClipboard().write(KIND_TEXT, b"hello")
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
            WaylandClipboard().write(KIND_TEXT, b"hello")  # must not raise
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
            result = WaylandClipboard().write(KIND_TEXT, b"hello")  # must not raise
        self.assertIsNone(result)
        self.assertTrue(
            any("wl-copy" in line for line in self.log_lines),
            "a PermissionError at spawn must be logged: %r" % self.log_lines,
        )


class TestWriteKindSelectsType(unittest.TestCase):
    """write(kind, data) is new this task: kind picks which --type wl-copy
    is spawned with. Every case here patches subprocess.Popen, so no real
    process is ever possible."""

    def test_text_kind_uses_the_text_mime_type(self):
        fake_process = mock.Mock()
        with mock.patch("subprocess.Popen", return_value=fake_process) as popen:
            WaylandClipboard().write(KIND_TEXT, b"hello")
        self.assertIn("text/plain;charset=utf-8", popen.call_args.args[0])

    def test_image_kind_uses_the_png_mime_type(self):
        fake_process = mock.Mock()
        with mock.patch("subprocess.Popen", return_value=fake_process) as popen:
            WaylandClipboard().write(KIND_IMAGE, b"\x89PNG...")
        self.assertIn("image/png", popen.call_args.args[0])
        self.assertNotIn("text/plain;charset=utf-8", popen.call_args.args[0])


class TestReadSubprocessBehavior(unittest.TestCase):
    """read()'s branching -- two wl-paste invocations, --list-types then a
    body fetch, sharing one failure path (_run_wl_paste) -- verified
    against mocked subprocess.run, so no real process, and therefore no
    real hang, is ever possible.

    A single side_effect exception/return_value (as opposed to a list)
    applies to EVERY subprocess.run call a test triggers. Used deliberately
    below wherever a case is about _run_wl_paste's shared failure handling,
    which both the --list-types and the body-fetch call go through
    identically, so triggering it via the first (--list-types) call is the
    simplest setup for the same method either way -- not a coincidence,
    since read() never reaches a second call once the first has failed.
    Cases about the BODY fetch specifically (its own returncode/empty-stdout
    handling, and the two different timeouts) use an explicit two-item
    side_effect list instead, so a real type list is offered and choose_kind
    actually has to pick a kind before the body read is reached."""

    def setUp(self):
        self.log_lines = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def test_exit_zero_with_text_content_is_returned(self):
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b"clip contents"),
        ]):
            self.assertEqual(WaylandClipboard().read(), (KIND_TEXT, b"clip contents"))

    def test_body_nonzero_exit_is_none_not_an_exception(self):
        """The brief's own framing: a non-zero exit means an empty or
        non-text selection, not an error -- and it must be treated as such
        even if wl-paste wrote something to stdout before failing. Non-empty
        stdout here is deliberate: it is what makes this test sensitive to a
        broken returncode check, rather than passing by coincidence."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(returncode=1, stdout=b"stale data"),
        ]):
            self.assertIsNone(WaylandClipboard().read())

    def test_body_empty_stdout_is_none(self):
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b""),
        ]):
            self.assertIsNone(WaylandClipboard().read())

    def test_list_types_nonzero_exit_is_none_and_asks_for_no_body(self):
        """A non-zero --list-types exit means the same 'empty or unreadable
        selection' as a non-zero body exit -- and, unlike the body case,
        there is nothing left for choose_kind to pick from, so read() must
        not go on to ask for a body at all."""
        with mock.patch("subprocess.run", return_value=_completed(returncode=1)) as run:
            self.assertIsNone(WaylandClipboard().read())
        self.assertEqual(
            run.call_count, 1, "a failed --list-types must not be followed by a body read"
        )

    def test_list_types_offering_nothing_useful_is_none_and_asks_for_no_body(self):
        """choose_kind's own 'neither' row (fixtures/clipkind.json), at the
        WaylandClipboard level: TARGETS/TIMESTAMP are real answers wl-paste
        gives, not a failure -- exit zero -- but choose_kind picks neither,
        so there is nothing to ask a body read for."""
        with mock.patch(
            "subprocess.run", return_value=_completed(stdout=b"TARGETS\nTIMESTAMP\n")
        ) as run:
            self.assertIsNone(WaylandClipboard().read())
        self.assertEqual(
            run.call_count, 1, "nothing offered a kind this agent syncs -- no body read"
        )

    def test_image_body_is_returned_with_the_longer_timeout(self):
        """Text reads have already been seen timing out at SUBPROCESS_TIMEOUT
        in production, and a 4 MiB body through a pipe needs more -- so the
        image body fetch specifically must use IMAGE_SUBPROCESS_TIMEOUT, not
        the text bound."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/tiff\nimage/png\n"),
            _completed(stdout=b"\x89PNG-body"),
        ]) as run:
            self.assertEqual(WaylandClipboard().read(), (KIND_IMAGE, b"\x89PNG-body"))
        self.assertEqual(run.call_args.kwargs["timeout"], IMAGE_SUBPROCESS_TIMEOUT)

    def test_text_body_read_uses_the_standard_timeout(self):
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b"hello"),
        ]) as run:
            WaylandClipboard().read()
        self.assertEqual(run.call_args.kwargs["timeout"], SUBPROCESS_TIMEOUT)

    def test_image_body_timeout_is_none_and_logged(self):
        """The body fetch's OWN timeout, distinct from --list-types': proves
        _run_wl_paste's shared failure handling actually reaches the second
        call too, not only the first -- the two are exercised separately on
        purpose rather than assumed identical from one passing case."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\n"),
            subprocess.TimeoutExpired("wl-paste", IMAGE_SUBPROCESS_TIMEOUT),
        ]):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("wl-paste failed" in line for line in self.log_lines),
            "a body-read timeout must be logged: %r" % self.log_lines,
        )

    def test_every_subprocess_call_logs_its_own_duration(self):
        """'log every read's duration' (the brief's own words), so the next
        time a bound is wrong there is evidence, not a guess. Checked
        against the log line's own shape -- both calls, not just one --
        rather than the timing value itself, which a mocked call cannot
        pin meaningfully."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b"hello"),
        ]):
            WaylandClipboard().read()
        duration_lines = [line for line in self.log_lines if line.startswith("clipboard read (")]
        self.assertEqual(
            len(duration_lines), 2,
            "both the --list-types call and the body read must each log their "
            "own duration: %r" % self.log_lines,
        )

    def test_timeout_returns_none_and_logs(self):
        with mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("wl-paste", SUBPROCESS_TIMEOUT)
        ):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("wl-paste" in line for line in self.log_lines),
            "a timeout must be logged: %r" % self.log_lines,
        )

    def test_missing_binary_returns_none_and_logs(self):
        with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("not installed" in line for line in self.log_lines),
            "a missing binary must be logged: %r" % self.log_lines,
        )

    def test_permission_denied_at_spawn_returns_none_and_logs(self):
        """wl-paste present but not executable raises PermissionError, an
        OSError that is not a FileNotFoundError -- the same latent escape
        as write()'s spawn guard."""
        with mock.patch("subprocess.run", side_effect=PermissionError("denied")):
            self.assertIsNone(WaylandClipboard().read())
        self.assertTrue(
            any("wl-paste" in line for line in self.log_lines),
            "a PermissionError at spawn must be logged: %r" % self.log_lines,
        )

    def test_repeated_timeout_logs_only_once_until_it_clears(self):
        """The design promises this is logged once, not every few seconds
        forever -- in polling mode, a hung selection owner makes every
        tick's wl-paste time out, and an unconditional log line on every
        occurrence would spam a line across the channel into the Mac's
        log for as long as the hang lasts. The per-call duration line added
        this task is deliberately worded without the substring "wl-paste"
        (see _run_wl_paste's own docstring), so this count keeps counting
        failures only, unaffected by it."""
        clipboard = WaylandClipboard()
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("wl-paste", SUBPROCESS_TIMEOUT),
        ):
            clipboard.read()
            clipboard.read()
            clipboard.read()
        self.assertEqual(
            sum(1 for line in self.log_lines if "wl-paste" in line), 1,
            "a repeated timeout must log once, not on every occurrence: %r" % self.log_lines,
        )

    def test_timeout_logs_again_after_a_successful_read_clears_it(self):
        """Once the hang clears (a normal completed read, whatever its
        return code), the condition has cleared -- a later, new timeout
        is a fresh occurrence and must be logged again, not silenced
        forever by the first one."""
        clipboard = WaylandClipboard()
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("wl-paste", SUBPROCESS_TIMEOUT),
        ):
            clipboard.read()
        with mock.patch("subprocess.run", return_value=_completed(stdout=b"back")):
            clipboard.read()
        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired("wl-paste", SUBPROCESS_TIMEOUT),
        ):
            clipboard.read()
        self.assertEqual(
            sum(1 for line in self.log_lines if "wl-paste" in line), 2,
            "a new timeout after the condition clears must be logged again: %r" % self.log_lines,
        )


class TestCanonicalRead(unittest.TestCase):
    """choose_kind and WaylandClipboard.read() together: the shared fixture
    pins the decision table, and the two read() cases pin that read() wires
    --list-types's answer into that same decision, then asks for the right
    body."""

    def test_the_kind_matches_the_shared_fixture(self):
        rows = json.loads(CLIPKIND_FIXTURE.read_text())
        for row in rows:
            with self.subTest(row["name"]):
                self.assertEqual(choose_kind(row["types"]), row["expect"])

    def test_read_returns_the_text_body_when_text_is_present(self):
        clip = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\nimage/png\n"),
            _completed(stdout=b"hello"),
        ]):
            self.assertEqual(clip.read(), (KIND_TEXT, b"hello"))

    def test_read_asks_for_png_when_only_an_image_is_present(self):
        clip = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/tiff\nimage/png\n"),
            _completed(stdout=b"\x89PNG..."),
        ]) as run:
            self.assertEqual(clip.read(), (KIND_IMAGE, b"\x89PNG..."))
        self.assertIn("image/png", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
