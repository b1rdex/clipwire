"""The v3.3 fast tier: GPaste's history uuid as the change token.

Design: docs/superpowers/specs/2026-08-03-v3.3-focus-free-detection-design.md
"""
import os
import subprocess
import unittest

import agent_under_test as agent


class TestIntervalInjection(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("CLIPWIRE_FAST_TIER_SECONDS", None)

    def test_defaults_when_unset(self):
        self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 5.0)

    def test_reads_a_float(self):
        os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = "0.05"
        self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 0.05)

    def test_junk_falls_back_to_the_default(self):
        """A typo in a harness must not silently produce a zero-second poll
        that spins a core, nor a crash on a machine where the variable was
        never meant to be read at all."""
        for junk in ("", "abc", "-1", "0"):
            os.environ["CLIPWIRE_FAST_TIER_SECONDS"] = junk
            self.assertEqual(agent._env_seconds("CLIPWIRE_FAST_TIER_SECONDS", 5.0), 5.0,
                             "%r was not rejected" % junk)


class TestHistoryUuidProbe(unittest.TestCase):
    def probe(self, stdout=b"", returncode=0, raises=None):
        def run(argv, **kwargs):
            if raises is not None:
                raise raises
            return subprocess.CompletedProcess(argv, returncode, stdout, b"")
        return agent.gpaste_history_uuid(run=run)

    def test_parses_the_uuid_from_a_real_shaped_reply(self):
        self.assertEqual(
            self.probe(b"('ecf318fd-a295-40a8-b591-9721fcf7cbcc', 'CD-3167')\n"),
            "ecf318fd-a295-40a8-b591-9721fcf7cbcc")

    def test_a_nonzero_exit_is_not_measured(self):
        """A well-formed reply, not empty stdout: b"" independently trips the
        `len(parts) < 2` parse guard regardless of returncode, so an empty
        payload here could not tell this guard apart from that one -- fix
        round 1 finding. A real gdbus non-zero exit does not print the
        method's reply at all, but this deliberately gives it one anyway, so
        a `returncode != 0` check that gets deleted is caught HERE rather
        than only coincidentally by whatever stdout happens to be empty."""
        self.assertIsNone(self.probe(b"('would-be-uuid', 'text')\n", returncode=1))

    def test_a_timeout_is_not_measured(self):
        self.assertIsNone(self.probe(
            raises=subprocess.TimeoutExpired(["gdbus"], 3)))

    def test_a_missing_gdbus_is_not_measured(self):
        self.assertIsNone(self.probe(raises=FileNotFoundError()))

    def test_unparseable_output_is_not_measured(self):
        """A renamed method or a changed reply shape must read as 'unknown',
        never as a uuid and never as a crash -- spec 4.0.1.

        The first four values below all contain no "'" at all, so every one
        of them is rejected by the parser's `len(parts) < 2` guard alone --
        none reaches the `not parts[1]` guard beside it. `('', 'text')`
        (an empty first field between two real quotes) is the one value here
        that DOES reach `len(parts) >= 2`, with parts[1] == "". Delete the
        `not parts[1]` half of the check and this loop still passes for the
        first four junk values and only catches the regression because this
        fifth one is here -- verified by deleting that check locally and
        watching this test stay green until this value was added."""
        for junk in (b"", b"()\n", b"no quotes here\n", b"(uint64 0,)\n",
                     b"('', 'text')\n"):
            self.assertIsNone(self.probe(junk), "%r parsed as a uuid" % junk)

    def test_the_payload_never_reaches_the_return_value(self):
        """Spec 5.2: only the uuid may be retained."""
        uuid = self.probe(b"('abc-def', 'hunter2 the password')\n")
        self.assertEqual(uuid, "abc-def")
        self.assertNotIn("hunter2", uuid)


if __name__ == "__main__":
    unittest.main()
