# agent/tests/test_clip_state_store.py
import hashlib
import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_under_test import (
    announce_clip_state,
    clip_state_path,
    decode_clip_state,
    load_clip_state,
    resolve_current_clip_state,
    resolve_startup_state,
    save_clip_state,
    sha256_hex,
)

JOIN_TIMEOUT = 2  # generous relative to the millisecond-scale waits below

# 64 lowercase hex characters: the only shape decode_clip_state accepts,
# and the only shape hashlib.sha256(...).hexdigest() -- hence the wire --
# ever produces. Obviously fake, but well-formed, so these tests exercise
# the same path a real digest does instead of one the protocol forbids.
# See _is_sha256_hex on why that validation exists at all. HASH_A sorts
# below HASH_B, which resolve_freshness's hash tie-break depends on.
HASH_A = "aa" * 32
HASH_B = "bb" * 32


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
        digest = "deadbeefcafe0123" * 4
        save_clip_state(digest, 1785400000.5, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (digest, 1785400000.5))

    def test_second_save_overwrites_the_first(self):
        save_clip_state(HASH_A, 1, path=self.path)
        save_clip_state(HASH_B, 2, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (HASH_B, 2.0))

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
            handle.write(('{"sha256": "%s"}' % HASH_A).encode())
        self.assertIsNone(load_clip_state(path=self.path),
                           "valid JSON missing the required ts key is still not a clip state")

    def test_oversized_integer_timestamp_loads_as_none(self):
        """json.loads parses integer literals as arbitrary-precision int, so
        a 400-digit ts sails past decode_clip_state's
        isinstance(ts, (int, float)) check and only fails once
        math.isfinite(ts) tries to convert it to a float -- raising
        OverflowError, not ClipStateError. The float form of the same
        hazard (`1e400`) is already safe: json.loads gives back `inf`
        directly, isfinite reports False, and decode_clip_state raises its
        own ClipStateError. Only the integer literal form takes a
        different path through the standard library and needs its own
        test."""
        with open(self.path, "wb") as handle:
            handle.write(('{"sha256": "%s", "ts": 1' % HASH_A).encode() + b"0" * 400 + b"}")
        self.assertIsNone(load_clip_state(path=self.path),
                           "an oversized ts must read as absent, not raise OverflowError")

    def test_truncated_write_loads_as_none(self):
        """The literal torn-write scenario: a crash mid-write leaves a
        syntactically incomplete JSON object (no closing brace), not a
        missing file or a competing directory. Task 7's Swift report never
        actually exercised a real tear, only argued from the shared
        atomic-write primitive -- pin the outcome directly here instead."""
        with open(self.path, "wb") as handle:
            handle.write(('{"sha256": "%s", "ts": 17' % HASH_A).encode())
        self.assertIsNone(load_clip_state(path=self.path),
                           "a write torn mid-JSON must read as absent, not raise")


class TestSaveIsAtomic(_TempPathCase):
    def test_save_leaves_no_temp_file_behind(self):
        save_clip_state(HASH_A, 1, path=self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                          "the temp file used for the atomic replace must not linger")

    def test_save_creates_intermediate_directories(self):
        nested = os.path.join(self._tmp.name, "nested", "clip-state.json")
        save_clip_state(HASH_A, 1, path=nested)
        self.assertEqual(load_clip_state(path=nested), (HASH_A, 1.0))


class TestSaveIsSerialized(_TempPathCase):
    """save_clip_state has three call sites, and since the safety-net poll
    landed they run on up to three threads: _write_clip and
    announce_clip_state on run()'s thread, _local_change on the watcher
    threads. All three write the SAME `target + ".tmp"` and then os.replace
    it, so two concurrent savers truncate one another's temp file and
    whichever replace runs second finds it already consumed.

    The consequence is already on record: a half-written temp moved into
    place makes load_clip_state return None, so the next connection stamps
    ts=now on old content and wins a reconciliation it should lose -- a
    silent clipboard clobber, the exact failure protocol v2 exists to
    prevent. All three call sites swallow the exception, so nothing surfaces
    either.

    Mirrors ClipStateStore.save's NSLock on the Swift side, fixed for this in
    an earlier task; the Python half of the same defect was never done.
    ClipStateStoreTests.swift documents why it did NOT ship a
    timing-threshold assertion for it (real overlap between the locked and
    unlocked distributions). This test needs no threshold: it observes
    whether the two critical sections OVERLAP, which is a yes/no fact, and
    pins the resulting exception directly."""

    def test_a_second_saver_cannot_enter_while_the_first_is_inside(self):
        real_replace = os.replace
        events = []
        errors = []
        first_inside = threading.Event()
        second_done = threading.Event()

        def gated_replace(src, dst):
            if dst != self.path:
                return real_replace(src, dst)
            events.append("enter")
            if len(events) == 1:
                first_inside.set()
                # Released the moment the second saver FINISHES, which can only
                # happen while the first is still in here if the two are not
                # serialized. Serialized, this is a short bounded wait and the
                # second saver is still parked outside when it expires.
                second_done.wait(0.02)
            real_replace(src, dst)
            events.append("leave")

        def save(sha, ts, done=None):
            try:
                save_clip_state(sha, ts, path=self.path)
            except Exception as error:  # the swallowed production failure, surfaced
                errors.append(error)
            if done is not None:
                done.set()

        first = threading.Thread(target=save, args=(HASH_A, 1))
        second = threading.Thread(target=save, args=(HASH_B, 2),
                                  kwargs={"done": second_done})
        with mock.patch("os.replace", gated_replace):
            first.start()
            self.assertTrue(first_inside.wait(JOIN_TIMEOUT),
                            "the first saver never reached the replace")
            second.start()
            first.join(timeout=JOIN_TIMEOUT)
            second.join(timeout=JOIN_TIMEOUT)

        self.assertEqual(
            errors, [],
            "a concurrent saver must not race the shared temp file out from "
            "under the other; got %r" % (errors,),
        )
        self.assertEqual(
            events, ["enter", "leave", "enter", "leave"],
            "the two saves must not overlap inside the write-and-replace section",
        )
        self.assertIsNotNone(
            load_clip_state(path=self.path),
            "concurrent saves must never leave a file that fails to load",
        )


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


class FixedReadClipboard:
    """A clipboard double whose read() always returns the same fixed value
    -- unlike test_watcher.py's QueueClipboard, which pops a scripted
    sequence. resolve_current_clip_state reads the clipboard exactly once
    per call, so either double would work here; this one is used so a test
    that calls it more than once (never needed today, but cheap to keep
    true) still sees the SAME clipboard content each time, matching how a
    real clipboard behaves between two reads with nothing in between."""

    def __init__(self, value):
        self._value = value

    def read(self):
        return self._value


class TestResolveCurrentClipState(unittest.TestCase):
    """Mirrors Sources/clipwire/ClipStateStore.swift's
    resolveCurrentClipState / ClipStateStoreTests.swift coverage of it:
    reconciles what the clipboard holds RIGHT NOW against what was last
    persisted. A None or empty clipboard must never be hashed -- it must
    resolve exactly like resolve_startup_state's own None-hash case,
    matching the wire contract that sha256 is null for exactly that
    clipboard state."""

    def test_empty_clipboard_resolves_to_a_none_hash(self):
        clipboard = FixedReadClipboard(None)
        result = resolve_current_clip_state(clipboard, stored=("aa", 100), now=999)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 999)

    def test_blank_but_non_none_clipboard_also_resolves_to_a_none_hash(self):
        """b"" is falsy but not None -- resolve_current_clip_state must
        treat it the same as a totally empty clipboard, not hash zero
        bytes and treat that as real content."""
        clipboard = FixedReadClipboard(b"")
        result = resolve_current_clip_state(clipboard, stored=None, now=999)
        self.assertIsNone(result[0])

    def test_content_matching_the_store_keeps_the_stored_timestamp(self):
        """The load-bearing case: unchanged content must keep its true
        recorded age, not look freshly copied just because a new process
        is asking. now (999) and the stored ts (100) are deliberately
        different, so a bug that returns `now` instead cannot pass by
        coincidence."""
        text = b"unchanged clip"
        clipboard = FixedReadClipboard(text)
        stored = (hashlib.sha256(text).hexdigest(), 100)
        result = resolve_current_clip_state(clipboard, stored, now=999)
        self.assertEqual(result, (stored[0], 100))

    def test_content_differing_from_the_store_uses_now(self):
        clipboard = FixedReadClipboard(b"brand new content")
        stored = ("some-other-hash-entirely", 100)
        result = resolve_current_clip_state(clipboard, stored, now=999)
        self.assertEqual(
            result,
            (hashlib.sha256(b"brand new content").hexdigest(), 999),
        )

    def test_nothing_stored_uses_now(self):
        clipboard = FixedReadClipboard(b"first time seeing this")
        result = resolve_current_clip_state(clipboard, stored=None, now=999)
        self.assertEqual(
            result,
            (hashlib.sha256(b"first time seeing this").hexdigest(), 999),
        )


class TestAnnounceClipState(unittest.TestCase):
    """announce_clip_state, tested directly against the free function rather
    than through Agent.clipboard_became_ready -- mirrors
    HandleFrameTests.swift's "announceClipState: the outgoing announcement,
    tested directly" section, for the same reason: the load-bearing cases
    need control over `now` that going through the full phase machine would
    obscure."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = os.path.join(self._tmp.name, "clip-state.json")

    def test_keeps_stored_timestamp_when_content_is_unchanged(self):
        text = b"same"
        save_clip_state(sha256_hex(text), 555, path=self.path)
        sent = []

        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard(text),
                            now=999999, path=self.path)

        self.assertEqual(len(sent), 1)
        decoded = decode_clip_state(sent[0][1])
        self.assertEqual(decoded[1], 555, "content unchanged since last recorded must keep its real age")

    def test_uses_now_when_content_changed_while_apart(self):
        save_clip_state(HASH_B, 111, path=self.path)
        sent = []

        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard(b"new content"),
                            now=999999, path=self.path)

        decoded = decode_clip_state(sent[0][1])
        self.assertEqual(decoded[1], 999999, "content changed while apart -- only now is honest")

    def test_persists_the_resolved_value(self):
        """So a later .clipState comparison (or a crash immediately
        afterward) sees the reconciled value, not whatever was on disk
        before this connection began."""
        sent = []
        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard(b"fresh content"),
                            now=42, path=self.path)

        self.assertEqual(load_clip_state(path=self.path), (sha256_hex(b"fresh content"), 42.0))

    def test_still_sends_when_the_store_cannot_be_saved(self):
        """A local disk failure is not the peer's fault, and must not
        silently disable reconciliation for this connection. Forces a real
        save failure (a plain file occupying the path where the store needs
        to create a directory) rather than asserting this from reading the
        implementation, mirroring
        HandleFrameTests.testAnnounceClipStateStillSendsWhenTheStoreCannotBeSaved."""
        blocker = os.path.join(self._tmp.name, "blocker")
        with open(blocker, "wb") as handle:
            handle.write(b"occupying this name")
        unsaveable_path = os.path.join(blocker, "clip-state.json")
        # Confirm the setup actually forces a failure, or this test proves nothing.
        with self.assertRaises(OSError):
            save_clip_state(HASH_A, 1, path=unsaveable_path)

        sent = []
        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard(b"still send this"),
                            now=1, path=unsaveable_path)

        self.assertEqual(len(sent), 1,
                         "a local disk failure must not prevent the announcement from going out")


if __name__ == "__main__":
    unittest.main()
