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
    SLOW_IMAGE_READ_SECONDS,
    SLOW_READ_SECONDS,
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


def _completed(returncode=0, stdout=b"", stderr=b""):
    """A subprocess.CompletedProcess shaped like a real wl-paste result.
    Module-level so every class below shares one definition -- TestReadSubprocessBehavior
    used to keep its own copy as a bound method; folded into this one now that
    TestCanonicalRead needs the same shape too. stderr defaults empty because no
    case in this file reads it; test_clipboard_classified.py's cases do, and
    import this anchor rather than keep a second _completed (v3.2.1 split
    convention: shared doubles are imported, never copied)."""
    return subprocess.CompletedProcess(args=["wl-paste"], returncode=returncode, stdout=stdout, stderr=stderr)


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
        failures only, unaffected by it -- and unaffected too by Fix round
        1's gate, which governs only whether the duration line appears, not
        the separately-mechanised "wl-paste failed" dedup this counts."""
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

    def test_the_connections_first_read_logs_both_calls_unconditionally(self):
        """Fix round 1: duration logging is now gated (see
        TestDurationLoggingIsGated below for the volume this exists to
        prevent), but the very FIRST read() a WaylandClipboard makes is the
        one deliberate exception -- both of its wl-paste calls log
        regardless of how fast they were, so there is always at least one
        baseline pair in the log before the gate takes over. Checked
        against the log line's own shape -- both calls, not just one --
        rather than the timing value itself, which a mocked call cannot pin
        meaningfully."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b"hello"),
        ]):
            WaylandClipboard().read()
        duration_lines = [line for line in self.log_lines if line.startswith("clipboard read (")]
        self.assertEqual(
            len(duration_lines), 2,
            "the connection's first read must log both the --list-types call "
            "and the body read's own duration, unconditionally: %r" % self.log_lines,
        )


class TestProbeIsCheapForImages(unittest.TestCase):
    """probe() is the poll loop's change token, and the spec requires it:
    "in degraded mode the image body must be checked off a change in
    `wl-paste --list-types` rather than the content itself, with the added
    latency documented."

    What the plan shipped instead was PollingWatcher.pump calling read() on
    every tick. Once read() became kind-aware that meant two forks and the
    WHOLE image body -- up to MAX_IMAGE_BYTES through a pipe -- on every
    tick of every connection, HEALTHY ones included, since the safety net
    runs whether or not anything is wrong. Two 4 MiB buffers stayed
    resident as `previous` and `current` and were compared each tick, and
    any read slow enough to cross SLOW_IMAGE_READ_SECONDS logged a duration
    line EVERY TICK -- the exact flood TestDurationLoggingIsGated below
    exists to prevent, arriving through the one call site its gate cannot
    help.

    In v2 none of this existed: the read was text-only, so an image
    clipboard read back as nothing and cost nothing at all."""

    def setUp(self):
        self.log_lines = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def test_an_image_probe_asks_for_the_types_and_never_the_body(self):
        """The whole deliverable, in call count: ONE wl-paste invocation
        for an image clipboard, where read() makes two and pipes the body
        through the second."""
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"image/png\nimage/webp\n")) as run:
            token = WaylandClipboard().probe()
        self.assertEqual(run.call_count, 1,
                         "an image probe must never fetch the body")
        self.assertEqual(token, (KIND_IMAGE, ("image/png", "image/webp")))

    def test_the_image_token_is_the_type_list_not_the_body(self):
        """Deliberately NOT the shape read() returns for the same
        clipboard, so the two can never compare equal and confusing them is
        loud rather than silent. Nothing may hash, send or write a token."""
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"image/png\n")):
            token = WaylandClipboard().probe()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\n"),
            _completed(stdout=b"\x89PNG-body"),
        ]):
            read = WaylandClipboard().read()
        self.assertNotEqual(token, read)
        self.assertNotIsInstance(token[1], bytes)

    def test_a_reordered_type_list_is_not_a_change(self):
        """The token is compared with `!=` by PollingWatcher.pump, so its
        ORDER is as load-bearing as its membership -- and nothing in this
        chain has a live compositor to establish that `wl-paste
        --list-types` prints a stable order for an unchanged selection.

        If it does not, the consequence is precisely the defect this
        release exists to fix: pump signals a change nobody made,
        _observe_tick arms its verdict, and one more silent tick CONFIRMS
        it -- a healthy install declared dead and dropped to 1-second
        polling for the rest of the connection. Under the old body
        comparison the bytes were stable, so this is a new input class
        rather than a pre-existing risk.

        sorted() removes the assumption instead of betting on it, at no
        cost: the token becomes a membership comparison, and every real
        change alters the membership."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\nimage/webp\nimage/tiff\n"),
            _completed(stdout=b"image/tiff\nimage/png\nimage/webp\n"),
        ]):
            first = clipboard.probe()
            second = clipboard.probe()
        self.assertEqual(first, second,
                         "the same types in a different order are the same clipboard")

    def test_a_genuinely_different_type_list_is_still_a_change(self):
        """The complement, and what keeps the sort from being a way to
        stop noticing things: order-insensitive is not change-insensitive.
        A selection offering a different SET of formats is a different
        selection, and the token must say so."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\nimage/webp\n"),
            _completed(stdout=b"image/png\nimage/tiff\n"),
        ]):
            first = clipboard.probe()
            second = clipboard.probe()
        self.assertNotEqual(first, second)

    def test_a_second_image_probe_with_the_same_types_compares_equal(self):
        """What makes the token usable as `previous` at all -- and, said
        plainly, what the added latency IS: two selections offering the
        same type list are indistinguishable to the poll loop, so an image
        replacing an identical-typed image is not seen until the next
        type-list change. See probe()'s own docstring for why that is
        expected to be rare here and what covers it in healthy mode."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"image/png\nimage/webp\n")):
            first = clipboard.probe()
            second = clipboard.probe()
        self.assertEqual(first, second)

    def test_a_text_probe_is_the_body_exactly_as_read_returns_it(self):
        """Text keeps v2's behaviour byte for byte: there is no cheap proxy
        for text, its cost is not what the spec objects to, and an
        identical token means every text path behaves exactly as it did."""
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"),
            _completed(stdout=b"clip contents"),
        ]) as run:
            self.assertEqual(WaylandClipboard().probe(), (KIND_TEXT, b"clip contents"))
        self.assertEqual(run.call_count, 2)

    def test_probe_is_none_on_the_same_listing_failures_read_is(self):
        """_observe_tick's `read_ok` treats None as "no evidence either
        way" and resets an armed verdict, so probe() reporting a live token
        where read() would report None changes what the safety net counts
        as evidence. Both listing failures stay identical."""
        for stdout, returncode in ((b"", 1), (b"TARGETS\nTIMESTAMP\n", 0)):
            with self.subTest(returncode=returncode):
                with mock.patch("subprocess.run",
                                return_value=_completed(returncode=returncode, stdout=stdout)):
                    self.assertIsNone(WaylandClipboard().probe())
                    self.assertIsNone(WaylandClipboard().read())

    def test_repeated_image_probes_never_log_a_body_read_duration(self):
        """The compounding half of the finding. With the loop calling
        read(), an image body slow enough to cross SLOW_IMAGE_READ_SECONDS
        -- which a large screenshot through a pipe can easily be -- logged
        a duration line on EVERY tick, 86,400 a day at
        DEGRADED_POLL_SECONDS=1.0, all of it crossing the SSH channel into
        the Mac's log. That is the flood the gate below was added to
        prevent, arriving through the one call site the gate cannot help:
        the read genuinely IS slow, so the threshold is genuinely met,
        every single tick.

        probe() never fetches the body, so the line has no way to recur.
        Ten ticks produce exactly the one --list-types line the
        connection's first call is entitled to, and no body line ever --
        whatever the body would have cost."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run",
                        return_value=_completed(stdout=b"image/png\n")):
            for _ in range(10):
                clipboard.probe()
        duration_lines = [line for line in self.log_lines if line.startswith("clipboard read (")]
        self.assertEqual(
            len(duration_lines), 1,
            "only the connection's first call may log unconditionally: %r" % self.log_lines)
        self.assertNotIn("image/png", duration_lines[0],
                         "no probe may ever report the duration of a body fetch")

    def test_the_first_call_of_either_kind_claims_the_unconditional_baseline(self):
        """_first_read_done belongs to the clipboard, not to read(): a
        baseline duration from a probe is worth exactly as much as one from
        a read, and the flag must be consumed once per connection however
        it is first reached. In production read() still wins -- the
        connect-time seed runs before any watcher is built -- but nothing
        about the flag depends on that ordering, and this pins that a probe
        which got there first does not leave a second baseline to be
        claimed by the next read."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\n"),
            _completed(stdout=b"image/png\n"),
            _completed(stdout=b"\x89PNG-body"),
        ]):
            clipboard.probe()
            clipboard.read()
        self.assertEqual(
            len([line for line in self.log_lines if line.startswith("clipboard read (")]), 1,
            "the probe claimed the baseline; the read that follows must not re-claim it")


class TestDurationLoggingIsGated(unittest.TestCase):
    """Fix round 1. Logging every read's duration unconditionally -- built
    exactly as the original brief specified, and exactly what
    TestReadSubprocessBehavior's own first-read test above still pins --
    floods the log at production scale: PollingWatcher's safety net forks
    wl-paste on every tick for as long as a connection lasts, whether or not
    anything changed. That was clipboard.read(), up to two invocations, so
    at DEGRADED_POLL_SECONDS=1.0: 86400 x 2 = 172,800 duration lines a day,
    all of it crossing the SSH channel into the Mac's log. The loop asks
    clipboard.probe() now (TestProbeIsCheapForImages above), which is still
    two invocations for text -- so that ceiling is unchanged and this gate
    is still the only thing holding it down. Every case here is pinned by
    LINE COUNT, not by wording -- the
    deliverable the coordinator asked for is a test that goes red if the
    gate is ever removed, and a wording-based assertion would not notice
    that; a count would."""

    def setUp(self):
        self.log_lines = []
        original_log = clipwire_agent.log
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)

    def _duration_lines(self):
        return [line for line in self.log_lines if line.startswith("clipboard read (")]

    def test_fast_reads_after_the_first_do_not_each_log_a_duration_line(self):
        """The gate's core claim, reproduced directly: three MORE reads
        after the connection's first, all fast (mocked subprocess.run calls
        complete in effectively zero time, comfortably under
        SLOW_READ_SECONDS), must not each add a duration line. If the gate
        were ever removed -- reverting to logging every call
        unconditionally, which is what this task's own first cut did --
        this goes red: 4 reads x 2 calls = 8 lines, not the 2 asserted
        here."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"one"),
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"two"),
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"three"),
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"four"),
        ]):
            clipboard.read()  # the connection's first read -- logs unconditionally
            clipboard.read()
            clipboard.read()
            clipboard.read()
        self.assertEqual(
            len(self._duration_lines()), 2,
            "only the first read's two calls should ever log here -- three more fast "
            "reads (six more wl-paste calls) must add nothing: %r" % self.log_lines,
        )

    def test_a_slow_read_after_the_first_still_logs(self):
        """The gate's other half, so the fix cannot be 'never log again
        after the first read': a read past SLOW_READ_SECONDS must still
        produce a line, on whichever read() call and whichever of the two
        wl-paste invocations it happens on. time.monotonic is mocked
        (rather than actually sleeping, which this suite avoids throughout)
        to make the SECOND wl-paste call of the second read() report a
        duration safely past the threshold with no real delay."""
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"fast one"),
        ]):
            clipboard.read()  # consumes the connection's unconditional first read
        self.log_lines.clear()

        # time.monotonic is called twice per _run_wl_paste call (started,
        # then the duration subtraction): [start1, end1, start2, end2].
        # The first (--list-types) is fast; the second (the text body) is
        # pushed well past SLOW_READ_SECONDS.
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"slow one"),
        ]), mock.patch("time.monotonic", side_effect=[0.0, 0.01, 10.0, 10.0 + SLOW_READ_SECONDS + 1]):
            clipboard.read()

        duration_lines = self._duration_lines()
        self.assertEqual(
            len(duration_lines), 1,
            "a read past the threshold must still log even though it is not "
            "the connection's first: %r" % self.log_lines,
        )
        self.assertIn(
            "text/plain;charset=utf-8", duration_lines[0],
            "the SLOW call (the body fetch) must be the one that logged, not "
            "the fast --list-types call: %r" % duration_lines,
        )

    def test_the_image_threshold_is_higher_than_the_text_threshold_and_applied_independently(self):
        """Holding both call shapes to the same number would either spam on
        a normal-sized image (if set to the text threshold) or hide a
        genuinely slow text read for extra seconds (if set to the image
        threshold) -- pinned directly by a duration that is slow BY TEXT'S
        standard but not by image's, on an actual image read."""
        self.assertGreater(
            SLOW_IMAGE_READ_SECONDS, SLOW_READ_SECONDS,
            "an image body is up to MAX_IMAGE_BYTES through a pipe, not a few "
            "kilobytes of text -- it needs a higher bar before its duration is "
            "worth a line",
        )
        clipboard = WaylandClipboard()
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"text/plain;charset=utf-8\n"), _completed(stdout=b"fast"),
        ]):
            clipboard.read()  # consumes the connection's unconditional first read
        self.log_lines.clear()

        between_the_two_thresholds = (SLOW_READ_SECONDS + SLOW_IMAGE_READ_SECONDS) / 2
        with mock.patch("subprocess.run", side_effect=[
            _completed(stdout=b"image/png\n"), _completed(stdout=b"\x89PNG-body"),
        ]), mock.patch("time.monotonic", side_effect=[
            0.0, 0.01, 10.0, 10.0 + between_the_two_thresholds,
        ]):
            clipboard.read()

        self.assertEqual(
            self._duration_lines(), [],
            "a duration between the two thresholds is slow for text but not "
            "for an image, and this read is an image: %r" % self.log_lines,
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
