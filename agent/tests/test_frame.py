import unittest

from agent_under_test import (
    OversizedFrame,
    TYPE_CLIP,
    UnknownFrameType,
    decode_frame,
    encode_frame,
)


class TestFrame(unittest.TestCase):
    def test_header_layout(self):
        self.assertEqual(
            encode_frame(TYPE_CLIP, b"hi"), b"\x00\x00\x00\x02\x01hi"
        )

    def test_round_trip(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, "привет".encode()))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, "привет".encode()))
        self.assertEqual(len(buffer), 0)

    def test_partial_frame_returns_none(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b"hi"))[:-1]
        self.assertIsNone(decode_frame(buffer))
        self.assertEqual(len(buffer), 6, "an incomplete frame must not be consumed")

    def test_two_frames_in_one_buffer(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b"a"))
        buffer += encode_frame(TYPE_CLIP, b"b")
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b"a"))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b"b"))
        self.assertIsNone(decode_frame(buffer))

    def test_oversized_length_raises(self):
        with self.assertRaises(OversizedFrame):
            decode_frame(bytearray(b"\xff\xff\xff\xff\x01"))

    def test_unknown_type_raises(self):
        with self.assertRaises(UnknownFrameType):
            decode_frame(bytearray(b"\x00\x00\x00\x00\x7f"))

    def test_empty_payload_is_valid(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b""))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b""))


if __name__ == "__main__":
    unittest.main()
