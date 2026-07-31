# agent/tests/test_clip_state_store.py
import os
import tempfile
import unittest

from agent_under_test import (
    clip_state_path,
    load_clip_state,
    resolve_startup_state,
    save_clip_state,
)


class TestClipStatePath(unittest.TestCase):
    """Mirrors test_clipboard.py's TestEnvironment cases for runtime_dir:
    same env-var-with-fallback shape, applied to XDG_STATE_HOME instead of
    XDG_RUNTIME_DIR."""

    def test_uses_xdg_state_home_when_present(self):
        env = dict(os.environ, XDG_STATE_HOME="/custom/state")
        self.assertEqual(clip_state_path(env), "/custom/state/clipwire/clip-state.json")

    def test_falls_back_to_home_local_state_when_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_STATE_HOME"}
        expected = os.path.join(os.path.expanduser("~/.local/state"), "clipwire", "clip-state.json")
        self.assertEqual(clip_state_path(env), expected)


class _TempPathCase(unittest.TestCase):
    """Shared setUp/tearDown for every test below that touches the
    filesystem: a fresh temp directory per test, cleaned up in tearDown so
    a failing assertion partway through a test can never skip cleanup --
    the same reasoning ClipStateStoreTests.swift's own tearDown comment
    gives for not cleaning up at the end of the test body instead.

    No test in this file ever calls clip_state_path() and lets it flow
    into load_clip_state/save_clip_state's default -- every I/O test below
    passes `path=` explicitly, so nothing here can ever touch the real
    state directory.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "clip-state.json")

    def tearDown(self):
        self._tmp.cleanup()


class TestRoundTrip(_TempPathCase):
    def test_round_trip(self):
        save_clip_state("deadbeefcafe", 1785400000.5, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), ("deadbeefcafe", 1785400000.5))

    def test_second_save_overwrites_the_first(self):
        save_clip_state("aa", 1, path=self.path)
        save_clip_state("bb", 2, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), ("bb", 2.0))

    def test_none_hash_round_trips(self):
        save_clip_state(None, 0, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (None, 0.0))


class TestLoadNeverRaises(_TempPathCase):
    def test_missing_file_loads_as_none(self):
        self.assertIsNone(load_clip_state(path=self.path), "no file at all means nothing is known yet")

    def test_unreadable_file_loads_as_none(self):
        """Mirrors testUnreadableFileLoadsAsNil: a directory in place of
        the file exists but can never be opened as a regular file,
        regardless of privilege level -- unlike chmod 0o000, which root
        bypasses and would make this flaky in CI."""
        os.makedirs(self.path)
        self.assertIsNone(load_clip_state(path=self.path),
                           "a present-but-unreadable file must read as absent, not raise")

    def test_malformed_file_loads_as_none(self):
        with open(self.path, "wb") as handle:
            handle.write(b"not json at all")
        self.assertIsNone(load_clip_state(path=self.path),
                           "a torn or corrupt state file must read as absent, never as a corrupt timestamp")

    def test_file_missing_required_key_loads_as_none(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"sha256": "aa"}')
        self.assertIsNone(load_clip_state(path=self.path),
                           "valid JSON missing the required ts key is still not a clip state")

    def test_truncated_write_loads_as_none(self):
        """The literal torn-write scenario: a crash mid-write leaves a
        syntactically incomplete JSON object (no closing brace), not a
        missing file or a competing directory. Task 7's Swift report never
        actually exercised a real tear, only argued from the shared
        atomic-write primitive -- pin the outcome directly here instead."""
        with open(self.path, "wb") as handle:
            handle.write(b'{"sha256": "aa", "ts": 17')
        self.assertIsNone(load_clip_state(path=self.path),
                           "a write torn mid-JSON must read as absent, not raise")


class TestSaveIsAtomic(_TempPathCase):
    def test_save_leaves_no_temp_file_behind(self):
        save_clip_state("aa", 1, path=self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                          "the temp file used for the atomic replace must not linger")

    def test_save_creates_intermediate_directories(self):
        nested = os.path.join(self._tmp.name, "nested", "clip-state.json")
        save_clip_state("aa", 1, path=nested)
        self.assertEqual(load_clip_state(path=nested), ("aa", 1.0))


class TestResolveStartupState(unittest.TestCase):
    """The four cases that make the wake flow work -- mirrors
    ClipStateStoreTests.swift's own MARK section, case for case."""

    def test_stored_hash_matches_current_returns_stored_timestamp_not_now(self):
        """The load-bearing case: content that has not changed since it
        was last recorded must keep its real age. stored's ts (100) and
        now (999) are deliberately different values -- a bug that returns
        `now` instead of the stored timestamp would still pass a test
        where the two happen to coincide; this one actually fails it."""
        stored = ("aa", 100)
        result = resolve_startup_state("aa", stored, 999)
        self.assertEqual(result, ("aa", 100))

    def test_stored_hash_differs_returns_now(self):
        stored = ("aa", 100)
        result = resolve_startup_state("bb", stored, 999)
        self.assertEqual(result, ("bb", 999))

    def test_nothing_stored_returns_now(self):
        result = resolve_startup_state("aa", None, 999)
        self.assertEqual(result, ("aa", 999))

    def test_current_hash_none_returns_none_hash_regardless_of_stored(self):
        """A None current hash always wins over whatever is on disk, and
        it takes precedence even when something IS stored: resolve_freshness
        never compares timestamps when either side's hash is None, so `ts`
        is unread downstream here -- this pins current behaviour (`now`)
        rather than asserting a hard requirement on its exact value."""
        stored = ("aa", 100)
        result = resolve_startup_state(None, stored, 999)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 999)

    def test_current_hash_none_and_nothing_stored_returns_none_hash(self):
        result = resolve_startup_state(None, None, 999)
        self.assertIsNone(result[0])


if __name__ == "__main__":
    unittest.main()
