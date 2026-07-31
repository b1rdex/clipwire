# agent/tests/test_freshness.py
import json
import pathlib
import unittest

from agent_under_test import (
    ClipStateError,
    DO_NOTHING,
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


def _pair(state):
    """The (sha256, ts) shape both resolve_freshness and decode_clip_state share."""
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
        encoded = encode_clip_state("deadbeefcafe", 1785400000.5)
        self.assertEqual(decode_clip_state(encoded), ("deadbeefcafe", 1785400000.5))

    def test_round_trip_empty_hash(self):
        encoded = encode_clip_state(None, 0)
        self.assertEqual(decode_clip_state(encoded), (None, 0.0))

    def test_decodes_existing_frames_fixture(self):
        """Pins decode_clip_state against the *existing* `clip-state` vector
        in fixtures/frames.json -- committed decode-only back in an earlier
        task specifically because this codec did not exist yet. That vector
        is the payload this codec must decode through, checked directly
        against the fixture file rather than assumed from its shape."""
        cases = json.loads(FRAMES_FIXTURES.read_text())["cases"]
        clip_state_cases = [c for c in cases if c["type"] == TYPE_CLIP_STATE]
        self.assertEqual(len(clip_state_cases), 1, "expected exactly 1 type-2 fixture case in frames.json")
        c = clip_state_cases[0]

        buffer = bytearray(bytes.fromhex(c["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_CLIP_STATE, "clip-state fixture must decode as type 2")
        self.assertEqual(len(buffer), 0)

        sha256, ts = decode_clip_state(payload)
        self.assertIsNone(sha256, "clip-state fixture's sha256 must decode to None")
        self.assertEqual(ts, 1.0)

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
                payload = ('{"sha256": "aa", "ts": %s}' % literal).encode()
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
                    encode_clip_state("aa", bad_ts)

    def test_decode_rejects_missing_ts(self):
        with self.assertRaises(ClipStateError):
            decode_clip_state(b'{"sha256": "aa"}')

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
        oversized = b'{"sha256": "aa", "ts": 1' + b"0" * 400 + b"}"
        with self.assertRaises(ClipStateError):
            decode_clip_state(oversized)


if __name__ == "__main__":
    unittest.main()
