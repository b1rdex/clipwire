# agent/tests/test_clip_state_store.py
import hashlib
import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_under_test import (
    KIND_IMAGE,
    KIND_TEXT,
    MAX_IMAGE_BYTES,
    MAX_TEXT_BYTES,
    TIMESTAMP_BYTES,
    TYPE_CLIP_STATE,
    WAIT_FOR_PEER,
    announce_clip_state,
    clip_state_path,
    decode_clip_state,
    load_clip_state,
    resolve_current_clip_state,
    resolve_freshness,
    resolve_startup_state,
    save_clip_state,
    sha256_hex,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here to swap the module-level log() for a list
# appender, the same way test_frame.py and test_watcher.py already do.
import clipwire_agent

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
        save_clip_state(digest, 1785400000.5, KIND_TEXT, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (digest, 1785400000.5, KIND_TEXT, None))

    def test_second_save_overwrites_the_first(self):
        """Different kinds on the two saves, not just different hashes: the
        second save must overwrite kind too, not only sha256/ts."""
        save_clip_state(HASH_A, 1, KIND_TEXT, path=self.path)
        save_clip_state(HASH_B, 2, KIND_IMAGE, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (HASH_B, 2.0, KIND_IMAGE, None))

    def test_none_hash_round_trips(self):
        save_clip_state(None, 0, None, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), (None, 0.0, None, None))

    def test_the_store_round_trips_the_kind(self):
        save_clip_state("cd" * 32, 9.0, KIND_IMAGE, path=self.path)
        self.assertEqual(load_clip_state(path=self.path), ("cd" * 32, 9.0, KIND_IMAGE, None))


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
        save_clip_state(HASH_A, 1, KIND_TEXT, path=self.path)
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                          "the temp file used for the atomic replace must not linger")

    def test_save_creates_intermediate_directories(self):
        nested = os.path.join(self._tmp.name, "nested", "clip-state.json")
        save_clip_state(HASH_A, 1, KIND_TEXT, path=nested)
        self.assertEqual(load_clip_state(path=nested), (HASH_A, 1.0, KIND_TEXT, None))


class TestSaveIsSerialized(_TempPathCase):
    """save_clip_state has five call sites across four methods, and since
    the safety-net poll landed they run on up to three threads: _write_clip
    and announce_clip_state on run()'s thread, _observe_local_change (once
    on its text path and once on its image one) and _consume_image_reoffer
    on the watcher threads. All of them write the SAME
    `target + ".tmp"` and then os.replace it, so two concurrent savers
    truncate one another's temp file and whichever replace runs second finds
    it already consumed.

    The consequence is already on record: a half-written temp moved into
    place makes load_clip_state return None, so the next connection stamps
    ts=now on old content and wins a reconciliation it should lose -- a
    silent clipboard clobber, the exact failure protocol v2 exists to
    prevent. All four call sites swallow the exception, so nothing surfaces
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

        def save(sha, ts, kind, done=None):
            try:
                save_clip_state(sha, ts, kind, path=self.path)
            except Exception as error:  # the swallowed production failure, surfaced
                errors.append(error)
            if done is not None:
                done.set()

        first = threading.Thread(target=save, args=(HASH_A, 1, KIND_TEXT))
        second = threading.Thread(target=save, args=(HASH_B, 2, KIND_IMAGE),
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
    ClipStateStoreTests.swift's own MARK section, case for case.

    current_kind is deliberately set to a WRONG guess in every case below
    where the rule says it must be ignored (the stored-hash-matches case,
    and the None-hash case), and to KIND_IMAGE -- deliberately not the
    KIND_TEXT everything else in this file defaults to -- in every case
    where the rule says it must be threaded through. Either direction, a
    bug that used the wrong source for the returned kind cannot pass by
    coincidence."""

    def test_stored_hash_matches_current_returns_stored_timestamp_not_now(self):
        """The load-bearing case: content that has not changed since it
        was last recorded must keep its real age -- and, by the same
        reasoning, its real kind: unchanged content did not change what
        kind of content it is either. stored's ts (100) and now (999) are
        deliberately different values -- a bug that returns `now` instead
        of the stored timestamp would still pass a test where the two
        happen to coincide; this one actually fails it. current_kind is
        passed as KIND_TEXT, the wrong answer for this stored row, so a
        bug that returned current_kind instead of the stored one cannot
        pass by coincidence either."""
        stored = ("aa", 100, KIND_IMAGE)
        result = resolve_startup_state("aa", KIND_TEXT, stored, 999)
        self.assertEqual(result, ("aa", 100, KIND_IMAGE))

    def test_stored_hash_differs_returns_the_reads_own_kind(self):
        """Content that changed while apart is read fresh, and Task 7 made
        that fresh read kind-aware: the kind returned here must be
        current_kind, the kind that SAME read reported -- not a hardcoded
        KIND_TEXT guess (the bug this task exists to prevent; see the
        integration-level reproduction of it in
        test_clip_state_store.py::TestResolveCurrentClipState::test_content_differing_from_the_store_uses_the_reads_own_kind).
        KIND_IMAGE here, against a stored KIND_TEXT, is what makes this
        assertion distinguish "threaded through" from "copied from
        stored"."""
        stored = ("aa", 100, KIND_TEXT)
        result = resolve_startup_state("bb", KIND_IMAGE, stored, 999)
        self.assertEqual(result, ("bb", 999, KIND_IMAGE))

    def test_nothing_stored_returns_the_reads_own_kind(self):
        """Same rule as the row above, for the OTHER branch that reaches
        `return current_hash, now, current_kind`: nothing on disk is not
        distinguishable, from this function's point of view, from stored
        content that no longer matches -- both mean "trust the read"."""
        result = resolve_startup_state("aa", KIND_IMAGE, None, 999)
        self.assertEqual(result, ("aa", 999, KIND_IMAGE))

    def test_current_hash_none_returns_none_hash_and_none_kind_regardless_of_stored_or_current_kind(self):
        """A None current hash always wins over whatever is on disk, and
        it takes precedence even when something IS stored: resolve_freshness
        never compares timestamps when either side's hash is None, so `ts`
        is unread downstream here -- this pins current behaviour (`now`)
        rather than asserting a hard requirement on its exact value.
        current_kind is passed as KIND_TEXT -- not None -- specifically to
        prove the returned kind is a literal None, not current_kind passed
        through: an empty/unreadable clipboard has no real kind to report,
        whatever a caller mistakenly supplied for it."""
        stored = ("aa", 100, KIND_TEXT)
        result = resolve_startup_state(None, KIND_TEXT, stored, 999)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 999)
        self.assertIsNone(result[2], "a null hash must carry a null kind")

    def test_current_hash_none_and_nothing_stored_returns_none_hash(self):
        result = resolve_startup_state(None, None, None, 999)
        self.assertIsNone(result[0])


class FixedReadClipboard:
    """A clipboard double whose read() always returns the same fixed value
    -- unlike test_watcher.py's QueueClipboard, which pops a scripted
    sequence. resolve_current_clip_state reads the clipboard exactly once
    per call, so either double would work here; this one is used so a test
    that calls it more than once (never needed today, but cheap to keep
    true) still sees the SAME clipboard content each time, matching how a
    real clipboard behaves between two reads with nothing in between.

    `value` is whatever WaylandClipboard.read() itself would return: a
    (kind, bytes) pair, or None. Passed through completely unshaped -- this
    double does no wrapping of its own, so a test's own construction call
    is the one place that says what kind of read is being simulated."""

    def __init__(self, value):
        self._value = value

    def read(self):
        return self._value


class TestResolveCurrentClipState(unittest.TestCase):
    """Mirrors Sources/clipwire/ClipStateStore.swift's
    resolveCurrentClipState / ClipStateStoreTests.swift coverage of it:
    reconciles what the clipboard holds RIGHT NOW against what was last
    persisted. A None read, or a pair whose body is empty, must never be
    hashed -- it must resolve exactly like resolve_startup_state's own
    None-hash case, matching the wire contract that sha256 is null for
    exactly that clipboard state."""

    def test_empty_clipboard_resolves_to_a_none_hash(self):
        clipboard = FixedReadClipboard(None)
        result = resolve_current_clip_state(clipboard, stored=("aa", 100), now=999)
        self.assertIsNone(result[0])
        self.assertEqual(result[1], 999)

    def test_blank_but_non_none_clipboard_also_resolves_to_a_none_hash(self):
        """(KIND_TEXT, b"") is a pair whose BODY is falsy but not None --
        resolve_current_clip_state must treat it the same as a totally
        empty clipboard (read() returning None outright), not hash zero
        bytes and treat that as real content. In production
        WaylandClipboard.read() never actually returns this shape (an
        empty body always collapses to a bare None, see its own
        docstring), but the resolver stays defensive against a double that
        does, exactly as it already was before Task 7."""
        clipboard = FixedReadClipboard((KIND_TEXT, b""))
        result = resolve_current_clip_state(clipboard, stored=None, now=999)
        self.assertIsNone(result[0])
        self.assertIsNone(result[2], "a null hash must carry a null kind here too")

    def test_content_matching_the_store_keeps_the_stored_timestamp(self):
        """The load-bearing case: unchanged content must keep its true
        recorded age -- and its true recorded kind -- not look freshly
        copied (or of some other kind) just because a new process is
        asking. now (999) and the stored ts (100) are deliberately
        different, so a bug that returns `now` instead cannot pass by
        coincidence; KIND_IMAGE in `stored` (against a KIND_TEXT read,
        the actual kind of `text`) is deliberately the wrong guess for
        "the kind this connection just read off the clipboard", so a bug
        that overwrote the stored kind with the freshly-read KIND_TEXT
        cannot pass by coincidence either."""
        text = b"unchanged clip"
        clipboard = FixedReadClipboard((KIND_TEXT, text))
        stored = (hashlib.sha256(text).hexdigest(), 100, KIND_IMAGE)
        result = resolve_current_clip_state(clipboard, stored, now=999)
        self.assertEqual(result, (stored[0], 100, KIND_IMAGE))

    def test_content_differing_from_the_store_uses_now(self):
        """New content observed here comes from clipboard.read(), which
        Task 7 made kind-aware -- so the kind it resolves must be the
        read's OWN kind, not an assumed KIND_TEXT. See
        test_content_differing_from_the_store_uses_the_reads_own_kind
        immediately below for the case that actually distinguishes the
        two (this one uses KIND_TEXT for both, so it cannot)."""
        clipboard = FixedReadClipboard((KIND_TEXT, b"brand new content"))
        stored = ("some-other-hash-entirely", 100, KIND_TEXT)
        result = resolve_current_clip_state(clipboard, stored, now=999)
        self.assertEqual(
            result,
            (hashlib.sha256(b"brand new content").hexdigest(), 999, KIND_TEXT),
        )

    def test_content_differing_from_the_store_uses_the_reads_own_kind(self):
        """The regression this task exists to prevent, reproduced directly:
        resolve_startup_state's docstring warns that a mechanical
        adaptation of THIS function (`current_hash = sha256_hex(read[1])`,
        the new kind quietly dropped on the floor) runs clean and passes
        the whole suite while silently fabricating KIND_TEXT for a PNG
        hash. stored deliberately describes a DIFFERENT kind (KIND_TEXT)
        than what is read (KIND_IMAGE), so a bug that just reused stored's
        kind, or hardcoded KIND_TEXT, cannot pass by coincidence either."""
        png = b"\x89PNG-a-screenshot"
        clipboard = FixedReadClipboard((KIND_IMAGE, png))
        stored = ("some-other-hash-entirely", 100, KIND_TEXT)
        result = resolve_current_clip_state(clipboard, stored, now=999)
        self.assertEqual(result, (sha256_hex(png), 999, KIND_IMAGE))

    def test_nothing_stored_uses_now(self):
        clipboard = FixedReadClipboard((KIND_TEXT, b"first time seeing this"))
        result = resolve_current_clip_state(clipboard, stored=None, now=999)
        self.assertEqual(
            result,
            (hashlib.sha256(b"first time seeing this").hexdigest(), 999, KIND_TEXT),
        )


class TestOversizedContentIsNotAnnounced(unittest.TestCase):
    """The announce path had no size cap on either machine, and the two
    guards it lacked are the two the SENDERS already apply.

    The failure it produced, traced end to end on the Mac and identical in
    shape here: the local observation path skips an oversized clip with a
    log and returns BEFORE persisting anything, so the store keeps its
    older entry -- but resolve_current_clip_state then hashed the oversized
    body with no cap at all, resolve_startup_state saw a hash differing
    from the store and stamped `now`, and announce_clip_state persisted and
    announced it. That announcement wins reconciliation against anything
    the peer copied earlier, and the send branch then refuses the very
    content it just won with. The peer, having correctly resolved
    waitForPeer, has already suppressed its own push -- so a perfectly
    sendable clip on the peer never arrives. That is precisely the wake
    flow protocol v2 exists to serve.

    The remedy is a null hash, which resolve_freshness already handles:
    "we hold nothing announceable" makes the peer win and deliver.

    The predicates are the senders' own, character for character --
    `len(text) + TIMESTAMP_BYTES > MAX_TEXT_BYTES` for text and
    `len(png) > MAX_IMAGE_BYTES` for an image -- so "announceable" and
    "sendable" cannot diverge. The asymmetry between them is not a slip:
    MAX_IMAGE_BYTES bounds the IMAGE (an image at exactly the limit is
    legal and encodes to a payload 8 bytes over it, still far inside the
    frame cap), while MAX_TEXT_BYTES bounds the encoded text clip.
    """

    def capture_log(self):
        original = clipwire_agent.log
        lines = []
        clipwire_agent.log = lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original)
        return lines

    def test_an_oversized_image_resolves_a_null_hash_and_kind(self):
        lines = self.capture_log()
        oversized = b"\x89" * (MAX_IMAGE_BYTES + 1)

        result = resolve_current_clip_state(
            FixedReadClipboard((KIND_IMAGE, oversized)), stored=None, now=999)

        self.assertEqual(result, (None, 999, None),
                         "content this side can never send must be announced as nothing, "
                         "not hashed and stamped `now`")
        self.assertIn(
            "not announcing an image of %d bytes: over the image limit" % len(oversized),
            lines,
            "a silent skip is how a user concludes the tool is broken")

    def test_an_image_at_exactly_the_limit_is_still_announced(self):
        """The boundary the three separated caps exist to permit. Written
        as `len(png) + TIMESTAMP_BYTES > MAX_IMAGE_BYTES` this would refuse
        exactly the maximum-size screenshot the sender accepts, and the two
        would disagree about one image."""
        lines = self.capture_log()
        exact = b"\x89" * MAX_IMAGE_BYTES

        result = resolve_current_clip_state(
            FixedReadClipboard((KIND_IMAGE, exact)), stored=None, now=999)

        self.assertEqual(result, (sha256_hex(exact), 999, KIND_IMAGE))
        self.assertEqual(lines, [], "nothing was skipped, so nothing is worth a line")

    def test_oversized_text_resolves_a_null_hash_and_kind(self):
        """Text at exactly MAX_TEXT_BYTES is already over: it is wrapped in
        an 8-byte timestamp before it reaches the wire, so the sender's own
        guard refuses it, and this one must refuse the same byte count."""
        lines = self.capture_log()
        oversized = b"x" * MAX_TEXT_BYTES

        result = resolve_current_clip_state(
            FixedReadClipboard((KIND_TEXT, oversized)), stored=None, now=999)

        self.assertEqual(result, (None, 999, None))
        self.assertIn(
            "not announcing a clip of %d bytes: over the text limit" % len(oversized),
            lines)

    def test_text_at_exactly_the_sendable_boundary_is_still_announced(self):
        exact = b"x" * (MAX_TEXT_BYTES - TIMESTAMP_BYTES)
        lines = self.capture_log()

        result = resolve_current_clip_state(
            FixedReadClipboard((KIND_TEXT, exact)), stored=None, now=999)

        self.assertEqual(result, (sha256_hex(exact), 999, KIND_TEXT))
        self.assertEqual(lines, [])

    def test_the_peer_wins_instead_of_being_locked_out(self):
        """The whole point, stated as the decision both sides actually run.
        Before the guard, `mine` carried a real hash stamped `now` and beat
        the peer's older-but-sendable clip; the peer then waited for a frame
        the send branch had already refused to build."""
        oversized = b"\x89" * (MAX_IMAGE_BYTES + 1)
        self.capture_log()

        mine = resolve_current_clip_state(
            FixedReadClipboard((KIND_IMAGE, oversized)), stored=None, now=999999)

        self.assertEqual(resolve_freshness(mine[:2], (HASH_A, 100)), WAIT_FOR_PEER,
                         "a clip we cannot send must never win against one the peer can")

    def test_an_oversized_clipboard_overwrites_the_store_with_nothing(self):
        """announce_clip_state persists whatever it resolved, and that must
        be the null state here rather than the older entry left behind by
        the observation path's own skip. A store still describing content
        the clipboard no longer offers is the "store goes stale" disease
        this whole design exists to close: the next connection would find
        it, see a hash the clipboard does not hold, and stamp `now` on it.

        And the reconciliation line stays silent, because a null hash never
        reaches a timestamp comparison -- there is no judgement to report.
        """
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "clip-state.json")
        save_clip_state(HASH_B, 111, KIND_TEXT, path=path)
        lines = self.capture_log()
        sent = []

        announce_clip_state(
            lambda t, p: sent.append((t, p)),
            FixedReadClipboard((KIND_IMAGE, b"\x89" * (MAX_IMAGE_BYTES + 1))),
            now=999999, path=path)

        self.assertEqual(load_clip_state(path=path), (None, 999999, None, None))
        self.assertEqual(decode_clip_state(sent[0][1]), (None, 999999, None, None))
        self.assertNotIn("clipboard changed while apart", lines)


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

    def capture_log(self):
        """Everything log() was asked to write during this test. Same
        module-attribute swap the rest of the suite uses."""
        original = clipwire_agent.log
        lines = []
        clipwire_agent.log = lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original)
        return lines

    def test_logs_when_the_clipboard_changed_while_apart(self):
        """The design doc mandates this line by name where startup
        reconciliation takes the ts = now branch, and nothing implemented it
        on either side. Two things depend on it: acceptance item 2 requires
        the conflict to APPEAR IN THE LOG, and the design's one accepted
        trade-off -- the side whose agent was born more recently wins -- is
        justified on the grounds of being "visible in the log rather than
        mysterious". Without this line, that mitigation does not exist."""
        save_clip_state(HASH_B, 111, KIND_TEXT, path=self.path)
        lines = self.capture_log()

        announce_clip_state(lambda t, p: None, FixedReadClipboard((KIND_TEXT, b"new content")),
                            now=999999, path=self.path)

        self.assertIn("clipboard changed while apart", lines)

    def test_logs_when_nothing_was_ever_stored(self):
        """"Nothing on disk" is the same branch: the content appeared while
        nothing was watching, and only now is honest about its age."""
        lines = self.capture_log()

        announce_clip_state(lambda t, p: None, FixedReadClipboard((KIND_TEXT, b"first ever content")),
                            now=42, path=self.path)

        self.assertIn("clipboard changed while apart", lines)

    def test_does_not_log_when_the_stored_state_is_still_authoritative(self):
        """The complement, and the one that keeps the line meaningful: the
        stored hash still matches, so nothing changed while apart and the
        stored timestamp is authoritative. A line here on every reconnect
        would teach everyone to ignore it."""
        text = b"unchanged"
        save_clip_state(sha256_hex(text), 555, KIND_TEXT, path=self.path)
        lines = self.capture_log()

        announce_clip_state(lambda t, p: None, FixedReadClipboard((KIND_TEXT, text)),
                            now=999999, path=self.path)

        self.assertNotIn("clipboard changed while apart", lines)

    def test_does_not_log_for_an_empty_clipboard(self):
        """A null hash never reaches a timestamp comparison at all --
        resolve_freshness refuses to compare timestamps when either side's
        hash is None -- so there is no reconciliation judgement to report."""
        lines = self.capture_log()

        announce_clip_state(lambda t, p: None, FixedReadClipboard(None),
                            now=42, path=self.path)

        self.assertNotIn("clipboard changed while apart", lines)

    def test_keeps_stored_timestamp_when_content_is_unchanged(self):
        text = b"same"
        save_clip_state(sha256_hex(text), 555, KIND_TEXT, path=self.path)
        sent = []

        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard((KIND_TEXT, text)),
                            now=999999, path=self.path)

        self.assertEqual(len(sent), 1)
        decoded = decode_clip_state(sent[0][1])
        self.assertEqual(decoded[1], 555, "content unchanged since last recorded must keep its real age")

    def test_uses_now_when_content_changed_while_apart(self):
        save_clip_state(HASH_B, 111, KIND_TEXT, path=self.path)
        sent = []

        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard((KIND_TEXT, b"new content")),
                            now=999999, path=self.path)

        decoded = decode_clip_state(sent[0][1])
        self.assertEqual(decoded[1], 999999, "content changed while apart -- only now is honest")

    def test_persists_the_resolved_value(self):
        """So a later .clipState comparison (or a crash immediately
        afterward) sees the reconciled value, not whatever was on disk
        before this connection began."""
        sent = []
        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard((KIND_TEXT, b"fresh content")),
                            now=42, path=self.path)

        self.assertEqual(load_clip_state(path=self.path), (sha256_hex(b"fresh content"), 42.0, KIND_TEXT, None))

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
            save_clip_state(HASH_A, 1, KIND_TEXT, path=unsaveable_path)

        sent = []
        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard((KIND_TEXT, b"still send this")),
                            now=1, path=unsaveable_path)

        self.assertEqual(len(sent), 1,
                         "a local disk failure must not prevent the announcement from going out")

    def test_an_image_only_clipboard_announces_its_real_kind_not_a_fabricated_text(self):
        """The end-to-end proof, one level closer to production than
        TestResolveCurrentClipState's own direct-call version: an
        image-only clipboard (no text at all -- Task 7's whole point is
        that this no longer reads back as an empty clipboard) must
        announce kind: "image" on the wire, not silently drop the kind and
        announce (or persist) a PNG's hash mislabelled as text. This is
        exactly the store-corruption half of the bug resolve_startup_state's
        docstring warns about: a fabricated KIND_TEXT here would be
        WRITTEN to the store by the save_clip_state call below, not just
        wrong in memory for one connection."""
        png = b"\x89PNG-a-screenshot-only-clipboard"
        sent = []

        announce_clip_state(lambda t, p: sent.append((t, p)), FixedReadClipboard((KIND_IMAGE, png)),
                            now=42, path=self.path)

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0], TYPE_CLIP_STATE)
        decoded = decode_clip_state(sent[0][1])
        self.assertEqual(decoded, (sha256_hex(png), 42.0, KIND_IMAGE, None))
        self.assertEqual(
            load_clip_state(path=self.path), (sha256_hex(png), 42.0, KIND_IMAGE, None),
            "the persisted store must carry the real kind too, not just the sent frame",
        )


class TestV2StoreIsRejected(_TempPathCase):
    """protocol v3 adds `kind` to the clip-state wire AND store format
    (this task). A store file written by a v2 agent -- a real scenario
    after any upgrade, not a hypothetical -- has a real (non-null) sha256
    and no `kind` key at all.

    load_clip_state already shares decode_clip_state's validation (see
    load_clip_state's own docstring), so the same "kind must be null
    exactly when sha256 is null" rule that rejects a malformed WIRE payload
    also rejects this file: dict.get("kind") returns None whether the key
    is absent or explicitly null, so a non-null sha256 paired with an
    absent kind fails that equivalence exactly as a wire payload missing
    the pairing would. Silently accepting it instead -- as "a hash of
    unknown kind" -- would feed resolve_startup_state, and eventually the
    send branch (Task 11), a state with no kind to act on. Pinned directly
    here rather than left to be inferred from decode_clip_state's own
    tests, since this is the concrete situation the rule exists for."""

    def test_a_v2_store_file_is_rejected_not_loaded_as_kindless(self):
        with open(self.path, "wb") as handle:
            handle.write(('{"sha256": "%s", "ts": 100}' % HASH_A).encode())
        self.assertIsNone(
            load_clip_state(path=self.path),
            "a v2 store (real hash, no kind key) must be rejected, not "
            "silently treated as a hash of unknown kind",
        )


if __name__ == "__main__":
    unittest.main()
