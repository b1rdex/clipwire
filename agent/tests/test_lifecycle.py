# agent/tests/test_lifecycle.py
import io
import unittest
from unittest import mock

from agent_under_test import (
    Agent,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_HELLO,
    decode_frame,
)

# agent_under_test registers the loaded module under this name in
# sys.modules; grabbed here only to patch make_watcher the same way
# test_watcher.py does, so these lifecycle tests never spawn a real gdbus
# subprocess or leave a polling thread running after the test ends.
import clipwire_agent


class _NoOpWatcher:
    """A watcher double for lifecycle tests that only care about the phase
    machine (clipboard_became_ready()/clipboard_lost()), not the watcher
    itself. Without this, calling clipboard_became_ready() unpatched runs
    the real make_watcher(), which either spawns a real `gdbus introspect`
    (up to 3s if it hangs) or falls back to a PollingWatcher whose
    background thread is never stopped unless the test also calls
    clipboard_lost() -- a leaked daemon thread polling forever."""

    def start(self, on_change):
        pass

    def stop(self):
        pass


class FakeClipboard:
    def __init__(self, ready=False):
        self._ready = ready
        self.written = []

    def ready(self):
        return self._ready

    def become_ready(self):
        self._ready = True

    def read(self):
        return None

    def write(self, data):
        self.written.append(data)


class TestLifecycle(unittest.TestCase):
    def build(self, ready=False):
        clipboard = FakeClipboard(ready=ready)
        out = io.BytesIO()
        return Agent(stdin=io.BytesIO(), stdout=out, clipboard=clipboard), clipboard, out

    def test_hello_is_sent_before_clipboard_is_ready(self):
        agent, _, out = self.build(ready=False)
        agent.send_hello()
        buffer = bytearray(out.getvalue())
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_HELLO)
        self.assertEqual(json_of(payload)["protocol"], PROTOCOL_VERSION)

    def test_starts_in_clipboard_pending(self):
        agent, _, _ = self.build(ready=False)
        self.assertEqual(agent.phase, "clipboard-pending")

    def test_clip_arriving_while_pending_is_not_written(self):
        agent, clipboard, _ = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, b"early")
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.pending_clip, b"early")

    def test_only_the_newest_pending_clip_survives(self):
        agent, clipboard, _ = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, b"first")
        agent.on_frame(TYPE_CLIP, b"second")
        agent.on_frame(TYPE_CLIP, b"third")
        clipboard.become_ready()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [b"third"])

    def test_becoming_ready_without_a_pending_clip_writes_nothing(self):
        agent, clipboard, _ = self.build(ready=False)
        clipboard.become_ready()
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.phase, "ready")

    def test_clip_when_ready_is_written_immediately(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, b"now")
        self.assertEqual(clipboard.written, [b"now"])
        self.assertIsNone(agent.pending_clip)

    def test_empty_clip_is_never_written(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, b"")
        self.assertEqual(clipboard.written, [])

    def test_losing_the_clipboard_returns_to_pending(self):
        agent, clipboard, _ = self.build(ready=True)
        with mock.patch.object(clipwire_agent, "make_watcher", return_value=_NoOpWatcher()):
            agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertEqual(agent.phase, "clipboard-pending")
        agent.on_frame(TYPE_CLIP, b"during outage")
        self.assertEqual(clipboard.written, [], "must not write while the session is gone")


def json_of(payload):
    import json
    return json.loads(payload.decode())


if __name__ == "__main__":
    unittest.main()
