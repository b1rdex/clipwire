import json
import pathlib
import unittest

from agent_under_test import (
    TYPE_CLIP_STATE,
    decode_clip_payload,
    decode_frame,
    decode_image_payload,
    encode_frame,
    sha256_hex,
)

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "frames.json"
HASH_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "hashes.json"


class TestFixtures(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(FIXTURES.read_text())["cases"]
        self.assertTrue(self.cases, "fixture file must not be empty")

    def test_encode_matches_golden(self):
        for c in self.cases:
            with self.subTest(c["name"]):
                encoded = encode_frame(c["type"], bytes.fromhex(c["payload_hex"]))
                self.assertEqual(encoded.hex(), c["frame_hex"])

    def test_decode_matches_golden(self):
        for c in self.cases:
            with self.subTest(c["name"]):
                buffer = bytearray(bytes.fromhex(c["frame_hex"]))
                frame_type, payload = decode_frame(buffer)
                self.assertEqual(frame_type, c["type"])
                self.assertEqual(payload.hex(), c["payload_hex"])
                self.assertEqual(len(buffer), 0)

    def test_clip_payload_decodes_golden(self):
        """Pins the fixtures' type-1 payloads against the clip payload codec
        too, not just the frame envelope -- otherwise the fixture file pins
        the envelope while the new [ts][text] inner layout drifts freely."""
        clip_cases = [c for c in self.cases if c["type"] == 1]
        self.assertEqual(len(clip_cases), 7, "expected exactly 7 type-1 fixture cases")
        for c in clip_cases:
            with self.subTest(c["name"]):
                payload = bytes.fromhex(c["payload_hex"])
                ts, text = decode_clip_payload(payload)
                self.assertEqual(ts, 1.0)
                self.assertEqual(text, payload[8:])

        # An independent, hardcoded pin -- not derived from payload_hex by
        # slicing, so it cannot pass merely by symmetry with the encoder.
        ascii_case = next(c for c in self.cases if c["name"] == "ascii-clip")
        ts, text = decode_clip_payload(bytes.fromhex(ascii_case["payload_hex"]))
        self.assertEqual(ts, 1.0)
        self.assertEqual(text, b"hi")

    def test_clip_state_payload_decodes_golden(self):
        """Pinned decode-only, same as hello: JSON key order and float
        formatting differ between Swift and Python, so a byte-exact encode
        vector would fail for reasons that have nothing to do with the
        protocol. Decodes the frame envelope (proving the type byte is
        really 2, not just that some payload was found) and then parses each
        payload's JSON directly, asserting the full (sha256, ts, kind) triple
        for both the null-hash/null-kind row and the real-hash/text-kind row
        Task 6 added alongside it."""
        clip_state_cases = [c for c in self.cases if c["type"] == TYPE_CLIP_STATE]
        self.assertEqual(len(clip_state_cases), 2, "expected exactly 2 type-2 fixture cases")
        by_name = {c["name"]: c for c in clip_state_cases}

        empty = by_name["clip-state"]
        buffer = bytearray(bytes.fromhex(empty["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_CLIP_STATE, "clip-state fixture must decode as type 2")
        self.assertEqual(len(buffer), 0)
        parsed = json.loads(payload.decode())
        self.assertIsNone(parsed["sha256"], "sha256 must parse to null")
        self.assertEqual(parsed["ts"], 1.0)
        self.assertIsNone(parsed["kind"], "a null hash must carry a null kind")

        texted = by_name["clip-state-text"]
        buffer = bytearray(bytes.fromhex(texted["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_CLIP_STATE, "clip-state-text fixture must decode as type 2")
        self.assertEqual(len(buffer), 0)
        parsed = json.loads(payload.decode())
        self.assertEqual(parsed["sha256"], "ab" * 32)
        self.assertEqual(parsed["ts"], 1.0)
        self.assertEqual(parsed["kind"], "text")

    def test_image_payload_decodes_golden(self):
        """Pins the fixtures' type-3 payload against the image payload codec
        too, not just the frame envelope. Filtered and asserted against the
        literal 3, not TYPE_IMAGE_CLIP: a vector that routed the type byte
        through the constant and back could not catch that constant being
        relabelled, which was a real defect in v1 (see task-5-brief.md)."""
        image_cases = [c for c in self.cases if c["type"] == 3]
        self.assertEqual(len(image_cases), 1, "expected exactly 1 type-3 fixture case")
        c = image_cases[0]

        buffer = bytearray(bytes.fromhex(c["frame_hex"]))
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, 3, "image-clip fixture must decode as type 3")
        self.assertEqual(len(buffer), 0)

        ts, png = decode_image_payload(payload)
        self.assertEqual(ts, 1785400000.5)
        # An independent, hardcoded pin -- not derived from payload_hex by
        # slicing, so it cannot pass merely by symmetry with the encoder.
        self.assertEqual(png, bytes.fromhex("89504e470d0a1a0a0000000d49484452"))


class TestHashFixtures(unittest.TestCase):
    """Shared with Swift's FixtureTests.testSha256HexMatchesSharedVectors --
    the same file, the same vectors, so neither implementation can drift
    from the other's idea of what sha256_hex/sha256Hex should produce.

    This alone does not catch a call site that hashes the WRONG bytes (a
    ts-prefixed wire payload, or a re-encoded str) -- it only pins
    sha256_hex in isolation. test_watcher.py's
    TestWriteClipDecodesTheWirePayload.test_stores_the_literal_known_hash_for_a_pinned_vector
    and its _local_change twin close that gap, by pinning a literal digest
    at the actual call site rather than re-deriving it from this same
    function."""

    def setUp(self):
        self.cases = json.loads(HASH_FIXTURES.read_text())["cases"]
        self.assertTrue(self.cases, "fixtures/hashes.json must not be empty")

    def test_sha256_hex_matches_shared_vectors(self):
        for c in self.cases:
            with self.subTest(c["name"]):
                self.assertEqual(sha256_hex(bytes.fromhex(c["input_hex"])), c["sha256"])


if __name__ == "__main__":
    unittest.main()
