# agent/tests/test_clip_payload.py
import struct
import unittest

from agent_under_test import (
    ClipPayloadError,
    decode_clip_payload,
    encode_clip_payload,
)


class TestClipPayload(unittest.TestCase):
    def test_round_trip(self):
        blob = encode_clip_payload(1785400000.5, "привет 🔥".encode())
        ts, text = decode_clip_payload(blob)
        self.assertEqual(ts, 1785400000.5)
        self.assertEqual(text, "привет 🔥".encode())

    def test_layout_is_timestamp_then_bytes(self):
        blob = encode_clip_payload(1.0, b"hi")
        self.assertEqual(blob[:8], struct.pack(">d", 1.0))
        self.assertEqual(blob[:8], bytes([0x3F, 0xF0, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(blob[8:], b"hi")

    def test_empty_text_is_representable(self):
        self.assertEqual(decode_clip_payload(encode_clip_payload(5.0, b"")), (5.0, b""))

    def test_too_short_raises(self):
        with self.assertRaises(ClipPayloadError):
            decode_clip_payload(b"\x00\x01\x02")

    def test_arbitrary_bytes_survive(self):
        """Text is carried as bytes; nothing may normalise or re-encode it."""
        raw = b"e\xcc\x81\r\n\x00tail"          # NFD e-acute, CRLF, an embedded NUL
        self.assertEqual(decode_clip_payload(encode_clip_payload(1.0, raw))[1], raw)


if __name__ == "__main__":
    unittest.main()
