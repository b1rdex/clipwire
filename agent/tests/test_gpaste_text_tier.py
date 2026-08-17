# agent/tests/test_gpaste_text_tier.py
"""The v3.4 focus-free text tier: bodies through GPaste's D-Bus, fail-open on
every edge -- None/None mean NOT DONE and the caller falls back to
wl-clipboard. The read-back is the only thing that confirms a write: GPaste
drops an over-limit clip silently with exit 0 (measured, v3.4 §3.4/§4.4)."""
import subprocess
import unittest

from agent_under_test import (
    GPASTE_ADD_TIMEOUT_SECONDS,
    GPASTE_CALL_TIMEOUT,
    GPasteTextTier,
    gpaste_element_kind,
    gpaste_select,
)


def _completed(returncode, stdout=b"", stderr=b""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class ScriptedRun:
    """Answers run() calls in order from a script; an Exception instance in
    the script is raised instead of returned. Records every argv and kwargs."""
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


class FakeAddProcess:
    """Stands in for Popen(["gpaste-client", "add"]): records the body handed
    to communicate() and whether it was killed; optionally times out until
    killed, the way a real add with an unreachable daemon does."""
    def __init__(self, returncode=0, times_out=False):
        self.body = None
        self.returncode = returncode
        self.killed = False
        self._times_out = times_out

    def communicate(self, input=None, timeout=None):
        if self._times_out and not self.killed:
            raise subprocess.TimeoutExpired(["gpaste-client", "add"], timeout)
        if input is not None:
            self.body = input
        return b"", b""

    def kill(self):
        self.killed = True


class TestElementKind(unittest.TestCase):
    def test_parses_the_kind_out_of_the_gvariant_tuple(self):
        run = ScriptedRun([_completed(0, b"('Text',)\n")])
        self.assertEqual(gpaste_element_kind("u-1", run=run), "Text")
        argv, kwargs = run.calls[0]
        self.assertEqual(argv[-2:], ["org.gnome.GPaste2.GetElementKind", "u-1"])
        self.assertEqual(kwargs.get("timeout"), GPASTE_CALL_TIMEOUT)

    def test_image_is_image(self):
        run = ScriptedRun([_completed(0, b"('Image',)\n")])
        self.assertEqual(gpaste_element_kind("u-1", run=run), "Image")

    def test_failure_shapes_are_all_none(self):
        for answer in (_completed(1, stderr=b"gone"),
                       _completed(0, b"garbage"),
                       subprocess.TimeoutExpired(["gdbus"], 3),
                       OSError("no gdbus")):
            with self.subTest(answer=answer):
                self.assertIsNone(gpaste_element_kind("u-1", run=ScriptedRun([answer])))


class TestSelect(unittest.TestCase):
    def test_exit_zero_is_true(self):
        run = ScriptedRun([_completed(0, b"()\n")])
        self.assertIs(gpaste_select("u-1", run=run), True)
        self.assertEqual(run.calls[0][0][-2:], ["org.gnome.GPaste2.Select", "u-1"])

    def test_failure_shapes_are_all_false(self):
        for answer in (_completed(1), subprocess.TimeoutExpired(["gdbus"], 3),
                       FileNotFoundError("no gdbus")):
            with self.subTest(answer=answer):
                self.assertIs(gpaste_select("u-1", run=ScriptedRun([answer])), False)


class TestReadText(unittest.TestCase):
    def test_text_kind_routes_to_raw_get(self):
        tier = GPasteTextTier(run=ScriptedRun([_completed(0, b"the body")]),
                              read_element_kind=lambda uuid, run=None: "Text")
        self.assertEqual(tier.read_text("u-1"), b"the body")

    def test_raw_get_invocation_shape(self):
        """--raw get <uuid>, stdin=DEVNULL (EOF before the verb, v3.4 §4.1),
        GPASTE_CALL_TIMEOUT. The invocation IS the contract."""
        run = ScriptedRun([_completed(0, b"x")])
        tier = GPasteTextTier(run=run, read_element_kind=lambda u, run=None: "Text")
        tier.read_text("u-1")
        argv, kwargs = run.calls[0]
        self.assertEqual(argv, ["gpaste-client", "--raw", "get", "u-1"])
        self.assertEqual(kwargs.get("stdin"), subprocess.DEVNULL)
        self.assertEqual(kwargs.get("timeout"), GPASTE_CALL_TIMEOUT)

    def test_non_text_kinds_never_reach_raw_get(self):
        """GetElementAtIndex/display would hand back '[Image, ...]' -- wrong,
        not missing. Reversing this check must go red here (v3.4 §6.7)."""
        for kind in ("Image", "Uris", "Password", None):
            with self.subTest(kind=kind):
                run = ScriptedRun([])          # any call would IndexError
                tier = GPasteTextTier(run=run,
                                      read_element_kind=lambda u, run=None, k=kind: k)
                self.assertIsNone(tier.read_text("u-1"))
                self.assertEqual(run.calls, [])

    def test_empty_body_is_an_answer_not_a_failure(self):
        tier = GPasteTextTier(run=ScriptedRun([_completed(0, b"")]),
                              read_element_kind=lambda u, run=None: "Text")
        self.assertEqual(tier.read_text("u-1"), b"")

    def test_fetch_failures_are_none(self):
        for answer in (_completed(1), subprocess.TimeoutExpired(["gpaste-client"], 3),
                       FileNotFoundError("client gone")):
            with self.subTest(answer=answer):
                tier = GPasteTextTier(run=ScriptedRun([answer]),
                                      read_element_kind=lambda u, run=None: "Text")
                self.assertIsNone(tier.read_text("u-1"))


class TestWriteText(unittest.TestCase):
    def _tier(self, process, uuid="u-new", raw=None, monotonic=lambda: 42.0):
        answers = [] if raw is None else [raw]
        self.run = ScriptedRun(answers)
        self.popen_calls = []

        def popen(argv, **kwargs):
            self.popen_calls.append((argv, kwargs))
            return process

        return GPasteTextTier(run=self.run, popen=popen,
                              read_history_uuid=lambda: uuid,
                              monotonic=monotonic)

    def test_confirmed_write_returns_uuid_and_records_it(self):
        process = FakeAddProcess()
        tier = self._tier(process, raw=_completed(0, b"the body"))
        self.assertEqual(tier.write_text(b"the body"), "u-new")
        self.assertEqual(process.body, b"the body")
        uuid, stamp, sha = tier.last_confirmed_write
        self.assertEqual(uuid, "u-new")
        self.assertEqual(stamp, 42.0)
        self.assertEqual(len(sha), 64)
        argv, kwargs = self.popen_calls[0]
        self.assertEqual(argv, ["gpaste-client", "add"])
        self.assertEqual(kwargs.get("stdin"), subprocess.PIPE)

    def test_exit_zero_alone_is_not_confirmation(self):
        """The 1.5 MiB measurement: exit 0, history unmoved, clip gone. Only
        the read-back confirms; deleting it must go red HERE, on the loss."""
        process = FakeAddProcess(returncode=0)
        tier = self._tier(process, raw=_completed(0, b"whatever was there before"))
        self.assertIsNone(tier.write_text(b"the 1.5 MiB clip"))
        self.assertEqual(tier.last_mismatch_top, b"whatever was there before")
        self.assertIsNone(tier.last_confirmed_write)

    def test_timeout_kills_and_declines(self):
        process = FakeAddProcess(times_out=True)
        tier = self._tier(process)
        self.assertIsNone(tier.write_text(b"body"))
        self.assertTrue(process.killed)
        self.assertIsNone(tier.last_confirmed_write)

    def test_nonzero_exit_declines_without_a_read_back(self):
        process = FakeAddProcess(returncode=1)
        tier = self._tier(process)          # raw=None: a read-back would IndexError
        self.assertIsNone(tier.write_text(b"body"))

    def test_missing_client_declines(self):
        def popen(argv, **kwargs):
            raise FileNotFoundError("gpaste-client")
        tier = GPasteTextTier(popen=popen)
        self.assertIsNone(tier.write_text(b"body"))

    def test_unreadable_read_back_declines_without_a_mismatch_top(self):
        """uuid or raw-get failing after the add: not confirmed, and there is
        no top to re-arm the echo with -- last_mismatch_top must stay None."""
        process = FakeAddProcess()
        tier = GPasteTextTier(run=ScriptedRun([_completed(1)]),
                              popen=lambda argv, **k: process,
                              read_history_uuid=lambda: "u-new")
        self.assertIsNone(tier.write_text(b"body"))
        self.assertIsNone(tier.last_mismatch_top)
        tier2 = GPasteTextTier(popen=lambda argv, **k: FakeAddProcess(),
                               read_history_uuid=lambda: None)
        self.assertIsNone(tier2.write_text(b"body"))
        self.assertIsNone(tier2.last_mismatch_top)

    def test_add_timeout_is_its_own_and_at_least_five(self):
        """3 s against a measured flat 1.41 s (2026-08-05) is 2.1x headroom --
        a timeout that fires under load rather than one that catches a hang
        (§4.1.1). The same Add measured 14-35 ms on 2026-08-17 on the same
        machine; the two readings are not reconciled, so this stays sized to
        the worse day rather than to either measured cost."""
        self.assertGreaterEqual(GPASTE_ADD_TIMEOUT_SECONDS, 5)
        recorded = {}

        class Recorder(FakeAddProcess):
            def communicate(self, input=None, timeout=None):
                recorded["timeout"] = timeout
                return super().communicate(input, timeout)

        tier = self._tier(Recorder(), raw=_completed(0, b"body"))
        tier.write_text(b"body")
        self.assertEqual(recorded["timeout"], GPASTE_ADD_TIMEOUT_SECONDS)

    def test_a_mismatch_clears_on_the_next_write(self):
        process = FakeAddProcess()
        tier = GPasteTextTier(run=ScriptedRun([_completed(0, b"old"),
                                               _completed(0, b"fresh")]),
                              popen=lambda argv, **k: process,
                              read_history_uuid=lambda: "u-x",
                              monotonic=lambda: 1.0)
        self.assertIsNone(tier.write_text(b"fresh"))       # mismatch: top is b"old"
        self.assertEqual(tier.last_mismatch_top, b"old")
        self.assertEqual(tier.write_text(b"fresh"), "u-x")  # confirmed now
        self.assertIsNone(tier.last_mismatch_top)


if __name__ == "__main__":
    unittest.main()
