import io
import json
import time
import unittest

from agent_under_test import (
    Agent,
    KIND_IMAGE,
    MAX_IMAGE_BYTES,
    MAX_PAYLOAD_BYTES,
    MAX_TEXT_BYTES,
    OversizedFrame,
    PROTOCOL_VERSION,
    SKEW_WARN_SECONDS,
    TIMESTAMP_BYTES,
    TYPE_CLIP,
    TYPE_CLIP_STATE,
    TYPE_HELLO,
    TYPE_IMAGE_CLIP,
    UnknownFrameType,
    _KNOWN_TYPES,
    decode_frame,
    encode_clip_payload,
    encode_frame,
    encode_image_payload,
    skew_log_line,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here to swap the module-level log() for a list
# appender, the same way test_mainloop.py and test_watcher.py already do.
import clipwire_agent


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
        # Frame declaring exactly MAX_PAYLOAD_BYTES (8388608, this task's new
        # frame cap -- see TestV3Constants) is incomplete, not oversized
        buffer = bytearray(b"\x00\x80\x00\x00\x00")
        self.assertIsNone(decode_frame(buffer))

    def test_max_payload_boundary_exceeded(self):
        # Frame declaring MAX_PAYLOAD_BYTES + 1 (8388609) is oversized
        buffer = bytearray(b"\x00\x80\x00\x01\x00")
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

    def test_protocol_version_is_bumped_to_v3(self):
        self.assertEqual(PROTOCOL_VERSION, 3)

    def test_hello_payload_contains_sent_at_as_a_number(self):
        # Checks only what we SEND: parse the built payload directly rather
        # than routing it through any peer-facing validation. What we do with
        # a peer's sent_at on receipt is TestSkewLogLine/TestHelloSkew below.
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=None)
        payload = json.loads(agent.hello_payload().decode())
        self.assertIsInstance(payload["sent_at"], float,
                               "sent_at must be a JSON number, not a string")


class TestV3Constants(unittest.TestCase):
    """Task 4: the frame cap and the two content limits are three separate
    numbers that happen to share two values today (see the comment on the
    constants themselves in clipwire-agent.py). Bare names here, not an
    `agent.` prefix -- unlike test_watcher.py (which binds the local name
    `agent` to `Agent(...)` instances at a dozen call sites), this file
    imports individual names from agent_under_test and uses them bare, and
    there is no module-level `agent` alias here to hang a dotted lookup off.
    """

    def test_the_frame_cap_is_larger_than_either_content_limit(self):
        self.assertEqual(MAX_PAYLOAD_BYTES, 8388608)
        self.assertEqual(MAX_TEXT_BYTES, 4194304)
        self.assertEqual(MAX_IMAGE_BYTES, 4194304)
        self.assertGreater(MAX_PAYLOAD_BYTES,
                           MAX_IMAGE_BYTES + TIMESTAMP_BYTES,
                           "a maximum-size image plus its ts must fit in a frame")

    def test_the_image_clip_type_is_known(self):
        self.assertEqual(TYPE_IMAGE_CLIP, 0x03)
        self.assertIn(TYPE_IMAGE_CLIP, _KNOWN_TYPES)

    def test_the_protocol_version_is_three(self):
        self.assertEqual(PROTOCOL_VERSION, 3)


class TestImageClipDispatch(unittest.TestCase):
    """Adding TYPE_IMAGE_CLIP to _KNOWN_TYPES removed decode_frame's
    UnknownFrameType raise for 0x03: before that, a stray 0x03 tore the
    connection down loudly, caught by main()'s `except FrameError` and
    logged as "protocol error: ...". on_frame's dispatch is an if/elif with
    no else, so a byte that decode_frame now hands over and on_frame has no
    branch for vanishes in total silence -- nothing reaches stderr, the one
    stream this agent's diagnostics depend on.

    That hole was held shut by a placeholder log line until the dispatch
    became real. This test is what stops it reopening: it asserts the frame
    reaches a handler that DOES something with it, which a dropped branch
    cannot fake. What the handler then does with an image is
    test_lifecycle.py's TestImagesEndToEndOnThePC; all this class asks is
    that 0x03 is dispatched at all.

    The pending phase is the shape that answers it without a clipboard:
    this agent starts PHASE_PENDING (no Wayland session yet -- the ordinary
    state on any reconnect before login), where every inbound clip is
    queued rather than applied."""

    def test_an_image_clip_reaches_a_handler_rather_than_being_dropped(self):
        agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=None)
        sent = []
        agent.send = lambda t, p: sent.append((t, p))
        payload = encode_image_payload(1000.0, b"\x89PNG-from-the-peer")

        agent.on_frame(TYPE_IMAGE_CLIP, payload)

        self.assertEqual(sent, [], "an inbound clip is applied, never answered")
        self.assertEqual(agent.pending_clip, payload,
                         "0x03 must reach the clip handler, not fall off the dispatch")
        self.assertEqual(agent.pending_clip_kind, KIND_IMAGE,
                         "and carry the codec it belongs to, since the two wire "
                         "shapes are indistinguishable")


class TestSkewLogLine(unittest.TestCase):
    """The pure half of skew reporting: value in, log line (or None) out.

    Mirrors Sources/clipwire/main.swift's skewLogLine, whose own tests in
    AgentStatusTests.swift assert the same strings -- the two log lines are
    meant to be byte-identical, the way the two "over the text limit" lines
    already are.
    """

    def test_a_small_difference_is_reported_without_a_warning(self):
        self.assertEqual(skew_log_line(1000.0, 1000.5), "peer clock skew 0.5s")

    def test_the_quantity_is_absolute_so_direction_does_not_matter(self):
        # A peer ahead of us and a peer behind us by the same amount read
        # identically: abs(now - sent_at), not a signed difference.
        self.assertEqual(skew_log_line(1000.5, 1000.0),
                         skew_log_line(1000.0, 1000.5))

    def test_above_the_threshold_warns(self):
        self.assertEqual(skew_log_line(1000.0, 1006.0),
                         "peer clock skew 6.0s — over 5s, check the clock on both machines")

    def test_exactly_at_the_threshold_does_not_warn(self):
        # "Warn ABOVE five seconds": the boundary itself is not a warning.
        self.assertEqual(skew_log_line(1000.0, 1005.0), "peer clock skew 5.0s")

    def test_the_warning_text_quotes_the_threshold_constant(self):
        # The threshold is written into the message as a literal (no second
        # float-formatting bridge to keep byte-identical with Swift), so pin
        # the literal against the constant here instead.
        self.assertEqual(SKEW_WARN_SECONDS, 5.0)
        self.assertIn("over %ds" % int(SKEW_WARN_SECONDS),
                      skew_log_line(0.0, 1000.0))

    def test_an_unmeasurable_sent_at_reports_nothing_and_does_not_raise(self):
        """Missing, null, non-numeric and non-finite all mean the same thing:
        skew cannot be measured. Say nothing -- an unmeasurable peer clock is
        not a protocol violation, so it must not warn and must not raise.

        The non-finite cases are reachable from the wire: json.loads accepts
        the bare literals NaN/Infinity/-Infinity by default (Swift's
        JSONDecoder rejects them, which is why this guard lives here and not
        only there), and a 400-digit integer literal parses as an
        arbitrary-precision int that cannot be converted to a float at all.
        """
        for value in (None, "1000.0", [], {}, True, False,
                      float("nan"), float("inf"), float("-inf"),
                      10 ** 400, -(10 ** 400)):
            with self.subTest(value=value):
                self.assertIsNone(skew_log_line(value, 1000.0))


class TestHelloSkew(unittest.TestCase):
    """The wiring half: a received hello reports skew, and nothing else does."""

    def setUp(self):
        # run() and _on_hello call the module-level log() by its bare name,
        # resolved from clipwire_agent's globals at call time -- so replacing
        # the module attribute captures every call without touching
        # process-wide sys.stderr.
        original_log = clipwire_agent.log
        self.log_lines = []
        clipwire_agent.log = self.log_lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original_log)
        self.agent = Agent(stdin=io.BytesIO(), stdout=io.BytesIO(), clipboard=None)

    def _hello(self, **fields):
        payload = {"protocol": PROTOCOL_VERSION, "agent": "0.1.0"}
        payload.update(fields)
        return json.dumps(payload).encode()

    def _skew_lines(self):
        return [line for line in self.log_lines if "skew" in line]

    def test_a_matched_hello_logs_the_skew_against_the_injected_now(self):
        # `now` is injected rather than read from the wall clock: with
        # time.time() this assertion would be a race against the machine it
        # runs on, and a sleep would only make it slower, not deterministic.
        self.agent._on_hello(self._hello(sent_at=1000.0), now=1000.5)
        self.assertEqual(self._skew_lines(), ["peer clock skew 0.5s"])

    def test_the_wall_clock_default_is_used_when_now_is_not_injected(self):
        """The one branch production always takes, and the only one every
        other test here would leave unpinned.

        run() dispatches through on_frame, which deliberately does not thread
        `now` -- so the real agent always reaches `now = time.time()`. Every
        other test in this class passes `now=` explicitly and would stay green
        with that fallback deleted, while the real Mac (which always sends
        sent_at) would hand skew_log_line a None `now`: TypeError, caught
        neither by skew_log_line's `except OverflowError` nor by main()'s
        `except FrameError`, so the channel is torn down on the first hello
        of every connection -- exactly what Ruling 2 exists to prevent.

        Deterministic without a sleep and without a mock: sent_at is read
        from the same clock the fallback reads, microseconds earlier, so the
        measured skew is far below the threshold on any machine. The
        assertion is on the SHAPE (one line, not a warning), never on an
        exact duration.
        """
        self.agent._on_hello(self._hello(sent_at=time.time()))   # no now=
        self.assertEqual(len(self._skew_lines()), 1,
                         "a hello carrying sent_at must report skew against the "
                         "wall clock when no now is injected")
        self.assertNotIn("over 5s", self._skew_lines()[0],
                         "this machine's clock against itself is not a skew warning")

    def test_a_badly_skewed_peer_warns(self):
        self.agent._on_hello(self._hello(sent_at=1000.0), now=1060.0)
        self.assertEqual(
            self._skew_lines(),
            ["peer clock skew 60.0s — over 5s, check the clock on both machines"],
        )

    def test_a_hello_without_sent_at_is_accepted_in_silence(self):
        # A v1 peer, or any hand-built payload. Not measurable, not a
        # violation: no line, and specifically no FrameError -- raising here
        # would tear down the connection over a missing optional field.
        self.agent._on_hello(self._hello(), now=1000.0)
        self.assertEqual(self._skew_lines(), [])

    def test_a_null_or_non_finite_sent_at_is_accepted_in_silence(self):
        # json.dumps(float("nan")) emits the bare literal NaN, which
        # json.loads accepts on the way back in -- so this is a payload a
        # (broken) peer really can put on the wire.
        for raw in (b'{"protocol": %d, "sent_at": null}' % PROTOCOL_VERSION,
                    b'{"protocol": %d, "sent_at": NaN}' % PROTOCOL_VERSION,
                    b'{"protocol": %d, "sent_at": Infinity}' % PROTOCOL_VERSION,
                    b'{"protocol": %d, "sent_at": "1000.0"}' % PROTOCOL_VERSION,
                    b'{"protocol": %d, "sent_at": 1%s}' % (PROTOCOL_VERSION, b"0" * 400)):
            with self.subTest(raw=raw):
                del self.log_lines[:]
                self.agent._on_hello(raw, now=1000.0)   # must not raise
                self.assertEqual(self._skew_lines(), [])

    def test_a_mismatched_hello_does_not_report_skew(self):
        # The mismatch is the story; a clock reading from a peer we are
        # about to hang up on is noise.
        with self.assertRaises(clipwire_agent.FrameError):
            self.agent._on_hello(self._hello(protocol=999, sent_at=1000.0), now=1060.0)
        self.assertEqual(self._skew_lines(), [])

    def test_an_old_clip_after_a_current_hello_does_not_warn(self):
        """The anti-requirement, and the whole point of measuring sent_at
        rather than the clip's own timestamp: a clip legitimately copied
        yesterday is a day old, and warning on THAT would fire on nearly
        every handshake and teach everyone to ignore the log.
        """
        now = 1_000_000.0
        self.agent._on_hello(self._hello(sent_at=now), now=now)
        self.agent.on_frame(TYPE_CLIP,
                            encode_clip_payload(now - 86400.0, "copied yesterday".encode()))
        self.assertEqual(self._skew_lines(), ["peer clock skew 0.0s"],
                         "the clip's age must not be measured as clock skew")


if __name__ == "__main__":
    unittest.main()
