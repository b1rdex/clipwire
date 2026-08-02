# agent/tests/test_watcher_pdeathsig.py
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
class TestPdeathsigPreexec(unittest.TestCase):
    def test_load_libc_is_none_when_libc_is_unavailable(self):
        """The macOS path: CDLL("libc.so.6") raises OSError there (no such
        library), and _load_libc() must come back with None rather than let
        it propagate -- this is what lets the whole module still import
        cleanly on the machine this suite runs on. Mocked rather than relying
        on the real macOS behavior, so this also pins the same contract on
        Linux, where CDLL would otherwise succeed for real."""
        with mock.patch.object(clipwire_agent.ctypes, "CDLL", side_effect=OSError):
            self.assertIsNone(clipwire_agent._load_libc())

    def test_load_libc_is_none_where_ctypes_itself_is_unavailable(self):
        """`import ctypes` is guarded at module level, because ctypes is an
        extension module and a stripped or unusual build can genuinely lack
        it -- and this file is copied to whatever Python the PC happens to
        have. The obvious follow-up guard is the wrong one: with `ctypes`
        bound to None, `ctypes.CDLL(...)` raises AttributeError, which is
        neither ImportError nor OSError and so escapes _load_libc entirely,
        taking the whole module down at import. That turns a missing
        optional nicety -- the pdeathsig belt on top of stop()'s braces --
        into an agent that cannot start at all."""
        with mock.patch.object(clipwire_agent, "ctypes", None):
            self.assertIsNone(clipwire_agent._load_libc())

    def test_it_does_not_resolve_libc_at_call_time(self):
        """The whole point of _LIBC: nothing inside preexec_fn may import,
        allocate, or take a lock, because preexec_fn runs in a forked child of
        a threaded process. _load_libc() calling import ctypes is fine at
        module load and would be a hazard here -- so this pins that
        _pdeathsig_preexec never calls it, not merely that _PRCTL is read."""
        with mock.patch.object(clipwire_agent, "_load_libc") as loader, \
             mock.patch.object(clipwire_agent, "_PRCTL", None):
            clipwire_agent._pdeathsig_preexec()
        loader.assert_not_called()

    def test_it_does_not_look_up_the_prctl_symbol_inside_the_fork(self):
        """The same hazard as the test above, one layer further down, and
        the one _LIBC alone did not close: ctypes resolves a CDLL's symbols
        LAZILY. `_LIBC.prctl` performs a dlsym on first access and caches
        the result on the library object -- so with the lookup written that
        way, the FIRST forked child was the one paying for it, inside
        preexec_fn. dlsym takes the dynamic loader's lock, and fork() clones
        only the calling thread without releasing locks another thread
        holds, so a child that forks at the wrong instant inherits that lock
        held forever and wedges before it ever execs. That is exactly the
        stuck-gdbus-child symptom the pdeathsig fix exists to remove,
        reintroduced by a subtler path.

        Binding the symbol once at import, into _PRCTL, is the fix; this
        pins that _pdeathsig_preexec calls that handle and never reaches
        through _LIBC for an attribute at all."""
        calls = []

        class ExplodingLibc:
            def __getattr__(self, name):
                raise AssertionError(
                    "preexec_fn resolved %r through _LIBC inside the fork" % name)

        def fake_prctl(*args):
            calls.append(args)
            return 0

        with mock.patch.object(clipwire_agent, "_LIBC", ExplodingLibc()), \
             mock.patch.object(clipwire_agent, "_PRCTL", fake_prctl), \
             mock.patch.object(clipwire_agent.os, "getppid", return_value=42):
            clipwire_agent._pdeathsig_preexec()

        self.assertEqual(calls, [(clipwire_agent.PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)])

    def test_the_prctl_handle_is_bound_at_import(self):
        """The other half of the rule above, pinned at the module rather
        than at the call: _PRCTL exists as a module-level name, and it is
        non-None exactly when _LIBC is. On this suite's macOS host both are
        None; on the PC both are real. Either way the binding has already
        happened by the time any thread or fork exists."""
        self.assertEqual(clipwire_agent._PRCTL is None, clipwire_agent._LIBC is None)

    def test_it_requests_sigterm_when_the_parent_dies(self):
        calls = []

        def fake_prctl(option, sig, *rest):
            calls.append((option, sig))
            return 0

        with mock.patch.object(clipwire_agent, "_PRCTL", fake_prctl), \
             mock.patch.object(clipwire_agent.os, "getppid", return_value=42):
            clipwire_agent._pdeathsig_preexec()

        self.assertEqual(calls, [(clipwire_agent.PR_SET_PDEATHSIG, signal.SIGTERM)])

    def test_it_exits_when_the_parent_already_died(self):
        """The fork/prctl window: if the parent died in between, the signal
        never arrives, so the child must notice and leave on its own.

        os._exit is mocked rather than expected to raise: it does NOT raise
        SystemExit, it ends the process immediately -- which is correct inside
        a preexec_fn, where an exception would be re-raised in the PARENT and
        take the agent down instead of the child. An assertRaises here would
        kill the test runner.
        """
        with mock.patch.object(clipwire_agent, "_PRCTL", lambda *rest: 0), \
             mock.patch.object(clipwire_agent.os, "getppid", return_value=1), \
             mock.patch.object(clipwire_agent.os, "_exit") as exit_call:
            clipwire_agent._pdeathsig_preexec()
        exit_call.assert_called_once_with(0)

    def test_it_is_a_no_op_where_prctl_is_unavailable(self):
        with mock.patch.object(clipwire_agent, "_PRCTL", None):
            clipwire_agent._pdeathsig_preexec()   # must not raise


