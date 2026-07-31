import json
import pathlib
import unittest

from agent_under_test import decode_frame, encode_frame

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


if __name__ == "__main__":
    unittest.main()
