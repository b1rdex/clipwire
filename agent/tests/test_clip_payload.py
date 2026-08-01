# agent/tests/test_clip_payload.py
import struct
import unittest

from agent_under_test import (
    ClipPayloadError,
    decode_clip_payload,
    decode_image_payload,
    encode_clip_payload,
    encode_image_payload,
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

    def test_non_finite_timestamp_raises(self):
        """Fix round 1, Finding 2: struct.unpack(">d", ...) happily decodes
        ANY 8-byte pattern into inf/-inf/nan -- unlike decode_clip_state,
        which already guards ts finiteness, decode_clip_payload had no such
        check at all. The one unguarded caller this bites: Agent._write_clip
        writes the clip to the clipboard and (best-effort) persists it, but
        clipboard_became_ready's own follow-up
        encode_clip_state(*applied_pending) call -- reusing exactly this ts
        -- has no local try/except, so a non-finite ts, undetected here,
        would raise ClipStateError there and tear down the connection over
        a clip that had ALREADY been applied. Fixed at the source both
        callers share, following the same precedent as decode_clip_state's
        own OverflowError fix: reject it here, before it ever reaches a
        downstream encoder that assumes it's already finite."""
        for ts in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(ts):
                payload = struct.pack(">d", ts) + b"text"
                with self.assertRaises(ClipPayloadError):
                    decode_clip_payload(payload)


class TestImagePayload(unittest.TestCase):
    """type-0x03 payload: [f64 BE ts][PNG bytes]. Same shape as the text clip
    and for the same reason -- see encode_image_payload's own docstring.

    Bare names imported from agent_under_test, not an `agent.` prefix: this
    file has no Agent(...) instance to hang a dotted lookup off, matching
    TestClipPayload just above and TestV3Constants's own note in
    test_frame.py (test_watcher.py is the one file where `agent` is a local
    variable bound to Agent(...), not a module).
    """

    def test_round_trip(self):
        png = b"\x89PNG\r\n\x1a\n" + b"body"
        ts, body = decode_image_payload(encode_image_payload(1785400000.5, png))
        self.assertEqual(ts, 1785400000.5)
        self.assertEqual(body, png)

    def test_an_empty_body_is_rejected(self):
        with self.assertRaises(ClipPayloadError):
            decode_image_payload(encode_image_payload(1.0, b""))

    def test_a_payload_shorter_than_the_timestamp_is_rejected(self):
        with self.assertRaises(ClipPayloadError):
            decode_image_payload(b"\x00\x00\x00")

    def test_a_non_finite_ts_is_rejected_on_both_sides(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(ts=bad):
                with self.assertRaises(ClipPayloadError):
                    encode_image_payload(bad, b"x")
        # and on decode, since the wire is peer-controlled
        payload = struct.pack(">d", float("nan")) + b"x"
        with self.assertRaises(ClipPayloadError):
            decode_image_payload(payload)


if __name__ == "__main__":
    unittest.main()
