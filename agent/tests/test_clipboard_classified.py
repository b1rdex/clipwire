# agent/tests/test_clipboard_classified.py
"""The tri-state read boundary (v3.5 spec §4.2): a failed read is not an
empty clipboard. Shard follows the v3.2.1 split convention."""
import unittest
from unittest import mock

from agent_under_test import (
    KIND_TEXT,
    READ_CONTENT,
    READ_EMPTY,
    READ_UNKNOWN,
    WaylandClipboard,
    classify_read,
)


def _completed(returncode, stdout=b"", stderr=b""):
    proc = mock.Mock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class TestReadClassified(unittest.TestCase):
    def setUp(self):
        self.clipboard = WaylandClipboard()

    def test_listing_subprocess_failure_is_unknown(self):
        """_run_wl_paste returns None on timeout/missing binary/OSError --
        the three cases the incident made expensive to conflate with empty."""
        with mock.patch.object(self.clipboard, "_run_wl_paste", return_value=None):
            self.assertEqual(self.clipboard.read_classified(), (READ_UNKNOWN, None))

    def test_no_selection_is_definitely_empty(self):
        """A genuinely empty clipboard is wl-paste exiting non-zero with the
        no-selection signature (spec §4.2 after review): it must announce and
        persist null, or the freshness recovery for a fresh login dies.
        Signature SYNTHETIC until Task 8 measures it."""
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               return_value=_completed(1, stderr=b"No selection\n")):
            self.assertEqual(self.clipboard.read_classified(), (READ_EMPTY, None))

    def test_listing_nonzero_exit_without_signature_is_unknown(self):
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               return_value=_completed(1, stderr=b"compositor said no\n")):
            self.assertEqual(self.clipboard.read_classified(), (READ_UNKNOWN, None))

    def test_no_usable_kind_is_definitely_empty(self):
        """A successful listing offering nothing syncable IS an empty
        clipboard -- announcing and persisting null stays correct there."""
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               return_value=_completed(0, b"audio/x-riff\n")):
            self.assertEqual(self.clipboard.read_classified(), (READ_EMPTY, None))

    def test_body_fetch_failure_is_unknown(self):
        listing = _completed(0, b"text/plain;charset=utf-8\n")
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               side_effect=[listing, None]):
            self.assertEqual(self.clipboard.read_classified(), (READ_UNKNOWN, None))

    def test_empty_body_is_empty(self):
        listing = _completed(0, b"text/plain;charset=utf-8\n")
        body = _completed(0, b"")
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               side_effect=[listing, body]):
            self.assertEqual(self.clipboard.read_classified(), (READ_EMPTY, None))

    def test_content_carries_kind_and_bytes(self):
        listing = _completed(0, b"text/plain;charset=utf-8\n")
        body = _completed(0, b"hello")
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               side_effect=[listing, body]):
            self.assertEqual(self.clipboard.read_classified(),
                             (READ_CONTENT, (KIND_TEXT, b"hello")))

    def test_read_drops_the_outcome_and_keeps_todays_contract(self):
        with mock.patch.object(self.clipboard, "_run_wl_paste", return_value=None):
            self.assertIsNone(self.clipboard.read())
        listing = _completed(0, b"text/plain;charset=utf-8\n")
        body = _completed(0, b"hello")
        with mock.patch.object(self.clipboard, "_run_wl_paste",
                               side_effect=[listing, body]):
            self.assertEqual(self.clipboard.read(), (KIND_TEXT, b"hello"))


class TestClassifyReadFallback(unittest.TestCase):
    def test_legacy_fake_none_maps_to_empty(self):
        """Every existing test double keeps its meaning: a read() of None was
        'empty' before this cycle and stays 'empty' through the helper."""
        fake = mock.Mock(spec=["read"])
        fake.read.return_value = None
        self.assertEqual(classify_read(fake), (READ_EMPTY, None))

    def test_legacy_fake_payload_maps_to_content(self):
        fake = mock.Mock(spec=["read"])
        fake.read.return_value = (KIND_TEXT, b"x")
        self.assertEqual(classify_read(fake), (READ_CONTENT, (KIND_TEXT, b"x")))

    def test_classified_clipboard_is_used_directly(self):
        fake = mock.Mock(spec=["read", "read_classified"])
        fake.read_classified.return_value = (READ_UNKNOWN, None)
        self.assertEqual(classify_read(fake), (READ_UNKNOWN, None))
        fake.read.assert_not_called()


class TestNeverReadyClassified(unittest.TestCase):
    def test_never_ready_reads_unknown(self):
        from agent_under_test import NeverReadyClipboard
        self.assertEqual(NeverReadyClipboard().read_classified(),
                         (READ_UNKNOWN, None))
