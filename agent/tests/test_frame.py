import io
import json
import unittest

from agent_under_test import (
    Agent,
    OversizedFrame,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
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
            decode_frame(bytearray(b"\xff\xff\xff\xff\x7f"))

    def test_unknown_type_raises(self):
        with self.assertRaises(UnknownFrameType):
            decode_frame(bytearray(b"\x00\x00\x00\x00\x7f"))

    def test_empty_payload_is_valid(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b""))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b""))

    def test_max_payload_boundary_incomplete(self):
        # Frame declaring exactly MAX_PAYLOAD_BYTES (4194304) is incomplete, not oversized
        buffer = bytearray(b"\x00\x40\x00\x00\x00")
        self.assertIsNone(decode_frame(buffer))

    def test_max_payload_boundary_exceeded(self):
        # Frame declaring MAX_PAYLOAD_BYTES + 1 (4194305) is oversized
        buffer = bytearray(b"\x00\x40\x00\x01\x00")
        with self.assertRaises(OversizedFrame):
            decode_frame(buffer)

    def test_type_constants_are_pinned(self):
        # The golden fixtures (Task 3) round-trip whichever raw integer a type
        # carries, so a consistent relabelling of TYPE_HELLO/TYPE_CLIP would
        # stay green there. Pin the actual wire-format assignment explicitly
        # here instead.
        self.assertEqual(TYPE_HELLO, 0x00, "hello must be wire type 0x00")
        self.assertEqual(TYPE_CLIP, 0x01, "clip must be wire type 0x01")
        # Same gap, same fix, for the type this task registers.
        self.assertEqual(TYPE_CLIP_STATE, 0x02, "clip-state must be wire type 0x02")

    def test_clip_state_round_trip(self):
        # Mirrors test_round_trip for TYPE_CLIP: proves the envelope carries
        # the new type correctly. The payload here is arbitrary opaque bytes
        # -- this task registers the frame type, not the clip-state JSON
        # codec, which lands later.
        buffer = bytearray(encode_frame(TYPE_CLIP_STATE, b"state"))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP_STATE, b"state"))
        self.assertEqual(len(buffer), 0)

    def test_protocol_version_is_bumped_to_v2(self):
        self.assertEqual(PROTOCOL_VERSION, 2)

    def test_hello_payload_contains_sent_at_as_a_number(self):
        # Nothing yet consumes sent_at on receipt (that lands later), so this
        # only checks what we send: parse the built payload directly rather
        # than routing it through any peer-facing validation.
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=None)
        payload = json.loads(agent.hello_payload().decode())
        self.assertIsInstance(payload["sent_at"], float,
                               "sent_at must be a JSON number, not a string")


if __name__ == "__main__":
    unittest.main()
