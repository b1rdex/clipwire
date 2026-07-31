import json
import pathlib
import unittest

from agent_under_test import decode_clip_payload, decode_frame, encode_frame

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "frames.json"


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
        self.assertEqual(len(clip_cases), 6, "expected exactly 6 type-1 fixture cases")
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


if __name__ == "__main__":
    unittest.main()
