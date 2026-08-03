"""The v3.3 fast tier: GPaste's history uuid as the change token.

Design: docs/superpowers/specs/2026-08-03-v3.3-focus-free-detection-design.md
"""
import os
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


if __name__ == "__main__":
    unittest.main()
