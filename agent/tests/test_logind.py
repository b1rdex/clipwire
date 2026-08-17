# agent/tests/test_logind.py
"""Resolving the graphical session (v3.5 spec §3.2): the agent lives in an
ssh session whose LockedHint is forever 'no', so picking the wrong session is
the one mistake fail-open makes silent. Fixtures captured on the real PC,
2026-08-17."""
import subprocess
import unittest
from unittest import mock

from agent_under_test import (
    GRAPHICAL_SESSION_TYPES,
    LOGIND_SESSION_IFACE,
    parse_logind_sessions,
    resolve_graphical_session,
    session_locked_hint,
)

# Verbatim from the PC -- type annotations on the FIRST element only.
LIST_SESSIONS_REAL = ("([('2', uint32 1000, 'anatoly', 'seat0', objectpath "
    "'/org/freedesktop/login1/session/_32'), ('245', 1000, 'anatoly', '', "
    "'/org/freedesktop/login1/session/_3245'), ('194', 1000, 'anatoly', '', "
    "'/org/freedesktop/login1/session/_3194'), ('247', 1000, 'anatoly', '', "
    "'/org/freedesktop/login1/session/_3247'), ('3', 1000, 'anatoly', '', "
    "'/org/freedesktop/login1/session/_33'), ('231', 1000, 'anatoly', '', "
    "'/org/freedesktop/login1/session/_3231')],)\n")

SESSION_PROPS_REAL = {  # all measured 2026-08-17: _32 directly; ssh=user/tty and
                        # manager=manager/unspecified confirmed on the same day's live table
    "/org/freedesktop/login1/session/_32": {"Class": "user", "Type": "wayland"},
    "/org/freedesktop/login1/session/_3245": {"Class": "user", "Type": "tty"},
    "/org/freedesktop/login1/session/_3194": {"Class": "user", "Type": "tty"},
    "/org/freedesktop/login1/session/_3247": {"Class": "user", "Type": "tty"},
    "/org/freedesktop/login1/session/_33": {"Class": "manager", "Type": "unspecified"},
    "/org/freedesktop/login1/session/_3231": {"Class": "user", "Type": "tty"},
}


def _scripted_run(props, list_reply=LIST_SESSIONS_REAL):
    """A subprocess.run double answering ListSessions and Properties.Get."""
    def run(argv, **kwargs):
        proc = mock.Mock()
        proc.returncode = 0
        if "ListSessions" in " ".join(argv):
            proc.stdout = list_reply.encode()
            return proc
        path = argv[argv.index("--object-path") + 1]
        prop = argv[-1]
        value = props.get(path, {}).get(prop)
        if value is None:
            proc.returncode = 1
            proc.stdout = b""
        else:
            proc.stdout = ("(<'%s'>,)\n" % value).encode()
        return proc
    return run


class TestParseSessions(unittest.TestCase):
    def test_all_six_sessions_survive_the_annotation_trap(self):
        parsed = parse_logind_sessions(LIST_SESSIONS_REAL)
        self.assertEqual(len(parsed), 6)
        self.assertIn(("/org/freedesktop/login1/session/_32", "seat0"), parsed)
        self.assertIn(("/org/freedesktop/login1/session/_3231", ""), parsed)

    def test_garbage_parses_to_nothing(self):
        self.assertEqual(parse_logind_sessions("error: not gvariant"), [])


class TestResolve(unittest.TestCase):
    def test_picks_the_one_graphical_user_session(self):
        run = _scripted_run(SESSION_PROPS_REAL)
        self.assertEqual(resolve_graphical_session(run=run),
                         "/org/freedesktop/login1/session/_32")

    def test_wrong_session_regression(self):
        """Spec §7.4: a resolver keyed on anything that matches an ssh or
        manager session must go red here. Every non-graphical session claims
        Class=user except the manager; only _32 is Type=wayland."""
        props = dict(SESSION_PROPS_REAL)
        run = _scripted_run(props)
        resolved = resolve_graphical_session(run=run)
        self.assertNotIn(resolved, [p for p in props if p != "/org/freedesktop/login1/session/_32"])

    def test_two_graphical_candidates_fail_open(self):
        props = dict(SESSION_PROPS_REAL)
        props["/org/freedesktop/login1/session/_3245"] = {"Class": "user", "Type": "x11"}
        self.assertIsNone(resolve_graphical_session(run=_scripted_run(props)))

    def test_a_failed_probe_among_two_graphical_candidates_fails_open(self):
        """Controller ruling, spec §3.2 step 1 (amended 726a978): a probe
        that never answers is not "definitely not a candidate" -- it may be
        hiding the second graphical session, so a single confirmed match
        under a probe failure is not "exactly one"."""
        props = dict(SESSION_PROPS_REAL)
        # _3245 reads as graphical, but its Class probe never answers (rc != 0).
        props["/org/freedesktop/login1/session/_3245"] = {"Type": "x11"}
        self.assertIsNone(resolve_graphical_session(run=_scripted_run(props)))

    def test_a_failed_probe_on_an_unrelated_session_still_fails_open(self):
        """Mirrors the above: the poison is global, not scoped to a
        plausible candidate. _3194 was never going to qualify -- it's a tty
        login -- but its Type probe failing outright is still enough to void
        an otherwise-clean single match (spec §3.2 step 1, amended 726a978)."""
        props = dict(SESSION_PROPS_REAL)
        props["/org/freedesktop/login1/session/_3194"] = {"Class": "user"}
        self.assertIsNone(resolve_graphical_session(run=_scripted_run(props)))

    def test_zero_candidates_fail_open(self):
        props = {p: {"Class": "user", "Type": "tty"} for p in SESSION_PROPS_REAL}
        self.assertIsNone(resolve_graphical_session(run=_scripted_run(props)))

    def test_listsessions_failure_fails_open(self):
        def run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, 3)
        self.assertIsNone(resolve_graphical_session(run=run))


class TestLockedHint(unittest.TestCase):
    def _run_answering(self, text, returncode=0):
        def run(argv, **kwargs):
            proc = mock.Mock()
            proc.returncode = returncode
            proc.stdout = text.encode()
            return proc
        return run

    def test_true(self):
        self.assertIs(session_locked_hint("/p", run=self._run_answering("(<true>,)\n")), True)

    def test_false(self):
        self.assertIs(session_locked_hint("/p", run=self._run_answering("(<false>,)\n")), False)

    def test_failure_is_none_not_false(self):
        """None and False are opposite evidence (the gpaste_tracking rule,
        agent:1059) -- fail-open must be a visible tri-state, not a default."""
        self.assertIsNone(session_locked_hint("/p", run=self._run_answering("", 1)))
