# agent/tests/test_announce_tristate.py
"""announce_clip_state under an unreadable clipboard (v3.5 spec §4.2): the
store survives, the announcement carries the true recorded age."""
import os
import tempfile
import unittest

from agent_under_test import (
    KIND_TEXT,
    READ_EMPTY,
    READ_UNKNOWN,
    announce_clip_state,
    decode_clip_state,
    load_clip_state,
    save_clip_state,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here to swap the module-level log() for a list
# appender, the same way test_clip_state_store.py's own capture_log does.
import clipwire_agent


class _Classified:
    def __init__(self, outcome, payload=None):
        self._answer = (outcome, payload)

    def read_classified(self):
        return self._answer

    def read(self):
        return self._answer[1]


class TestAnnounceUnknown(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.close()
        os.unlink(handle.name)
        self.path = handle.name
        self.addCleanup(lambda: os.path.exists(self.path) and os.unlink(self.path))
        self.sent = []

    def _send(self, frame_type, payload):
        self.sent.append((frame_type, payload))

    def capture_log(self):
        """Everything log() was asked to write during this test -- same
        module-attribute swap test_clip_state_store.py's own capture_log
        uses."""
        original = clipwire_agent.log
        lines = []
        clipwire_agent.log = lines.append
        self.addCleanup(setattr, clipwire_agent, "log", original)
        return lines

    def test_store_survives_and_is_announced_with_true_age(self):
        save_clip_state("ab" * 32, 1755400000.0, KIND_TEXT, path=self.path)
        announced = announce_clip_state(
            self._send, _Classified(READ_UNKNOWN), now=1755500000.0, path=self.path)
        self.assertEqual(announced, ("ab" * 32, 1755400000.0, KIND_TEXT, None))
        # load_clip_state returns decode_clip_state's bare tuple, never a
        # list -- matches the tuple literal every other assertion against
        # it uses across test_clip_state_store.py.
        self.assertEqual(load_clip_state(path=self.path),
                         ("ab" * 32, 1755400000.0, KIND_TEXT, None))
        decoded = decode_clip_state(self.sent[0][1])
        self.assertEqual(decoded[0], "ab" * 32)
        self.assertEqual(decoded[1], 1755400000.0)

    def test_unknown_with_no_store_announces_null_without_persisting(self):
        announced = announce_clip_state(
            self._send, _Classified(READ_UNKNOWN), now=1755500000.0, path=self.path)
        self.assertEqual(announced, (None, 1755500000.0, None, None))
        self.assertIsNone(load_clip_state(path=self.path))
        self.assertFalse(os.path.exists(self.path))

    def test_unreadable_clipboard_logs_from_the_store_not_a_change(self):
        """The spec names this line: an unreadable clipboard is not a
        reconciliation judgement (nothing was read to compare), so it gets
        its own line instead of borrowing "clipboard changed while apart"."""
        save_clip_state("ab" * 32, 1755400000.0, KIND_TEXT, path=self.path)
        lines = self.capture_log()

        announce_clip_state(
            self._send, _Classified(READ_UNKNOWN), now=1755500000.0, path=self.path)

        self.assertIn("announcing from the store: clipboard unreadable", lines)
        self.assertNotIn("clipboard changed while apart", lines)

    def test_definitely_empty_still_persists_null(self):
        """The anti-two-state test: collapsing empty into unknown must go red
        here. An empty clipboard is a fact and its announcement writes the
        store, exactly as before this cycle."""
        save_clip_state("ab" * 32, 1755400000.0, KIND_TEXT, path=self.path)
        announced = announce_clip_state(
            self._send, _Classified(READ_EMPTY), now=1755500000.0, path=self.path)
        self.assertEqual(announced, (None, 1755500000.0, None, None))
        self.assertEqual(load_clip_state(path=self.path),
                         (None, 1755500000.0, None, None))
