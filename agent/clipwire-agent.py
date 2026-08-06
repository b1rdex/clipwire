#!/usr/bin/env python3
"""clipwire PC-side agent. Spawned by sshd; speaks frames on stdin/stdout.

Only frames go to stdout. Everything else goes to stderr — a stray print()
on stdout desynchronises the protocol.

Contents. This stays one file on purpose: install scp's exactly this path,
so a package would lose the copy's atomicity and a build step would put a
generator between the file a person reads and the file the PC runs. What
replaces the split is a declared order — the banners below, in this order,
pinned by agent/tests/test_source_layout.py:

  1. Frame codec
  2. Clip payloads
  3. Freshness
  4. Agent runtime
  5. class Agent
  6. Clipboard state
  7. Wayland clipboard
  8. Selftest
  9. Watchers and entry

Selftest sits ahead of the watchers rather than after them. That is the
file's real order; the list describes the file, because the alternative —
moving code so the file matches a tidier list — is the thing the order is
meant to protect against.
"""

# ============================================================================
# 1. Frame codec — constants, FrameError, encode_frame, decode_frame
# ============================================================================

# Three bounds, separate even where two hold the same number: the frame cap is
# what the DECODER enforces, the content limits what SENDERS enforce before
# adding the 8-byte timestamp. One constant for all three makes a maximum-size
# image unsendable while looking within the limit.
MAX_PAYLOAD_BYTES = 8388608
MAX_TEXT_BYTES = 4194304
MAX_IMAGE_BYTES = 4194304
HEADER_BYTES = 5
TYPE_HELLO = 0x00
TYPE_CLIP = 0x01
TYPE_CLIP_STATE = 0x02
TYPE_IMAGE_CLIP = 0x03
PROTOCOL_VERSION = 3
_KNOWN_TYPES = (TYPE_HELLO, TYPE_CLIP, TYPE_CLIP_STATE, TYPE_IMAGE_CLIP)


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


# ============================================================================
# 2. Clip payloads — text and image, encode_*/decode_*
# ============================================================================

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
    # struct.unpack(">d") accepts EVERY 8-byte pattern, inf/nan included -- it
    # rejects nothing. Unguarded, a ts=inf clip is applied to the clipboard and
    # then kills the connection: clipboard_became_ready re-encodes that same ts
    # with no try/except, so ClipStateError escapes over an ALREADY-applied clip.
    if not math.isfinite(ts):
        raise ClipPayloadError("clip payload ts must be finite, got %r" % ts)
    return ts, bytes(payload[TIMESTAMP_BYTES:])


def encode_image_payload(ts, png):
    """type-0x03 payload: [f64 BE ts][PNG bytes]. The body is opaque here --
    PNG validity belongs to whoever read it off a clipboard, not to the codec.

    Rejects a non-finite ts on ENCODE, unlike encode_clip_payload: the
    invariant belongs to the codec rather than to whichever callers exist.
    """
    if not math.isfinite(ts):
        raise ClipPayloadError("refusing to encode a non-finite ts: %r" % ts)
    return struct.pack(">d", ts) + png


def decode_image_payload(payload):
    """Inverse of encode_image_payload. Length then ts finiteness, both before
    the body is looked at; only the last check differs from
    decode_clip_payload, since an empty image body is never representable
    while an empty clip TEXT is legal."""
    if len(payload) < TIMESTAMP_BYTES:
        raise ClipPayloadError("image payload shorter than its timestamp")
    (ts,) = struct.unpack(">d", payload[:TIMESTAMP_BYTES])
    if not math.isfinite(ts):
        raise ClipPayloadError("refusing a non-finite ts: %r" % ts)
    body = payload[TIMESTAMP_BYTES:]
    if not body:
        raise ClipPayloadError("image payload carries no image")
    return ts, bytes(body)


# ============================================================================
# 3. Freshness — decisions, kinds, clip-state codec, the two resolvers
# ============================================================================

import json
import math

SEND_MINE = "sendMine"
WAIT_FOR_PEER = "waitForPeer"
DO_NOTHING = "doNothing"

KIND_TEXT = "text"
KIND_IMAGE = "image"
_KNOWN_KINDS = (KIND_TEXT, KIND_IMAGE)


class ClipStateError(FrameError):
    pass


def encode_clip_state(sha256, ts, kind, origin=None):
    """type-0x02 payload: sha256/ts/kind, plus "origin" when given (omitted, not null, otherwise)."""
    if not math.isfinite(ts):
        # Refused here, on encode -- emitted, a bare NaN/Infinity token would
        # break the PEER's handshake instead, not this side's.
        raise ClipStateError("refusing to encode a non-finite ts: %r" % ts)
    state = {"sha256": sha256, "ts": ts, "kind": kind}
    if origin is not None:
        state["origin"] = origin
    return json.dumps(state).encode()


def _is_sha256_hex(value):
    """Exactly 64 lowercase [0-9a-f] characters."""
    # Python orders strings by code point, Swift by canonical equivalence --
    # they agree only over lowercase hex; loosen this and both sides can wait
    # on each other forever, clip lost, silently.
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def decode_clip_state(payload):
    """Inverse of encode_clip_state -- returns (sha256, ts, kind, origin)."""
    try:
        parsed = json.loads(payload.decode())
    except (UnicodeDecodeError, ValueError):
        raise ClipStateError("malformed clip-state payload")
    if not isinstance(parsed, dict):
        raise ClipStateError("malformed clip-state payload: not a JSON object")
    sha256 = parsed.get("sha256")
    if sha256 is not None and not isinstance(sha256, str):
        raise ClipStateError("malformed clip-state payload: sha256 must be a string or null")
    if sha256 is not None and not _is_sha256_hex(sha256):
        raise ClipStateError(
            "malformed clip-state payload: sha256 must be 64 lowercase hex characters"
        )
    ts = parsed.get("ts")
    # bool is a subclass of int: unguarded, JSON `true` passes isinstance as a
    # valid ts of 1 (1970). Swift throws typeMismatch on the same payload.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ClipStateError("malformed clip-state payload: ts must be a number")
    try:
        finite = math.isfinite(ts)
    except OverflowError:
        # json.loads parses an int literal as arbitrary precision; a 400-digit
        # ts passes isinstance and blows up here as a bare OverflowError.
        raise ClipStateError("malformed clip-state payload: ts out of range, got %r" % ts)
    if not finite:
        raise ClipStateError("malformed clip-state payload: ts must be finite, got %r" % ts)
    kind = parsed.get("kind")
    if (kind is None) != (sha256 is None):
        raise ClipStateError(
            "malformed clip-state payload: kind must be null exactly when sha256 is null"
        )
    if kind is not None and kind not in _KNOWN_KINDS:
        raise ClipStateError("malformed clip-state payload: unknown kind %r" % kind)
    origin = parsed.get("origin")
    if origin is not None and not isinstance(origin, str):
        raise ClipStateError("malformed clip-state payload: origin must be a string or null")
    if origin is not None and sha256 is None:
        raise ClipStateError(
            "malformed clip-state payload: origin is only admissible beside a sha256"
        )
    return sha256, float(ts), kind, origin


def resolve_freshness(mine, peer):
    """(sha256, ts) pair each; returns SEND_MINE / WAIT_FOR_PEER / DO_NOTHING."""
    mine_hash, mine_ts = mine
    peer_hash, peer_ts = peer
    if mine_hash is None and peer_hash is None:
        return DO_NOTHING
    if mine_hash is None:
        return WAIT_FOR_PEER
    if peer_hash is None:
        return SEND_MINE
    if mine_hash == peer_hash:
        return DO_NOTHING
    if mine_ts > peer_ts:
        return SEND_MINE
    if mine_ts < peer_ts:
        return WAIT_FOR_PEER
    return SEND_MINE if mine_hash > peer_hash else WAIT_FOR_PEER


def resolve_provenance(mine, peer):
    """(sha256, ts, kind, origin) record each. True iff one side's content descends from the other's."""
    # Whole records, not pairs: resolve_provenance(mine[:2], peer[:2]) would
    # compare a timestamp against a hash, return False forever, and fail no test.
    mine_hash, _mine_ts, _mine_kind, mine_origin = mine
    peer_hash, _peer_ts, _peer_kind, peer_origin = peer
    # None == None is True, so only compare two PRESENT values -- else an
    # empty clipboard against a peer with no origin reads as "related".
    if peer_origin is not None and mine_hash is not None and peer_origin == mine_hash:
        return True
    if mine_origin is not None and peer_hash is not None and mine_origin == peer_hash:
        return True
    return False


# ============================================================================
# 4. Agent runtime — version, phases, logging, skew
# ============================================================================

import os
import select
import sys
import threading
import time

AGENT_VERSION = "0.1.0"
PHASE_PENDING = "clipboard-pending"
PHASE_READY = "ready"
READ_CHUNK = 65536
CLIPBOARD_RECHECK_SECONDS = 1.0

REOFFER_TS_NUDGE_SECONDS = 0.001


def log(message):
    """Diagnostics go to stderr. stdout carries frames and nothing else."""
    print(message, file=sys.stderr, flush=True)


SKEW_WARN_SECONDS = 5.0


def skew_log_line(peer_sent_at, now):
    """Peer clock offset from ours as a log line, or None when unmeasurable."""
    # bool is a subclass of int; unguarded, JSON `true` would pass as a valid ts.
    if isinstance(peer_sent_at, bool) or not isinstance(peer_sent_at, (int, float)):
        return None
    try:
        if not math.isfinite(peer_sent_at):
            return None
        skew = abs(now - peer_sent_at)
    except OverflowError:
        return None
    if skew > SKEW_WARN_SECONDS:
        return ("peer clock skew %.1fs — over 5s, check the clock on both machines"
                % skew)
    return "peer clock skew %.1fs" % skew


# ============================================================================
# 5. class Agent — the protocol loop, with NeverReadyClipboard beside it
# ============================================================================

class Agent:
    def __init__(self, stdin, stdout, clipboard, clip_state_path=None):
        self.stdin = stdin
        self.stdout = stdout
        self.clipboard = clipboard
        self.phase = PHASE_PENDING
        self.pending_clip = None
        # Cannot be inferred later: both payloads are the same wire shape, so
        # a text payload decoded as an image would silently land in the
        # clipboard as image/png.
        self.pending_clip_kind = KIND_TEXT
        self._write_lock = threading.Lock()
        self._watcher = None
        self._last_written = None
        self._write_gen = 0
        self._clip_state_path = clip_state_path
        self._clip_state_sent = False
        self._pending_peer_clip_state = None
        # On the agent, not the watcher: clipboard_lost discards the watcher
        # and clipboard_became_ready builds a fresh one, so a mid-connection
        # Wayland flap must not re-log the diagnosis or reset the budget.
        self._event_source_degraded = False
        self._last_seen = None
        self._expect_reoffer = None
        self._echo_lock = threading.Lock()
        # Lock order: _observe_lock -> _echo_lock -> _write_lock, total and
        # acyclic -- keep any new acquisition consistent with it.
        self._observe_lock = threading.Lock()

    # --- outbound -------------------------------------------------------

    def hello_payload(self):
        return json.dumps(
            {"protocol": PROTOCOL_VERSION, "agent": AGENT_VERSION, "sent_at": time.time()}
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
        elif frame_type == TYPE_CLIP_STATE:
            self._on_clip_state(payload)
        elif frame_type == TYPE_IMAGE_CLIP:
            self._on_clip(payload, KIND_IMAGE)

    def _on_hello(self, payload, now=None):
        if now is None:
            now = time.time()
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
        line = skew_log_line(peer.get("sent_at"), now)
        if line is not None:
            log(line)

    def _on_clip(self, payload, kind=KIND_TEXT):
        """Both inbound clip types land here, tagged by `kind` -- the two wire shapes are identical."""
        if not payload:
            return
        if self.phase != PHASE_READY:
            self.pending_clip = payload
            self.pending_clip_kind = kind
            return
        self._write_clip(payload, kind)

    def _on_clip_state(self, payload):
        """Resolves an incoming clip-state now, or stashes it until clipboard_became_ready
        has reconciled our own side -- resolving against a stale store risks losing a clip."""
        peer = decode_clip_state(payload)
        if not self._clip_state_sent:
            self._pending_peer_clip_state = peer
            return
        self._resolve_clip_state(peer)

    def _resolve_clip_state(self, peer, mine=None):
        """Resolves a peer clip-state announcement against ours -- `mine`, if given, is this
        side's just-announced state; otherwise it is read from the store."""
        if mine is None:
            mine = load_clip_state(path=self._clip_state_path)
        if mine is None:
            mine = resolve_current_clip_state(self.clipboard, None, time.time())
        if resolve_provenance(mine, peer):
            if mine[3] is not None and mine[3] == peer[0]:
                log("what we hold descends from the peer's clipboard: standing down")
            else:
                log("the peer's clipboard descends from what we hold: standing down")
            decision = DO_NOTHING
        else:
            decision = resolve_freshness(mine[:2], peer[:2])
        log("reconciled with the peer: %s (mine=%s peer=%s)"
            % (decision, mine[2] or "none", peer[2] or "none"))
        if decision != SEND_MINE:
            return
        read = self.clipboard.read()
        if read is None or (read[0], sha256_hex(read[1])) != (mine[2], mine[0]):
            log("clipboard changed before the send")
            return
        kind, body = read
        if not body:
            return
        if kind == KIND_IMAGE:
            # MAX_IMAGE_BYTES bounds the image body alone; the text check
            # below adds TIMESTAMP_BYTES for the wire prefix -- separate
            # limits, kept separate even where two are numerically equal.
            if len(body) > MAX_IMAGE_BYTES:
                log("skipping an image of %d bytes: over the image limit" % len(body))
                return
            self.send(TYPE_IMAGE_CLIP, encode_image_payload(mine[1], body))
            with self._echo_lock:
                self._last_seen = (KIND_IMAGE, mine[0])
            return
        if kind != KIND_TEXT:
            return
        if len(body) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
            log("skipping a clip of %d bytes: over the text limit" % len(body))
            return
        self.send(TYPE_CLIP, encode_clip_payload(mine[1], body))
        with self._echo_lock:
            self._last_seen = (KIND_TEXT, mine[0])

    # --- phase transitions ----------------------------------------------

    def clipboard_became_ready(self):
        self.phase = PHASE_READY
        read = self.clipboard.read()
        seed = (read[0], sha256_hex(read[1])) if read is not None and read[1] else None
        with self._echo_lock:
            self._last_seen = seed
            self._expect_reoffer = None
        applied_pending = None
        if self.pending_clip is not None:
            applied_pending = self._write_clip(self.pending_clip, self.pending_clip_kind)
            self.pending_clip = None
            if applied_pending is not None:
                self._pending_peer_clip_state = None
        if not self._clip_state_sent:
            self._clip_state_sent = True
            if applied_pending is not None:
                # Not announce_clip_state (re-derives via a fresh read) --
                # wl-copy is detached and may still be taking ownership; a
                # read here would race that handoff.
                self.send(TYPE_CLIP_STATE, encode_clip_state(*applied_pending))
                announced = applied_pending
            else:
                announced = announce_clip_state(
                    self.send, self.clipboard, path=self._clip_state_path)
            if self._pending_peer_clip_state is not None:
                peer = self._pending_peer_clip_state
                self._pending_peer_clip_state = None
                self._resolve_clip_state(peer, mine=announced)
        if self._watcher is None:
            self._watcher = make_watcher(
                self.clipboard,
                degraded=self._event_source_degraded,
                on_degrade=self._note_event_source_degraded,
                on_idle_tick=self._reoffer_pending,
            )
            self._watcher.start(self._local_change)

    @staticmethod
    def _reoffer_is_overdue(expectation):
        """Has an armed expectation waited past the detection budget (SAFETY_NET_POLL_SECONDS)?"""
        return (expectation is not None
                and time.time() - expectation[2] >= SAFETY_NET_POLL_SECONDS)

    def _reoffer_pending(self):
        """True when an armed image re-offer is overdue -- asked by the poll on every
        tick that saw no change, to force one full read even though probe() is quiet."""
        with self._echo_lock:
            return self._reoffer_is_overdue(self._expect_reoffer)

    def _note_event_source_degraded(self):
        self._event_source_degraded = True

    def clipboard_lost(self):
        self.phase = PHASE_PENDING
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _write_clip(self, payload, kind=KIND_TEXT):
        """Decodes `payload` ([ts][body] wire format for `kind`) and applies it to the
        clipboard. Returns the (sha256, ts, kind, origin) record applied and persisted
        (origin always None here), or None if nothing was applied -- always four
        elements: resolve_provenance raises ValueError on a short record."""
        decode = decode_image_payload if kind == KIND_IMAGE else decode_clip_payload
        try:
            ts, body = decode(payload)
        except ClipPayloadError:
            return None
        if not body:
            return None
        sha256 = sha256_hex(body)
        with self._echo_lock:
            self._last_written = body if kind == KIND_TEXT else None
            self._write_gen += 1
            self._last_seen = (kind, sha256)
            self._expect_reoffer = (
                (ts, sha256, time.time()) if kind == KIND_IMAGE else None
            )
        self.clipboard.write(kind, body)
        # wl-copy is spawned detached; write() returns once its stdin pipe
        # closes -- a hand-off, not confirmation. Re-reading right here
        # would race it.
        try:
            save_clip_state(sha256, ts, kind, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        return sha256, ts, kind, None

    def _local_change(self):
        with self._observe_lock:
            self._observe_local_change()

    def _observe_local_change(self):
        """Judges one observed clipboard change and sends whatever is genuinely new;
        every return path must resolve any armed image re-offer expectation first."""
        with self._echo_lock:
            expected = self._last_written
            gen = self._write_gen
            last_seen = self._last_seen

        read = self.clipboard.read()
        observed_at = time.time()
        if read is None:
            # Every return path here must still spend an overdue expectation,
            # or the poll re-provokes a full read on every tick.
            self._give_up_on_reoffer(gen)
            return
        if read[0] == KIND_IMAGE:
            png = read[1]
            if not png:
                self._give_up_on_reoffer(gen)
                return
            sha256 = sha256_hex(png)
            if self._consume_image_reoffer(png, sha256, gen):
                return
            if len(png) > MAX_IMAGE_BYTES:
                log("skipping an image of %d bytes: over the image limit" % len(png))
                return
            try:
                save_clip_state(sha256, observed_at, KIND_IMAGE, path=self._clip_state_path)
            except (OSError, ClipStateError) as error:
                log("could not persist clip state: %r" % error)
            self.send(TYPE_IMAGE_CLIP, encode_image_payload(observed_at, png))
            with self._echo_lock:
                self._last_seen = (KIND_IMAGE, sha256)
            return
        kind, text = read
        if kind != KIND_TEXT or not text:
            self._give_up_on_reoffer(gen)
            return

        with self._echo_lock:
            stale = self._write_gen != gen
            if not stale:
                self._last_written = None
                self._expect_reoffer = None

        if stale:
            return
        if text == expected:
            return
        sha256 = sha256_hex(text)
        if (KIND_TEXT, sha256) == last_seen:
            return
        if len(text) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
            log("skipping a clip of %d bytes: over the text limit" % len(text))
            return
        try:
            save_clip_state(sha256, observed_at, KIND_TEXT, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        self.send(TYPE_CLIP, encode_clip_payload(observed_at, text))
        with self._echo_lock:
            self._last_seen = (KIND_TEXT, sha256)

    def _give_up_on_reoffer(self, gen):
        """Disarms an overdue image re-offer expectation on a read that could not judge
        it, so the poll stops re-probing every tick. A disarm, not a deferral: a later
        genuine re-offer is then simply treated as a fresh local clip."""
        with self._echo_lock:
            if self._write_gen != gen:
                return
            if not self._reoffer_is_overdue(self._expect_reoffer):
                return
            self._expect_reoffer = None
        log("the clipboard did not read back as an image within %gs of writing one: "
            "giving up on the re-offer" % SAFETY_NET_POLL_SECONDS)

    def _consume_image_reoffer(self, png, sha256, gen):
        """True when this observation is already accounted for (GPaste's re-encode of
        our own write, a stale read, or unchanged bytes) and must not be sent as a new
        local clip. False only for content the user actually copied."""
        disarmed = False
        with self._echo_lock:
            if self._write_gen != gen:
                return True
            if (KIND_IMAGE, sha256) == self._last_seen:
                expectation = self._expect_reoffer
                if self._reoffer_is_overdue(expectation):
                    self._expect_reoffer = None
                    disarmed = True
                else:
                    return True
            else:
                expectation = self._expect_reoffer
                if expectation is None:
                    return False
                self._expect_reoffer = None
                self._last_seen = (KIND_IMAGE, sha256)
        if disarmed:
            log("no re-offer within %gs of writing an image: "
                "nothing on this machine is re-encoding the clipboard"
                % SAFETY_NET_POLL_SECONDS)
            return True
        ts, origin, _written_at = expectation
        if len(png) > MAX_IMAGE_BYTES:
            log("the clipboard re-offered an image of %d bytes: over the image limit"
                % len(png))
        try:
            save_clip_state(sha256, ts + REOFFER_TS_NUDGE_SECONDS, KIND_IMAGE,
                            origin=origin, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        return True

    # --- main loop --------------------------------------------------------

    def run(self):
        self.send_hello()
        buffer = bytearray()
        while True:
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

    def probe(self):
        return None

    def write(self, kind, data):
        pass


# ============================================================================
# 6. Clipboard state — XDG paths, the store, resolution, announcing
# ============================================================================

import subprocess

SUBPROCESS_TIMEOUT = 3
IMAGE_SUBPROCESS_TIMEOUT = 10

SLOW_READ_SECONDS = 1.0
SLOW_IMAGE_READ_SECONDS = 3.0


def _xdg_dir(var_name, default, env=None):
    """`$<var_name>` if set, else `default`."""
    env = os.environ if env is None else env
    return env.get(var_name) or default


def runtime_dir(env=None):
    return _xdg_dir("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid(), env)


def clip_state_path(env=None):
    """`$XDG_STATE_HOME/clipwire/clip-state.json`, falling back to `~/.local/state`."""
    base = _xdg_dir("XDG_STATE_HOME", os.path.expanduser("~/.local/state"), env)
    return os.path.join(base, "clipwire", "clip-state.json")


def load_clip_state(path=None):
    """(sha256, ts, kind, origin) last persisted by save_clip_state, or None --
    covering "never written", an unreadable file, and a corrupt one alike."""
    target = clip_state_path() if path is None else path
    try:
        with open(target, "rb") as handle:
            payload = handle.read()
    except OSError:
        return None
    try:
        return decode_clip_state(payload)
    except (ClipStateError, OverflowError):
        return None


_clip_state_write_lock = threading.Lock()


def save_clip_state(sha256, ts, kind, origin=None, path=None):
    """Persists (sha256, ts, kind, origin) atomically: encode, write to a `.tmp`
    sibling, then os.replace it over the real path."""
    target = clip_state_path() if path is None else path
    with _clip_state_write_lock:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        payload = encode_clip_state(sha256, ts, kind, origin)
        tmp = target + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, target)


def resolve_startup_state(current_hash, current_kind, stored, now):
    """Reconciles a fresh clipboard read against the last stored record; always
    returns a full (sha256, ts, kind, origin) four-tuple."""
    if current_hash is None:
        return None, now, None, None
    if stored is not None and stored[0] == current_hash:
        return tuple(stored)
    return current_hash, now, current_kind, None


import hashlib


def sha256_hex(data):
    """Lowercase hex digest of `data`, which must be the clipboard's raw bytes --
    never the ts-prefixed wire payload."""
    return hashlib.sha256(data).hexdigest()


def resolve_current_clip_state(clipboard, stored, now):
    """Reconciles what the clipboard holds now against what was last persisted."""
    read = clipboard.read()
    if read is None:
        return resolve_startup_state(None, None, stored, now)
    current_kind, data = read
    if not data:
        return resolve_startup_state(None, None, stored, now)
    if current_kind == KIND_IMAGE and len(data) > MAX_IMAGE_BYTES:
        log("not announcing an image of %d bytes: over the image limit" % len(data))
        return resolve_startup_state(None, None, stored, now)
    if current_kind == KIND_TEXT and len(data) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
        log("not announcing a clip of %d bytes: over the text limit" % len(data))
        return resolve_startup_state(None, None, stored, now)
    return resolve_startup_state(sha256_hex(data), current_kind, stored, now)


def announce_clip_state(send, clipboard, now=None, path=None):
    """Builds, persists, and sends this side's clip-state announcement, reconciled
    against the store so unchanged content keeps its true recorded age."""
    if now is None:
        now = time.time()
    stored = load_clip_state(path=path)
    resolved = resolve_current_clip_state(clipboard, stored, now)
    if resolved[0] is not None and (stored is None or stored[0] != resolved[0]):
        log("clipboard changed while apart")
    try:
        save_clip_state(*resolved, path=path)
    except (OSError, ClipStateError) as error:
        log("could not persist clip state: %r" % error)
    send(TYPE_CLIP_STATE, encode_clip_state(*resolved))
    return resolved


# ============================================================================
# 7. Wayland clipboard — choose_kind, WaylandClipboard, subprocess plumbing
# ============================================================================

def wayland_socket_path(env=None):
    return os.path.join(runtime_dir(env), "wayland-0")


def clipboard_env(env=None):
    base = dict(os.environ if env is None else env)
    directory = runtime_dir(base)
    base["XDG_RUNTIME_DIR"] = directory
    base["WAYLAND_DISPLAY"] = "wayland-0"
    base["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % directory
    return base


def choose_kind(types):
    """Which kind to sync, given the clipboard's offered MIME types -- text wins when
    both are offered (a spreadsheet copy carries a bitmap of the cells alongside text)."""
    if any(t.startswith("text/plain") or t in ("UTF8_STRING", "STRING", "TEXT")
           for t in types):
        return KIND_TEXT
    if "image/png" in types:
        return KIND_IMAGE
    return None


class WaylandClipboard:
    def __init__(self):
        self._read_timeout_logged = False
        self._first_read_done = False

    def ready(self):
        return os.path.exists(wayland_socket_path())

    def read(self):
        """(kind, bytes) for whatever the clipboard holds, or None when nothing
        usable is offered."""
        force_log = self._claim_first_call()
        listed = self._list_kind(force_log)
        if listed is None:
            return None
        return self._read_body(listed[0], force_log)

    def probe(self):
        """A cheap CHANGE TOKEN for the poll loop, never content in its own right. For
        an image the token is the offered type list alone, not the body (piping
        MAX_IMAGE_BYTES every tick would be the cost); text has no such cheap proxy,
        so its token is still the body."""
        force_log = self._claim_first_call()
        listed = self._list_kind(force_log)
        if listed is None:
            return None
        kind, types = listed
        if kind == KIND_IMAGE:
            # Sorted: an unstable wl-paste order would look like a change nobody made.
            return kind, tuple(sorted(types))
        return self._read_body(kind, force_log)

    def _claim_first_call(self):
        """Whether this is the connection's first clipboard call, consuming the flag
        as it answers. Shared by read() and probe(): "first" means first of either."""
        force_log = not self._first_read_done
        self._first_read_done = True
        return force_log

    def _list_kind(self, force_log):
        """(kind, types) for what the clipboard currently offers, or None when the
        listing fails or choose_kind picks neither kind."""
        listed = self._run_wl_paste(["--list-types"], SUBPROCESS_TIMEOUT, SLOW_READ_SECONDS, force_log)
        if listed is None or listed.returncode != 0:
            return None
        types = listed.stdout.decode("utf-8", "replace").splitlines()
        kind = choose_kind(types)
        if kind is None:
            return None
        return kind, types

    def _read_body(self, kind, force_log):
        """(kind, bytes) for a kind already chosen, or None when the fetch
        fails or comes back empty. The other half of read(), and the one
        probe() skips for an image."""
        if kind == KIND_TEXT:
            result = self._run_wl_paste(
                ["-n", "--type", "text/plain;charset=utf-8"], SUBPROCESS_TIMEOUT,
                SLOW_READ_SECONDS, force_log)
        else:
            result = self._run_wl_paste(
                ["--type", "image/png"], IMAGE_SUBPROCESS_TIMEOUT, SLOW_IMAGE_READ_SECONDS, force_log)
        if result is None or result.returncode != 0 or not result.stdout:
            return None
        return kind, result.stdout

    def _run_wl_paste(self, args, timeout, slow_after, force_log):
        """One wl-paste invocation. Returns the CompletedProcess, or None on a missing
        binary, a timeout, or any OSError -- all three logged here already."""
        started = time.monotonic()
        try:
            result = subprocess.run(
                ["wl-paste"] + args, capture_output=True, timeout=timeout, env=clipboard_env(),
            )
        except FileNotFoundError:
            log("wl-paste is not installed")
            return None
        except (subprocess.TimeoutExpired, OSError) as error:
            self._log_duration_if_notable(args, time.monotonic() - started, slow_after, force_log)
            if not self._read_timeout_logged:
                log("wl-paste failed: %r" % error)
                self._read_timeout_logged = True
            return None
        self._log_duration_if_notable(args, time.monotonic() - started, slow_after, force_log)
        self._read_timeout_logged = False
        return result

    def _log_duration_if_notable(self, args, duration, slow_after, force_log):
        """Log iff forced (the connection's first read) or the duration cleared its threshold."""
        if force_log or duration >= slow_after:
            log("clipboard read (%s) took %.3fs" % (" ".join(args), duration))

    def write(self, kind, data):
        """wl-copy does not exit -- it stays resident as the selection owner. Spawn it
        detached with its pipes closed; waiting on it, or holding its fds, hangs the agent."""
        mime_type = "image/png" if kind == KIND_IMAGE else "text/plain;charset=utf-8"
        try:
            process = subprocess.Popen(
                ["wl-copy", "--type", mime_type],
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


# ============================================================================
# 8. Selftest — _select_clipboard, selftest
# ============================================================================

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
    gpaste = GPasteWatcher(_select_clipboard())
    log("info gpaste: %s" % ("available" if gpaste.available() else "unavailable, will poll"))

    return 0 if ok else 1


# ============================================================================
# 9. Watchers and entry — GPaste, polling, the safety net, main, __main__
# ============================================================================

import threading

GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
# BUS_NAME owns the object; INTERFACE doesn't -- swap them in --dest
# and available() goes permanently False, silently.
GPASTE_BUS_NAME = "org.gnome.GPaste"
GPASTE_INTERFACE = "org.gnome.GPaste2"

GPASTE_CALL_TIMEOUT = 3


def gpaste_history_uuid(run=subprocess.run):
    """Top history entry's uuid, or None when NOT MEASURED -- never compare None as "unchanged" (fail-open)."""
    try:
        result = run(
            ["gdbus", "call", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH,
             "--method", "%s.GetElementAtIndex" % GPASTE_INTERFACE, "0"],
            capture_output=True, timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
        )
    # Not bound `as error`: TimeoutExpired.output can hold the clipboard's
    # TEXT (a password) -- binding it risks leaking that into a log.
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    parts = text.split("'")
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


# GPaste 45.3 spells it "Active"; there is no "Tracking" property.
GPASTE_TRACKING_PROPERTY = "Active"


def gpaste_tracking(run=subprocess.run):
    """Whether GPaste says it's tracking, or None when NOT MEASURED -- never render None as False; they're opposite evidence."""
    try:
        result = run(
            ["gdbus", "call", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH,
             "--method", "org.freedesktop.DBus.Properties.Get",
             GPASTE_INTERFACE, GPASTE_TRACKING_PROPERTY],
            capture_output=True, timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    if "true" in text:
        return True
    if "false" in text:
        return False
    return None


IDLE_MONITOR_BUS_NAME = "org.gnome.Mutter.IdleMonitor"
IDLE_MONITOR_OBJECT_PATH = "/org/gnome/Mutter/IdleMonitor/Core"
IDLE_MONITOR_INTERFACE = "org.gnome.Mutter.IdleMonitor"

USER_IDLE_GATE_SECONDS = 300.0


def _user_recently_active(run=subprocess.run):
    """True/False for input within USER_IDLE_GATE_SECONDS, or None when NOT MEASURED -- None means PROCEED, never "idle"."""
    try:
        result = run(
            ["gdbus", "call", "--session", "--dest", IDLE_MONITOR_BUS_NAME,
             "--object-path", IDLE_MONITOR_OBJECT_PATH,
             "--method", "%s.GetIdletime" % IDLE_MONITOR_INTERFACE],
            capture_output=True, timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    _, marker, tail = text.partition("uint64")
    if not marker:
        return None
    digits = tail.strip().rstrip(",)").strip()
    if not digits.isdigit():
        return None
    return int(digits) / 1000.0 <= USER_IDLE_GATE_SECONDS


def _env_seconds(name, default):
    """`default`, overridden by env var `name` when set to a parseable positive number -- never raises."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


SAFETY_NET_POLL_SECONDS = 30.0
DEGRADED_POLL_SECONDS = 1.0
FAST_TIER_SECONDS = 5.0
DIVERGENCE_REPROBE_SECONDS = 10.0

# How long a connection that found GPaste silent keeps asking whether it has
# turned up after all, and how often. The window is generous because the thing
# it waits out is a desktop session finishing its login, not a timeout: asking
# costs one gdbus call, while giving up too early costs the whole connection.
GPASTE_WAIT_SECONDS = 60.0
GPASTE_PROBE_SECONDS = 1.0


def parse_gpaste_line(line):
    """True when a gdbus monitor line is a GPaste Update signal."""
    return "Update" in line and GPASTE_OBJECT_PATH in line


try:
    import ctypes
except ImportError:
    ctypes = None
import signal

PR_SET_PDEATHSIG = 1


def _load_libc():
    """libc via ctypes for prctl(), or None where unavailable (e.g. macOS, where the test suite runs)."""
    if ctypes is None:
        return None
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None


# Resolved HERE at import, not lazily inside preexec_fn: forking an
# already multi-threaded process can inherit a held import/dlsym lock
# and hang before it ever execs.
_LIBC = _load_libc()
_PRCTL = _LIBC.prctl if _LIBC is not None else None


def _pdeathsig_preexec():
    """Between fork and exec: SIGTERM this child if its parent dies -- what actually reaps the gdbus monitor child (stop()/terminate() alone can't)."""
    if _PRCTL is None:
        return
    _PRCTL(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    if os.getppid() == 1:
        os._exit(0)


import traceback


def _handle_observer_error(error, what):
    """Fatal (BrokenPipeError/ValueError -- the channel is gone) exits the process; everything else is logged with traceback and swallowed."""
    if isinstance(error, (BrokenPipeError, ValueError)):
        log("%s stopping, the channel is gone: %r" % (what, error))
        os._exit(0)
    log("%s error: %s" % (what, traceback.format_exc()))


def _start_observer(event, stop, on_change):
    """Start the one worker thread allowed to call on_change; every reader only ever sets `event`, never calls the handler directly."""
    def observe():
        while not stop.is_set():
            event.wait()
            if stop.is_set():
                return
            # Cleared BEFORE the handler runs, not after -- clearing after
            # would drop a change landing while the handler is busy.
            event.clear()
            try:
                on_change()
            except Exception as error:
                _handle_observer_error(error, "observer")

    worker = threading.Thread(target=observe, daemon=True)
    worker.start()
    return worker


class GPasteWatcher:
    """Event-driven: shells out to `gdbus monitor` (no stdlib D-Bus binding), backed by a fast-tier uuid poll and a slow-tier PollingWatcher safety net."""

    def __init__(self, clipboard,
                 safety_net_interval_seconds=None,
                 degraded_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None, on_idle_tick=None,
                 read_history_uuid=gpaste_history_uuid,
                 fast_interval_seconds=None, slow_interval_seconds=None,
                 read_idle_gate=None, read_tracking=None,
                 reprobe_interval_seconds=DIVERGENCE_REPROBE_SECONDS):
        self.clipboard = clipboard
        self._process = None
        self._thread = None
        self._worker = None
        self._event = threading.Event()
        self._stop = threading.Event()
        self._signals = 0
        self._signals_at_last_tick = 0
        self._uuid_at_last_tick = None
        self._armed = False
        # One-way latch for the connection: written once, never cleared,
        # deliberately SURVIVING a watcher rebuild (carried in via `degraded`).
        self._degraded = degraded
        self._degraded_interval = degraded_interval_seconds
        self._on_degrade = on_degrade
        self._read_history_uuid = read_history_uuid
        self._read_idle_gate = read_idle_gate
        self._read_tracking = read_tracking
        self._reprobe_interval = reprobe_interval_seconds
        self._interval_before_reprobe = None
        self._fast_interval = (fast_interval_seconds if fast_interval_seconds is not None
                               else _env_seconds("CLIPWIRE_FAST_TIER_SECONDS",
                                                 FAST_TIER_SECONDS))
        self._safety_net_interval = (
            safety_net_interval_seconds if safety_net_interval_seconds is not None
            else _env_seconds("CLIPWIRE_SAFETY_NET_SECONDS", SAFETY_NET_POLL_SECONDS))
        self._slow_interval = (slow_interval_seconds if slow_interval_seconds is not None
                               else _env_seconds("CLIPWIRE_SLOW_TIER_SECONDS",
                                                 self._safety_net_interval))
        self._last_uuid = None
        self._uuid_failures = 0
        self._uuid_tier_failed = False
        self._uuid_failures_before_fallback = max(
            3, int((2 * self._slow_interval) / self._fast_interval))
        self._fast_thread = None
        self._signal_path_silence_logged = False
        self._signals_at_last_fast_tick = 0
        self._safety_net = PollingWatcher(
            clipboard,
            degraded_interval_seconds if degraded else self._safety_net_interval,
            on_tick=self._observe_tick, event=self._event,
            on_idle_tick=on_idle_tick,
            should_probe=None if read_idle_gate is None else self._slow_tier_should_probe)

    def _slow_tier_should_probe(self):
        """Idle gate for a slow tick: False skips the probe, None (not measured) proceeds. Always True once degraded, when this tier is the only detector left."""
        if self._degraded:
            return True
        return self._read_idle_gate() is not False

    def _observe_tick(self, previous, current):
        """Judge whether GPaste is still tracking the clipboard, from one safety-net tick (`previous`/`current` are probe() tokens, never content)."""
        signals = self._signals
        read_ok = previous is not None and current is not None
        # uuid_failures is read BEFORE last_uuid, immediately above each
        # other -- reversed once, and it reproduced a false "tracking dead"
        # verdict on a healthy machine. Mirrors the write order in _fast_tick.
        uuid_failures = self._uuid_failures
        last_uuid = self._last_uuid
        uuid_frozen = (last_uuid is not None
                       and self._uuid_at_last_tick is not None
                       and last_uuid == self._uuid_at_last_tick)
        token_moved = previous != current
        if self._uuid_tier_failed:
            tracking_looks_dead = signals == self._signals_at_last_tick
        else:
            tracking_looks_dead = uuid_frozen and uuid_failures == 0
        if not read_ok or not tracking_looks_dead:
            confirmed = False
            self._armed = False
        elif self._armed:
            # SETTLED CLEARS here (benign re-offer) -- the opposite of the
            # fast tier's rule, where a settled/frozen reading counts AS
            # evidence toward dead.
            confirmed = token_moved
            self._armed = token_moved
        else:
            confirmed = False
            self._armed = token_moved
        if confirmed and not self._degraded:
            self._degraded = True
            tracking = None if self._read_tracking is None else self._read_tracking()
            log("GPaste reported no clipboard change while the content changed "
                "(signals=%d signals_at_last_tick=%d pump_alive=%s worker_alive=%s "
                "gpaste_%s=%s). "
                "Polling every %gs after each observed change and doubling to at "
                "most %gs while nothing changes, for the rest of this connection."
                % (signals, self._signals_at_last_tick,
                   self._thread.is_alive() if self._thread else False,
                   self.worker_alive(),  # dead, not wedged
                   GPASTE_TRACKING_PROPERTY,
                   "unavailable" if tracking is None else ("true" if tracking else "false"),
                   self._degraded_interval, SAFETY_NET_POLL_SECONDS))
            self._safety_net.interval = self._degraded_interval
            if self._on_degrade is not None:
                self._on_degrade()
        self._signals_at_last_tick = signals
        self._uuid_at_last_tick = last_uuid
        if self._degraded and read_ok:
            if token_moved:
                self._safety_net.interval = self._degraded_interval
            else:
                self._safety_net.interval = min(
                    SAFETY_NET_POLL_SECONDS, self._safety_net.interval * 2)
        # min() against the current interval, never a bare assignment --
        # tests drive this loop at 10-200ms, where a bare assignment would
        # be a ~1000x jump.
        if not self._degraded:
            if self._armed and self._interval_before_reprobe is None:
                self._interval_before_reprobe = self._safety_net.interval
                self._safety_net.interval = min(self._safety_net.interval,
                                                self._reprobe_interval)
            elif not self._armed and self._interval_before_reprobe is not None:
                self._safety_net.interval = self._interval_before_reprobe
                self._interval_before_reprobe = None

    def _fast_tick(self):
        """One fast-tier iteration: read the history uuid, signal iff it MOVED. Returns the reading, for the loop's own bookkeeping."""
        current = self._read_history_uuid()
        previous = self._last_uuid
        signals = self._signals
        if current is not None and previous is not None and current != previous:
            self._event.set()
            if (not self._signal_path_silence_logged
                    and signals == self._signals_at_last_fast_tick):
                self._signal_path_silence_logged = True
                log("the uuid tier saw the clipboard history move while the "
                    "accepted-signal count held at %d; the signal path may "
                    "be silent, but tracking itself is alive -- the fast "
                    "tier is already this connection's sync, every %gs, at "
                    "zero focus cost. Informational only; no interval "
                    "changes because of this line."
                    % (signals, self._fast_interval))
        if current is None:
            self._uuid_failures += 1
            if (not self._uuid_tier_failed
                    and self._uuid_failures >= self._uuid_failures_before_fallback):
                self._uuid_tier_failed = True
                log("the GPaste history uuid call has failed %d times in a "
                    "row (%gs); falling back to polling every %gs"
                    % (self._uuid_failures,
                       self._uuid_failures * self._fast_interval,
                       SAFETY_NET_POLL_SECONDS))
                if not self._degraded:
                    self._safety_net.interval = SAFETY_NET_POLL_SECONDS
        else:
            # ORDER MATTERS: last_uuid is stored BEFORE uuid_failures is
            # reset to 0 -- reversed once, and it reproduced a false
            # dead-tracker verdict on a healthy, actively-copying connection.
            self._last_uuid = current
            self._uuid_failures = 0
        self._signals_at_last_fast_tick = signals
        return current

    def _fast_loop(self):
        """The fast tier's own thread: run _fast_tick every _fast_interval seconds for the life of the connection."""
        while not self._stop.wait(self._fast_interval):
            try:
                self._fast_tick()
            except Exception as error:
                _handle_observer_error(error, "fast tier")

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
            text=True, env=clipboard_env(), preexec_fn=_pdeathsig_preexec,
        )

        def pump():
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    self._signals += 1
                    self._event.set()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()
        self._worker = _start_observer(self._event, self._stop, on_change)
        self._fast_thread = threading.Thread(
            target=self._fast_loop, daemon=True)
        self._fast_thread.start()
        self._safety_net.start()

    def worker_alive(self):
        """True iff the worker thread is alive -- proves DEAD, not WEDGED: a worker blocked inside a hung clipboard read is still is_alive()."""
        return self._worker is not None and self._worker.is_alive()

    def stop(self):
        self._stop.set()
        # NOT JOINED: stop() runs on the main protocol loop, and a worker
        # parked in a clipboard read can take up to 13s (SUBPROCESS_TIMEOUT +
        # IMAGE_SUBPROCESS_TIMEOUT) -- joining would stall that loop.
        self._event.set()
        self._safety_net.stop()
        if self._process:
            self._process.terminate()


class PollingWatcher:
    """Compare-and-notify against the clipboard: fork wl-paste, take a probe()
    token, signal on change. One `pump` loop serves three roles: GPasteWatcher's
    slow tier, its degraded backoff, and the standalone no-GPaste poller."""

    def __init__(self, clipboard, interval_seconds, on_tick=None, event=None,
                 on_idle_tick=None, should_probe=None):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._on_tick = on_tick
        self._on_idle_tick = on_idle_tick
        self._should_probe = should_probe
        self._stop = threading.Event()
        self._thread = None
        self._event = threading.Event() if event is None else event
        self._owns_the_worker = event is None
        self._worker = None
        self._retry_interval = None

    def available(self):
        return True

    def start(self, on_change=None):
        """`on_change` is required standalone; ignored when this poller shares someone else's event (that owner's worker calls the handler)."""
        if self._owns_the_worker:
            self._worker = _start_observer(self._event, self._stop, on_change)

        def pump():
            """Poll loop: probe() on a timer, signal the event on change (or recovery from an unresolved run), and forward tokens to on_tick."""
            previous = None
            try:
                previous = self.clipboard.probe()
            except Exception as error:
                _handle_observer_error(error, "poll")
            while not self._stop.wait(self._retry_interval or self.interval):
                try:
                    if self._should_probe is not None and not self._should_probe():
                        continue
                    current = self.clipboard.probe()
                    before = previous
                    unresolved = current is None and self._on_tick is not None
                    recovered = self._retry_interval is not None and not unresolved
                    if unresolved:
                        self._retry_interval = min(
                            SAFETY_NET_POLL_SECONDS,
                            (self._retry_interval or self.interval) * 2)
                    else:
                        self._retry_interval = None
                        if current != previous or recovered:
                            previous = current
                            self._event.set()
                        elif self._on_idle_tick is not None and self._on_idle_tick():
                            self._event.set()
                    if self._on_tick is not None:
                        self._on_tick(before, current)
                except Exception as error:
                    _handle_observer_error(error, "poll")

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._owns_the_worker:
            self._event.set()


class UpgradingWatcher(PollingWatcher):
    """The polling fallback, plus a thread that keeps asking whether GPaste has
    turned up after all.

    sshd answers earlier in a boot than a desktop session finishes coming up,
    so the Mac's connect can land in the window where GPaste has not yet taken
    its bus name. make_watcher's verdict was final for the connection, so that
    window cost the whole of it: the observed case polled every 1s for minutes,
    with clipboard reads taking 1.5-3.0s and timing out, against a GPaste that
    had been answering since seconds after the connect.

    The asking cannot be a wait inside make_watcher. That runs on the protocol
    loop, and one ask costs up to SUBPROCESS_TIMEOUT -- so the poll starts
    immediately and the asking happens behind it. Being a PollingWatcher rather
    than a wrapper around one is what keeps that true for every caller: until
    the promotion lands, this IS the fallback, with nothing delegating."""

    def __init__(self, clipboard, interval_seconds, gpaste,
                 wait_seconds=None, probe_interval_seconds=None, **kwargs):
        super().__init__(clipboard, interval_seconds, **kwargs)
        self._gpaste = gpaste
        self._wait_seconds = (
            _env_seconds("CLIPWIRE_GPASTE_WAIT_SECONDS", GPASTE_WAIT_SECONDS)
            if wait_seconds is None else wait_seconds)
        self._probe_interval = (GPASTE_PROBE_SECONDS
                                if probe_interval_seconds is None
                                else probe_interval_seconds)
        self._promotion = None
        self._promotion_lock = threading.Lock()
        self._promotion_over = threading.Event()
        self._promoted = None
        self._on_change = None

    def start(self, on_change=None):
        self._on_change = on_change
        super().start(on_change)
        self._promotion = threading.Thread(target=self._await_gpaste, daemon=True)
        self._promotion.start()

    def stop(self):
        self._promotion_over.set()
        with self._promotion_lock:
            promoted = self._promoted
        if promoted is not None:
            promoted.stop()
        super().stop()

    def _await_gpaste(self):
        """Ask on a timer until GPaste answers or the window closes. The first
        wait comes before the first ask on purpose: make_watcher just asked."""
        deadline = time.monotonic() + self._wait_seconds
        while not self._promotion_over.wait(self._probe_interval):
            try:
                answered = self._gpaste.available()
            except Exception as error:
                _handle_observer_error(error, "GPaste promotion")
                return
            if answered:
                self._promote()
                return
            if time.monotonic() >= deadline:
                log("GPaste has not answered in %gs; staying on the %gs poll"
                    % (self._wait_seconds, self.interval))
                return

    def _promote(self):
        """Hand observation to GPaste and stop the poll -- REPLACING it, not
        joining it. Claiming `_promoted` under the lock before starting is what
        lets a stop() racing this find something to stop; the start itself is
        outside the lock because stop() runs on the protocol loop and this
        spawns a subprocess."""
        with self._promotion_lock:
            if self._promotion_over.is_set():
                return
            self._promoted = self._gpaste
        super().stop()
        self._gpaste.start(self._on_change)
        if self._promotion_over.is_set():
            # stop() ran between the claim and here, so it either saw no
            # promotion or stopped one that had not started. Undo it.
            self._gpaste.stop()
            return
        log("GPaste answered late; promoted from the %gs poll to GPaste signals"
            % self.interval)


def make_watcher(clipboard, fallback_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None, on_idle_tick=None):
    """Build a GPasteWatcher if GPaste answers, else an UpgradingWatcher that polls now and promotes itself if GPaste turns up -- the production factory, wiring the real idle-gate and tracking readers."""
    watcher = GPasteWatcher(clipboard, degraded_interval_seconds=fallback_interval_seconds,
                            degraded=degraded, on_degrade=on_degrade,
                            on_idle_tick=on_idle_tick,
                            read_idle_gate=_user_recently_active,
                            read_tracking=gpaste_tracking)
    if watcher.available():
        if degraded:
            log("watching the clipboard through GPaste, already diagnosed as silent "
                "this connection, so polling every %.1fs" % fallback_interval_seconds)
        else:
            log("watching the clipboard through GPaste, with a safety-net poll every %gs"
                % watcher._safety_net_interval)
        return watcher
    log("GPaste unavailable, falling back to polling every %.1fs" % fallback_interval_seconds)
    # `watcher` is reused rather than rebuilt: available() does not mutate it,
    # and it already carries the degraded state and the D-Bus readers this
    # factory is the only place to wire.
    return UpgradingWatcher(clipboard, fallback_interval_seconds, gpaste=watcher,
                            on_idle_tick=on_idle_tick)


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
