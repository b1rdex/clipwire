# agent/tests/test_freshness.py
import json
import pathlib
import unittest

from agent_under_test import (
    ClipStateError,
    DO_NOTHING,
    KIND_IMAGE,
    KIND_TEXT,
    SEND_MINE,
    TYPE_CLIP_STATE,
    WAIT_FOR_PEER,
    decode_clip_state,
    decode_frame,
    encode_clip_state,
    resolve_freshness,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "freshness.json"
FRAMES_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "frames.json"

# 64 lowercase hex characters: the only shape decode_clip_state accepts,
# and the only shape hashlib.sha256(...).hexdigest() -- hence the wire --
# ever produces. Obviously fake, but well-formed, so these tests exercise
# the same path a real digest does instead of one the protocol forbids.
# See _is_sha256_hex on why that validation exists at all. HASH_A sorts
# below HASH_B, which resolve_freshness's hash tie-break depends on.
HASH_A = "aa" * 32
HASH_B = "bb" * 32


def _pair(state):
    """The (sha256, ts) shape resolve_freshness takes. Task 6 grew
    decode_clip_state to a (sha256, ts, kind) triple, and deliberately did
    NOT grow resolve_freshness to match (see its own docstring) -- so this
    stays a 2-tuple, matching fixtures/freshness.json's rows, which are
    (and must remain) kind-less by design."""
    return state["sha256"], state["ts"]


class TestFreshnessFixture(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(FIXTURES.read_text())["cases"]
        # Asserted here too, not only in the dedicated test below, so that
        # EVERY test in this class -- not just one -- is protected from
        # passing vacuously on a truncated or missing fixture file. Mirrors
        # test_fixtures.py's setUp.
        self.assertTrue(self.cases, "fixtures/freshness.json must not be empty")

    def test_fixture_file_is_not_empty(self):
        """A dedicated, explicit non-vacuousness check, distinct from the
        assertion folded into setUp() above. Pins the exact row count too,
        so a partially-truncated file (non-empty, but short) still fails."""
        self.assertTrue(self.cases, "fixtures/freshness.json must not be empty")
        self.assertEqual(len(self.cases), 8, "expected exactly 8 freshness fixture cases")

    def test_resolve_freshness_matches_fixture_table(self):
        """Drives every row of the table shared with Swift's FreshnessTests
        through resolve_freshness -- the same file, the same rule. Each row's
        mine/peer pair maps to exactly one decision, so flipping any single
        row's `expect` fails that row alone, not the suite in general.

        The fixture's `expect` values (sendMine/waitForPeer/doNothing) are
        compared directly against resolve_freshness's return value: no
        enum/lookup layer sits between them, because SEND_MINE, WAIT_FOR_PEER
        and DO_NOTHING *are* those exact strings.
        """
        for c in self.cases:
            with self.subTest(c["name"]):
                actual = resolve_freshness(_pair(c["mine"]), _pair(c["peer"]))
                self.assertEqual(actual, c["expect"], "resolve_freshness mismatch for %s" % c["name"])

    def test_decision_is_complementary_when_sides_swap(self):
        """The property the shared formula exists to guarantee: running the
        same function with mine/peer swapped must yield exact opposites --
        never both WAIT_FOR_PEER (the clip is lost forever, v1's bug) and
        never both SEND_MINE (a ping-pong). A pair of hand-mirrored
        per-language conditions could still pass
        test_resolve_freshness_matches_fixture_table in isolation while
        failing this -- exactly the class of defect this project has already
        hit twice, per the design doc.
        """
        opposite = {SEND_MINE: WAIT_FOR_PEER, WAIT_FOR_PEER: SEND_MINE, DO_NOTHING: DO_NOTHING}
        for c in self.cases:
            with self.subTest(c["name"]):
                mine_view = resolve_freshness(_pair(c["mine"]), _pair(c["peer"]))
                peer_view = resolve_freshness(_pair(c["peer"]), _pair(c["mine"]))
                self.assertEqual(peer_view, opposite[mine_view], "not complementary for %s" % c["name"])


class TestClipStateCodec(unittest.TestCase):
    def test_round_trip(self):
        digest = "deadbeefcafe0123" * 4
        encoded = encode_clip_state(digest, 1785400000.5, KIND_TEXT)
        self.assertEqual(decode_clip_state(encoded), (digest, 1785400000.5, KIND_TEXT))

    def test_round_trip_empty_hash(self):
        encoded = encode_clip_state(None, 0, None)
        self.assertEqual(decode_clip_state(encoded), (None, 0.0, None))

    def test_decodes_existing_frames_fixture(self):
        """Pins decode_clip_state against the type-2 vectors in
        fixtures/frames.json: the original null-hash/null-kind vector
        (decode-only since an earlier task, before this codec existed) and
        the real-hash/text-kind vector this task adds alongside it. Checked
        directly against the fixture file, not assumed from its shape."""
        cases = json.loads(FRAMES_FIXTURES.read_text())["cases"]
        clip_state_cases = [c for c in cases if c["type"] == TYPE_CLIP_STATE]
        self.assertEqual(len(clip_state_cases), 2, "expected exactly 2 type-2 fixture cases in frames.json")

        by_name = {c["name"]: c for c in clip_state_cases}
        empty = by_name["clip-state"]
        buffer = bytearray(bytes.fromhex(empty["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_CLIP_STATE, "clip-state fixture must decode as type 2")
        self.assertEqual(len(buffer), 0)
        self.assertEqual(decode_clip_state(payload), (None, 1.0, None))

        texted = by_name["clip-state-text"]
        buffer = bytearray(bytes.fromhex(texted["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_CLIP_STATE, "clip-state-text fixture must decode as type 2")
        self.assertEqual(len(buffer), 0)
        sha256, ts, kind = decode_clip_state(payload)
        self.assertEqual(sha256, "ab" * 32)
        self.assertEqual(ts, 1.0)
        self.assertEqual(kind, KIND_TEXT)

    def test_decode_rejects_non_finite_timestamp(self):
        """json.loads, unlike Swift's JSONDecoder, accepts a bare NaN /
        Infinity / -Infinity and hands back a non-finite float -- one that
        compares False against everything (nan > x and nan < x are both
        False; nan == nan is False too). Silently letting that reach
        resolve_freshness would compare a NaN against a real timestamp and
        send the decision somewhere neither side expects. Failing loudly
        here, before the value ever reaches a comparison, is the deliberate
        choice; silently returning it is not."""
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal):
                payload = ('{"sha256": "%s", "ts": %s}' % (HASH_A, literal)).encode()
                with self.assertRaises(ClipStateError):
                    decode_clip_state(payload)

    def test_encode_rejects_non_finite_timestamp(self):
        """The same hazard, the other direction: json.dumps happily emits a
        bare NaN token too, which would leave this side's own wire silently
        malformed by Swift's stricter reading -- breaking the *peer's*
        handshake instead of this side's. Both directions must fail loudly."""
        for bad_ts in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad_ts):
                with self.assertRaises(ClipStateError):
                    encode_clip_state(HASH_A, bad_ts, KIND_TEXT)

    def test_decode_rejects_a_sha256_that_is_not_64_lowercase_hex(self):
        """The wire contract says sha256 is `hashlib.sha256(...).hexdigest()`
        or null, and the whole cross-language comparison rests on that: this
        side orders hashes by CODE POINT, Swift's String orders them by
        canonical Unicode equivalence, and the two coincide only over
        lowercase hex.

        Unvalidated, that domain assumption escapes. Verified by execution
        rather than assumed: Python puts U+00C5 above the canonically
        equivalent "A" + U+030A, while Swift calls those two strings EQUAL.
        So a peer announcing "\\u00c5" against a local "A\\u030a" makes Swift
        resolve doNothing (hashes equal) while this side resolves
        WAIT_FOR_PEER (hashes differ, then the tie-break puts the peer's
        above ours) -- both sides wait, and the clip is lost with nothing
        logged anywhere. Hostile input only today, since both agents only
        ever put hexdigest() output on the wire; the point is that the
        branch's own principle is that both sides run ONE formula, and this
        is the domain that formula is only valid over."""
        for bad in (
            "",                    # empty
            "aa",                  # too short
            "0" * 63,              # one short of the boundary
            "0" * 65,              # one past it
            "A" * 64,              # uppercase: hexdigest() never emits it
            "g" * 64,              # right length, outside the hex alphabet
            "0" * 63 + " ",        # trailing space
            "Å" * 64,         # the composed character this fix exists for
            "Å" + "0" * 61,  # and its canonically equivalent decomposition
        ):
            with self.subTest(bad):
                payload = json.dumps({"sha256": bad, "ts": 1.0}).encode()
                with self.assertRaises(ClipStateError):
                    decode_clip_state(payload)

    def test_decode_accepts_a_real_hexdigest_and_null(self):
        """The other half: the guard above must not reject what the protocol
        actually carries. A real hexdigest, and the null that means an empty
        or unreadable clipboard."""
        real = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(decode_clip_state(encode_clip_state(real, 1.0, KIND_TEXT)), (real, 1.0, KIND_TEXT))
        self.assertEqual(decode_clip_state(encode_clip_state(None, 1.0, None)), (None, 1.0, None))

    def test_decode_rejects_missing_ts(self):
        with self.assertRaises(ClipStateError):
            decode_clip_state(('{"sha256": "%s"}' % HASH_A).encode())

    def test_decode_rejects_a_boolean_timestamp(self):
        """`bool` is a subclass of `int` in Python, so a bare
        `isinstance(ts, (int, float))` admits JSON `true` and decodes it as
        1.0 -- a real, finite, comparable timestamp from 1970, silently
        manufactured out of a field the peer controls. Swift's own decoder
        throws `typeMismatch` for the same payload, so without this guard
        the two sides disagree about whether a clip-state frame is even
        well-formed: this one would reconcile against a fabricated age
        while the Mac closes on the frame.

        skew_log_line, in this same file, already spells `isinstance(...,
        bool)` out explicitly for exactly this class of peer-controlled
        input. This mirrors it rather than inventing a second spelling."""
        for literal in (b"true", b"false"):
            with self.subTest(literal=literal):
                payload = b'{"sha256": null, "ts": ' + literal + b', "kind": null}'
                with self.assertRaises(ClipStateError):
                    decode_clip_state(payload)

    def test_decode_rejects_non_object_payload(self):
        with self.assertRaises(ClipStateError):
            decode_clip_state(b"[1, 2, 3]")

    def test_decode_rejects_malformed_json(self):
        with self.assertRaises(ClipStateError):
            decode_clip_state(b"not json")

    def test_decode_rejects_an_oversized_integer_timestamp(self):
        """The wire-path twin of test_clip_state_store.py's
        test_oversized_integer_timestamp_loads_as_none. json.loads parses an
        integer literal as arbitrary-precision int, unlike a float literal
        (1e400 already becomes inf, caught by the ordinary isfinite check).
        A 400-digit integer ts instead passes decode_clip_state's own
        isinstance(ts, (int, float)) check and only fails inside
        math.isfinite's int-to-float conversion, raising a bare
        OverflowError -- not a ClipStateError, and therefore not a
        FrameError, so main()'s `except FrameError` would not catch it.

        Until this task there was no call site for decode_clip_state whose
        input is peer-controlled: load_clip_state (test_clip_state_store.py)
        already guards its own call site by catching OverflowError there,
        because that input is this agent's own prior write to its own
        disk. The new wire call site (Agent._on_clip_state, added by this
        task) has no such local guard and none is added -- it deliberately
        mirrors _on_hello's existing bare `raise FrameError(...)`, so a
        malformed clip-state closes the connection exactly like a malformed
        hello does, via main()'s `except FrameError`. That symmetry only
        holds if decode_clip_state itself never raises anything outside the
        FrameError family -- which is what this test pins, at the source,
        rather than only at one caller."""
        oversized = ('{"sha256": "%s", "ts": 1' % HASH_A).encode() + b"0" * 400 + b"}"
        with self.assertRaises(ClipStateError):
            decode_clip_state(oversized)


class TestClipStateKind(unittest.TestCase):
    """Task 6: a hash alone cannot tell the two sides what they are
    agreeing about, so clip-state now carries a `kind`. This does NOT touch
    resolve_freshness itself -- the resolution formula is unchanged and
    still compares only (sha256, ts) pairs (see TestFreshnessFixture above,
    run unmodified against fixtures/freshness.json); `kind` is for the send
    branch (Task 11) and the log (Task 14).

    Two validation rules, enforced at decode -- the wire is peer-controlled
    input: `kind` must be None exactly when `sha256` is None, and otherwise
    must be one of the two known values, so an unknown kind can never reach
    the send branch that switches on it.
    """

    def test_round_trip_carries_the_kind(self):
        for kind in (KIND_TEXT, KIND_IMAGE):
            with self.subTest(kind=kind):
                payload = encode_clip_state("ab" * 32, 1.5, kind)
                self.assertEqual(decode_clip_state(payload), ("ab" * 32, 1.5, kind))

    def test_a_null_hash_carries_a_null_kind(self):
        payload = encode_clip_state(None, 1.5, None)
        self.assertEqual(decode_clip_state(payload), (None, 1.5, None))

    def test_an_unknown_kind_is_rejected(self):
        """Peer-controlled input. An unknown kind must not reach the send
        branch, which switches on it."""
        payload = json.dumps({"sha256": "ab" * 32, "ts": 1.5, "kind": "video"}).encode()
        with self.assertRaises(ClipStateError):
            decode_clip_state(payload)

    def test_a_hash_without_a_kind_is_rejected(self):
        payload = json.dumps({"sha256": "ab" * 32, "ts": 1.5, "kind": None}).encode()
        with self.assertRaises(ClipStateError):
            decode_clip_state(payload)

    def test_a_kind_without_a_hash_is_rejected(self):
        """The other direction of the same rule: a v2 store file (real
        hash, no kind at all) is the practical case that matters
        (test_clip_state_store.py's TestV2StoreIsRejected), but the rule
        itself is symmetric, so both directions are pinned here."""
        payload = json.dumps({"sha256": None, "ts": 1.5, "kind": "text"}).encode()
        with self.assertRaises(ClipStateError):
            decode_clip_state(payload)


if __name__ == "__main__":
    unittest.main()
