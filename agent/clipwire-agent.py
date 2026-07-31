#!/usr/bin/env python3
"""clipwire PC-side agent. Spawned by sshd; speaks frames on stdin/stdout.

Only frames go to stdout. Everything else goes to stderr — a stray print()
on stdout desynchronises the protocol.
"""

MAX_PAYLOAD_BYTES = 4194304
HEADER_BYTES = 5
TYPE_HELLO = 0x00
TYPE_CLIP = 0x01
PROTOCOL_VERSION = 1
_KNOWN_TYPES = (TYPE_HELLO, TYPE_CLIP)


class FrameError(Exception):
    pass


class OversizedFrame(FrameError):
    pass


class UnknownFrameType(FrameError):
    pass


def encode_frame(frame_type, payload):
    return len(payload).to_bytes(4, "big") + bytes([frame_type]) + payload


def decode_frame(buffer):
    """Consume one frame from the front of `buffer`.

    Returns (type, payload), or None when the buffer holds no complete frame.
    """
    if len(buffer) < HEADER_BYTES:
        return None
    length = int.from_bytes(buffer[:4], "big")
    if length > MAX_PAYLOAD_BYTES:
        raise OversizedFrame(length)
    frame_type = buffer[4]
    if frame_type not in _KNOWN_TYPES:
        raise UnknownFrameType(frame_type)
    total = HEADER_BYTES + length
    if len(buffer) < total:
        return None
    payload = bytes(buffer[HEADER_BYTES:total])
    del buffer[:total]
    return frame_type, payload


import struct

TIMESTAMP_BYTES = 8


class ClipPayloadError(FrameError):
    pass


def encode_clip_payload(ts, text):
    """[f64 big-endian ts][text bytes]. Text stays bytes end to end."""
    return struct.pack(">d", ts) + text


def decode_clip_payload(payload):
    if len(payload) < TIMESTAMP_BYTES:
        raise ClipPayloadError("clip payload shorter than its timestamp: %d bytes" % len(payload))
    (ts,) = struct.unpack(">d", payload[:TIMESTAMP_BYTES])
    return ts, bytes(payload[TIMESTAMP_BYTES:])


import json
import os
import select
import sys
import threading

AGENT_VERSION = "0.1.0"
PHASE_PENDING = "clipboard-pending"
PHASE_READY = "ready"
READ_CHUNK = 65536
CLIPBOARD_RECHECK_SECONDS = 1.0


def log(message):
    """Diagnostics go to stderr. stdout carries frames and nothing else."""
    print(message, file=sys.stderr, flush=True)


class Agent:
    def __init__(self, stdin, stdout, clipboard):
        self.stdin = stdin
        self.stdout = stdout
        self.clipboard = clipboard
        self.phase = PHASE_PENDING
        self.pending_clip = None
        self._write_lock = threading.Lock()
        self._watcher = None
        self._last_written = None
        self._write_gen = 0
        # Persistent memory of what was last synced between the two
        # machines -- set in _write_clip (content arriving FROM the peer)
        # and after a successful send in _local_change (content sent TO
        # the peer). Unlike _last_written/_write_gen (a ONE-SHOT echo
        # suppression, consumed by the very next observed change), this
        # never expires on its own: it is what the Mac already holds,
        # for as long as neither side has genuinely changed it. See
        # _local_change for why a one-shot echo alone is not enough.
        self._last_seen = None
        # Guards _last_written/_write_gen/_last_seen only. A separate lock
        # from _write_lock (which guards stdout) on purpose: nesting them
        # would invite a deadlock later, and this one is held across
        # nothing that ever blocks.
        self._echo_lock = threading.Lock()

    # --- outbound -------------------------------------------------------

    def hello_payload(self):
        return json.dumps(
            {"protocol": PROTOCOL_VERSION, "agent": AGENT_VERSION}
        ).encode()

    def send_hello(self):
        self.send(TYPE_HELLO, self.hello_payload())

    def send(self, frame_type, payload):
        """The single writer. Every frame leaves through here."""
        with self._write_lock:
            self.stdout.write(encode_frame(frame_type, payload))
            flush = getattr(self.stdout, "flush", None)
            if flush:
                flush()

    # --- inbound --------------------------------------------------------

    def on_frame(self, frame_type, payload):
        if frame_type == TYPE_HELLO:
            self._on_hello(payload)
        elif frame_type == TYPE_CLIP:
            self._on_clip(payload)

    def _on_hello(self, payload):
        try:
            peer = json.loads(payload.decode())
        except (UnicodeDecodeError, ValueError):
            raise FrameError("malformed hello payload")
        if not isinstance(peer, dict):
            raise FrameError("malformed hello payload: not a JSON object")
        if peer.get("protocol") != PROTOCOL_VERSION:
            raise FrameError(
                "protocol mismatch: peer speaks %r, this agent speaks %d — "
                "run `clipwire install`" % (peer.get("protocol"), PROTOCOL_VERSION)
            )

    def _on_clip(self, payload):
        if not payload:
            return
        if self.phase != PHASE_READY:
            # Keep only the newest. Replaying a backlog into clipboard history
            # once the session appears is noise, not a feature.
            self.pending_clip = payload
            return
        self._write_clip(payload)

    # --- phase transitions ----------------------------------------------

    def clipboard_became_ready(self):
        self.phase = PHASE_READY
        # Seed a content baseline before anything else in this function
        # runs, so a brand-new watcher's very first observation has
        # something to compare against. _last_seen otherwise starts None,
        # and this agent lives exactly one connection (sshd spawns a fresh
        # process per SSH connection) -- so without a seed, ANY reconnect
        # (a Mac sleep/wake or a network blip, not only a PC reboot: these
        # also spawn a brand-new agent while the Wayland session is
        # already up) would let the first spurious signal (GPasteWatcher's
        # pump has no baseline of its own, unlike PollingWatcher) send
        # whatever the PC's clipboard already held, and the Mac applies it
        # unconditionally -- destroying a copy the user made on the Mac
        # while the channel was down. Read outside the lock, same as
        # _local_change: clipboard.read() is a wl-paste round trip that
        # can take up to SUBPROCESS_TIMEOUT=3s.
        #
        # Trade-off, accepted deliberately: re-copying on the PC to force a
        # push no longer works as the FIRST action after a connect. That is
        # correct, not a regression: it makes the two sides symmetric,
        # since the Mac does not resend its own clipboard on reconnect
        # either. The previous asymmetry ran in the destructive direction,
        # which is worse than losing a convenience. Do not "fix" this back.
        seed = self.clipboard.read()
        with self._echo_lock:
            self._last_seen = seed
        if self.pending_clip is not None:
            # Supersedes the seed above with the more authoritative value:
            # once a queued clip from the Mac has actually been applied,
            # both sides genuinely hold ITS content, not whatever the PC's
            # clipboard held a moment earlier.
            self._write_clip(self.pending_clip)
            self.pending_clip = None
        if self._watcher is None:
            self._watcher = make_watcher(self.clipboard)
            self._watcher.start(self._local_change)

    def clipboard_lost(self):
        self.phase = PHASE_PENDING
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _write_clip(self, payload):
        """Single place where we touch the local clipboard, so echo
        bookkeeping cannot be forgotten on one of the paths.

        Runs on the main thread. _local_change() (below) runs on the
        watcher's background thread and reads this same bookkeeping, so the
        two fields are only ever touched under _echo_lock.
        """
        with self._echo_lock:
            self._last_written = payload
            self._write_gen += 1
            self._last_seen = payload
        self.clipboard.write(payload)

    def _local_change(self):
        # Snapshot what we expect and the generation it belongs to BEFORE
        # reading the clipboard. clipboard.read() is a wl-paste round trip
        # that can take up to SUBPROCESS_TIMEOUT=3s, and _write_clip() can
        # land on the main thread at any point during that window. Holding
        # _echo_lock across the read would block _write_clip() for the
        # whole round trip, so it is released before the read and
        # re-acquired only to compare afterward.
        with self._echo_lock:
            expected = self._last_written
            gen = self._write_gen
            last_seen = self._last_seen

        text = self.clipboard.read()
        if not text:
            return

        # Consume the suppression on the FIRST observed change, whatever it is —
        # not only on a match. Our write produces exactly one change event; if we
        # observe a different one instead, ours is already gone, and a lingering
        # hash would silently swallow the user's later deliberate copy of the
        # same text. Mirrors EchoGuard.shouldSend on the Swift side, where the
        # match-only variant was found to be a real defect.
        with self._echo_lock:
            stale = self._write_gen != gen
            if not stale:
                self._last_written = None

        if stale:
            # A newer write landed on the main thread while this read was in
            # flight, so `text` might just be what the fork captured before
            # that write happened — stale, not a genuine local change. Drop
            # it and leave the newer write's suppression armed, so its own
            # echo (or a later genuine change) is still judged correctly.
            return
        if text == expected:
            return
        # _last_written/expected is a ONE-SHOT value: it is consumed by the
        # very next observed change, whatever that change is (the block
        # above), and is otherwise None. Signals here are deliberately
        # unfiltered (a real GPaste Update can be a history deletion, not a
        # clipboard change at all; a polling tick can follow a transient
        # read() timeout that returned None instead of the real content) --
        # so once the one-shot value is spent, ANY fired signal whose
        # content merely differs from it looks like a fresh local change.
        # _last_seen has no such expiry: it is what the peer already holds,
        # for as long as neither side has genuinely changed it, and catches
        # exactly the non-change signals the one-shot value cannot.
        if text == last_seen:
            return
        if len(text) > MAX_PAYLOAD_BYTES:
            log("skipping a clip of %d bytes: over the frame cap" % len(text))
            return
        self.send(TYPE_CLIP, text)
        # Only after a successful send: if send() ever raises (e.g. a dead
        # channel), _last_seen must not advance to content the peer never
        # actually received.
        with self._echo_lock:
            self._last_seen = text

    # --- main loop --------------------------------------------------------

    def run(self):
        self.send_hello()
        buffer = bytearray()
        while True:
            # select() with a timeout in EVERY phase. An agent that sleeps in a
            # "wait for the Wayland socket" loop never reads stdin, never sees
            # EOF, and lingers after the channel drops — so each Mac reconnect
            # before login would leave another agent behind, and they would all
            # race to write the clipboard once the session appears.
            readable, _, _ = select.select([self.stdin], [], [], CLIPBOARD_RECHECK_SECONDS)

            if readable:
                chunk = os.read(self.stdin.fileno(), READ_CHUNK)
                if not chunk:
                    log("stdin closed, exiting")
                    return 0
                buffer += chunk
                while True:
                    frame = decode_frame(buffer)
                    if frame is None:
                        break
                    self.on_frame(*frame)

            was_ready = self.phase == PHASE_READY
            is_ready = self.clipboard.ready()
            if is_ready and not was_ready:
                log("clipboard is available")
                self.clipboard_became_ready()
            elif was_ready and not is_ready:
                log("clipboard went away, waiting for it to come back")
                self.clipboard_lost()


class NeverReadyClipboard:
    """Test double selected by CLIPWIRE_FAKE_CLIPBOARD, so the main loop can be
    exercised on a machine with no Wayland session — including CI."""

    def ready(self):
        return False

    def read(self):
        return None

    def write(self, data):
        pass


import subprocess

SUBPROCESS_TIMEOUT = 3


def runtime_dir(env=None):
    env = os.environ if env is None else env
    return env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def wayland_socket_path(env=None):
    return os.path.join(runtime_dir(env), "wayland-0")


def clipboard_env(env=None):
    base = dict(os.environ if env is None else env)
    directory = runtime_dir(base)
    base["XDG_RUNTIME_DIR"] = directory
    base["WAYLAND_DISPLAY"] = "wayland-0"
    base["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % directory
    return base


class WaylandClipboard:
    def __init__(self):
        # Set once a read() call times out (or otherwise fails as an
        # OSError) and cleared the moment a call completes normally,
        # whatever its returncode -- so a hung selection owner in polling
        # mode logs the hang once, not once per tick for as long as it
        # lasts, while a later, separate hang still gets its own
        # first-occurrence log line once this one clears.
        self._read_timeout_logged = False

    def ready(self):
        return os.path.exists(wayland_socket_path())

    def read(self):
        """Current clipboard text, or None when empty or not text.

        A non-zero exit from wl-paste means an empty or non-text selection.
        That is a normal state, not an error.
        """
        try:
            result = subprocess.run(
                ["wl-paste", "-n", "--type", "text/plain;charset=utf-8"],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT, env=clipboard_env(),
            )
        except FileNotFoundError:
            log("wl-paste is not installed")
            return None
        except (subprocess.TimeoutExpired, OSError) as error:
            if not self._read_timeout_logged:
                log("wl-paste failed: %r" % error)
                self._read_timeout_logged = True
            return None
        self._read_timeout_logged = False
        if result.returncode != 0:
            return None
        return result.stdout or None

    def write(self, data):
        """wl-copy does not exit — it stays resident as the selection owner.

        It must be spawned detached with its pipes closed. Waiting on it, or
        holding its fds, hangs the agent.
        """
        try:
            process = subprocess.Popen(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, env=clipboard_env(),
            )
        except FileNotFoundError:
            log("wl-copy is not installed")
            return
        except OSError as error:
            log("wl-copy could not be started: %r" % error)
            return
        try:
            process.stdin.write(data)
        except BrokenPipeError:
            log("wl-copy closed its pipe early")
        finally:
            try:
                process.stdin.close()   # flushes; hand off ownership, never wait()
            except OSError as error:
                log("wl-copy went away before the clip was handed over: %r" % error)


def _select_clipboard():
    if os.environ.get("CLIPWIRE_FAKE_CLIPBOARD") == "never-ready":
        return NeverReadyClipboard()
    return WaylandClipboard()


def selftest():
    """Verify the deployed agent without needing a Wayland session.

    Checks what install can check remotely; reports the rest as information.
    """
    ok = True

    if sys.version_info < (3, 11):
        log("FAIL python %s is older than 3.11" % ".".join(map(str, sys.version_info[:3])))
        ok = False
    else:
        log("ok   python %s" % ".".join(map(str, sys.version_info[:3])))

    probe = encode_frame(TYPE_CLIP, "clipwire selftest ✓".encode())
    buffer = bytearray(probe)
    decoded = decode_frame(buffer)
    if decoded != (TYPE_CLIP, "clipwire selftest ✓".encode()) or buffer:
        log("FAIL codec round trip")
        ok = False
    else:
        log("ok   codec round trip")

    for tool in ("wl-copy", "wl-paste"):
        try:
            found = subprocess.run(
                ["which", tool], capture_output=True, timeout=SUBPROCESS_TIMEOUT,
            ).returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            found = False
        log("%s %s" % ("ok  " if found else "FAIL", tool))
        ok = ok and found

    log("info wayland session: %s" % ("present" if os.path.exists(wayland_socket_path()) else "absent (fine before login)"))
    log("info gpaste: %s" % ("available" if GPasteWatcher().available() else "unavailable, will poll"))

    return 0 if ok else 1


import threading

GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
# The BUS name is org.gnome.GPaste; org.gnome.GPaste2 is the INTERFACE name on
# that object. Verified on the target machine: --dest org.gnome.GPaste2 has no
# owner, so probing it would make available() always false and silently leave
# the watcher on the polling fallback forever.
GPASTE_BUS_NAME = "org.gnome.GPaste"


def parse_gpaste_line(line):
    """True when a gdbus monitor line is a GPaste Update signal.

    Verified against GPaste 45.3 on the target machine. The signal is
    Update(s action, s target, t index) and a real line looks like:

        /org/gnome/GPaste: org.gnome.GPaste2.Update ('REPLACE', 'ALL', uint64 0)

    The target is 'ALL', not 'CLIPBOARD'. Do not filter on the target: the
    observed value would reject every real signal, and the set of values
    depends on GPaste's own settings. Treat the signal as "something may have
    changed" and let the content comparison in Agent._local_change decide —
    that is correct whatever GPaste reports, and it also absorbs duplicate
    signals, which GPaste does emit.
    """
    return "Update" in line and GPASTE_OBJECT_PATH in line


class GPasteWatcher:
    """Event-driven. Python has no stdlib DBus binding, so this shells out to
    gdbus monitor and parses its output line by line."""

    def __init__(self):
        self._process = None
        self._thread = None
        self._stop = threading.Event()

    def available(self):
        try:
            result = subprocess.run(
                ["gdbus", "introspect", "--session", "--dest", GPASTE_BUS_NAME,
                 "--object-path", GPASTE_OBJECT_PATH],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT, env=clipboard_env(),
            )
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0

    def start(self, on_change):
        self._process = subprocess.Popen(
            ["gdbus", "monitor", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=clipboard_env(),
        )

        def pump():
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    on_change()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._process:
            self._process.terminate()


class PollingWatcher:
    """Degraded mode: forks wl-paste and reads the whole clipboard each time,
    so it runs at a slower interval than the Mac's in-process poll."""

    def __init__(self, clipboard, interval_seconds):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = None

    def available(self):
        return True

    def start(self, on_change):
        def pump():
            previous = self.clipboard.read()
            while not self._stop.wait(self.interval):
                current = self.clipboard.read()
                if current != previous:
                    previous = current
                    on_change()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def make_watcher(clipboard, fallback_interval_seconds=1.0):
    watcher = GPasteWatcher()
    if watcher.available():
        log("watching the clipboard through GPaste")
        return watcher
    log("GPaste unavailable, falling back to polling every %.1fs" % fallback_interval_seconds)
    return PollingWatcher(clipboard, fallback_interval_seconds)


def main(argv):
    if "--selftest" in argv:
        return selftest()
    agent = Agent(
        stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, clipboard=_select_clipboard()
    )
    try:
        return agent.run()
    except FrameError as error:
        log("protocol error: %s" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
