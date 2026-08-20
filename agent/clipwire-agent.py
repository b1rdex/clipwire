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


def _describe_duration(seconds):
    """'17h 30m', '1m 3s', '45s'. The unlock line is the incident report,
    and its motivating incident is 17.5 HOURS -- raw seconds ('63000s') fail
    'readable from this log alone' on the one case that matters."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    parts = []
    if hours:
        parts.append("%dh" % hours)
    if minutes:
        parts.append("%dm" % minutes)
    if secs or not parts:
        parts.append("%ds" % secs)
    return " ".join(parts)


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
        # Counts every clip that coalesced into pending_clip, not just the
        # one that survives -- the flush report is about what the user's
        # peer SENT while this side couldn't apply it, not about the single
        # payload that happened to still be pending when it could. Reset at
        # every clipboard_became_ready flush regardless of what it finds
        # (spec §6), so a plain reconnect with no lock involved never leaves
        # a stale count for the next genuine stretch to inherit.
        self._pending_arrivals = 0
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
        # The v3.4 text tier (make_watcher's GPaste branch) and the last
        # CONFIRMED tier write (uuid, monotonic stamp, sha256). Both survive
        # clipboard_lost: the unlock flush and the reassert both run before
        # the next watcher exists.
        self._tier = None
        self._last_tier_write = None

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
            self._pending_arrivals += 1
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
        # getattr, not a direct call: every clipboard double predating Task 5,
        # and NeverReadyClipboard, has no lock_stretch_ended at all and must
        # keep working exactly as before rather than grow one just to answer
        # this call.
        stretch = getattr(self.clipboard, "lock_stretch_ended", lambda: None)()
        # Reset unconditionally, not only when a stretch just ended: a plain
        # flush with no lock involved must not leave a stale count for
        # whatever locked stretch ends next to inherit -- pinned by
        # test_a_flush_with_no_stretch_still_spends_the_count, which fails
        # with a fabricated "N clips arrived" line if this moves inside the
        # `if` below (verified: that mutation leaves the rest of the suite
        # green).
        arrivals, self._pending_arrivals = self._pending_arrivals, 0
        if stretch is not None:
            # LockGate.open() logs only the CLOSING edge (spec's lock line);
            # stretch_just_ended() itself logs nothing. So this is
            # deliberately the only unlock line there is -- it says what was
            # held, not just that the gate reopened. ended_by picks which of
            # two pairs (spec §6): "unlocked" is a real LockedHint=false
            # held past the debounce -- README.md quotes this pair,
            # updated in the same commit as the duration format below.
            # "fell-open" is the monitor losing the session mid-lock -- a
            # stretch that ends in fail-open still reports, the most
            # incident-shaped event this feature has, and silence here
            # would put this whole log's "readable from this alone" purpose
            # out of reach exactly when it matters most. Never-locked
            # flushes still see stretch is None above and stay silent,
            # unchanged.
            seconds, ended_by = stretch[0], stretch[1]
            held = _describe_duration(seconds)
            if ended_by == "unlocked":
                if arrivals:
                    log("session unlocked after %s; %d clips arrived while locked,"
                        " applied the newest" % (held, arrivals))
                else:
                    log("session unlocked after %s; no clips arrived while locked"
                        % held)
            else:
                if arrivals:
                    log("lock gate fell open after %s; %d clips arrived while locked,"
                        " applied the newest" % (held, arrivals))
                else:
                    log("lock gate fell open after %s; no clips arrived while locked"
                        % held)
        read = self.clipboard.read()
        seed = (read[0], sha256_hex(read[1])) if read is not None and read[1] else None
        with self._echo_lock:
            self._last_seen = seed
            self._expect_reoffer = None
        if stretch is not None:
            # Unpacked here, not carried down from the log block: pyright
            # reads bindings made under one `if stretch is not None` as
            # possibly unbound under the next, and the split itself is
            # deliberate (the seed read sits between them).
            _, _, lock_edge, ended_at = stretch
            record = self._last_tier_write
            if record is not None and lock_edge <= record[1] <= ended_at:
                # After the seed read, not before: the seed's own
                # `_last_seen = seed` above would clobber an earlier arm.
                # Select: sub-ms, idempotent. Arming AFTER it (elsewhere in
                # this file, before) is safe here only because no watcher
                # is alive between the Select and the arm -- clipboard_lost
                # discarded the last one, the next starts later in this
                # method. Arms _last_seen alone -- a one-shot could go
                # unconsumed and swallow the owner's next real copy (M4).
                # Stored uuids are perishable (dedup-move re-mints one), so
                # a dead uuid's Select fails rc=1 -> False: ordinary here.
                self._last_tier_write = None
                uuid, _, sha256 = record
                if gpaste_select(uuid):
                    log("re-selected %s: tier write completed inside a locked "
                        "interval" % uuid)
                    with self._echo_lock:
                        self._last_seen = (KIND_TEXT, sha256)
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
            self._tier = getattr(self._watcher, "tier", None)

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

    def _tier_write(self, kind, body, sha256):
        """The v3.4 write guard: uuid of a confirmed focus-free write, or
        None -- the caller then runs today's wl-copy path unchanged. Declines
        are silent (no tier, not text, NUL, non-UTF-8: routing, not failure);
        a tier that TRIED and could not confirm logs the fallback."""
        tier = self._tier
        if tier is None or kind != KIND_TEXT:
            return None
        if b"\x00" in body:
            return None
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            return None
        with self._echo_lock:
            self._last_written = body
            self._write_gen += 1
            self._last_seen = (KIND_TEXT, sha256)
            self._expect_reoffer = None
        uuid = tier.write_text(body)
        if uuid is not None:
            self._last_tier_write = tier.last_confirmed_write
            return uuid
        top = tier.last_mismatch_top
        if top is not None:
            # A failed Add can raise TWO Updates (move + rollback). The
            # one-shot is spent on the first, so the second would read the
            # rolled-back OLD top as a fresh local change and send it to the
            # peer -- a failed write silently overwriting the Mac's clipboard.
            with self._echo_lock:
                self._last_seen = (KIND_TEXT, sha256_hex(top))
        log("the GPaste add was not confirmed; writing with wl-copy instead")
        return None

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
        if self._tier_write(kind, body, sha256) is None:
            # A fallback write supersedes the tier record; a reassert on the
            # stale uuid would revert this newer clip (v3.5 §5).
            self._last_tier_write = None
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

    def _tier_read(self):
        """The v3.4 read guard: (KIND_TEXT, bytes) through GPaste for a Text
        top-of-history, else None and the caller runs today's wl-paste read.
        The uuid is fetched fresh rather than borrowed from the fast tier:
        its poll can trail the Update this read answers, and a stale uuid
        fetches the PREVIOUS clip -- wrong, not missing.

        And it is not fetched at all when the safety net says this
        observation came from a selection that moved over a FROZEN history
        uuid: GPaste recorded nothing, so its top is somebody else's older
        clip and wl-paste is the only reader that can see this one (v3.3's
        excluded-clip contract -- alive only where the probe still runs;
        v3.6 suppresses both while GPaste is trusted)."""
        tier = self._tier
        if tier is None:
            return None
        watcher = self._watcher
        # getattr with a default, not hasattr or a type check: the polling and
        # Upgrading watchers never grew this method, and neither did any of
        # the suite's watcher doubles -- all of them must stay on today's path.
        if (watcher is not None
                and getattr(watcher, "consume_untracked_change", lambda: False)()):
            return None
        uuid = tier.current_uuid()
        if uuid is None:
            return None
        body = tier.read_text(uuid)
        if body is None:
            return None
        return KIND_TEXT, body

    def _observe_local_change(self):
        """Judges one observed clipboard change and sends whatever is genuinely new;
        every return path must resolve any armed image re-offer expectation first."""
        with self._echo_lock:
            expected = self._last_written
            gen = self._write_gen
            last_seen = self._last_seen

        read = self._tier_read()
        if read is None:
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
        # Non-None by the branch logic above: the echo-match arm either
        # returned or disarmed, the disarmed exit returned just before this
        # line, and the only arm left null-checked expectation before
        # falling through. pyright cannot correlate the disarmed flag with
        # that binding, so the invariant is stated as an assert. First
        # assert in this file, so the ground rule it sets: the comment
        # carries the invariant, the assert only enforces it -- `python3 -O`
        # would strip every assert silently, and production runs plain
        # python3.
        assert expectation is not None
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

    def read_classified(self):
        return READ_UNKNOWN, None

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

READ_CONTENT = "content"
READ_EMPTY = "empty"
READ_UNKNOWN = "unknown"

# Evidenced 2026-08-17 via `strings /usr/bin/wl-paste` (wl-clipboard 2.2.1).
# The state that would send it is unreachable here -- GPaste refills <=1ms
# of any clear, even track-changes=false -- so exit code/stream stay
# expectation; wrong here falls to unknown, announcing from the store, not null.
WL_PASTE_NO_SELECTION = b"Nothing is copied"


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
    return _resolve_readable_clip_state(clipboard.read(), stored, now)


def announce_clip_state(send, clipboard, now=None, path=None):
    """Builds, persists, and sends this side's clip-state announcement, reconciled
    against the store so unchanged content keeps its true recorded age. An
    unreadable clipboard announces the store untouched: fabricating "empty" out
    of a failed read is what turned one dark screen into a resend per reconnect."""
    if now is None:
        now = time.time()
    stored = load_clip_state(path=path)
    outcome, payload = classify_read(clipboard)
    if outcome == READ_UNKNOWN:
        log("announcing from the store: clipboard unreadable")
        resolved = tuple(stored) if stored is not None else (None, now, None, None)
        send(TYPE_CLIP_STATE, encode_clip_state(*resolved))
        return resolved
    resolved = _resolve_readable_clip_state(payload, stored, now)
    if resolved[0] is not None and (stored is None or stored[0] != resolved[0]):
        log("clipboard changed while apart")
    try:
        save_clip_state(*resolved, path=path)
    except (OSError, ClipStateError) as error:
        log("could not persist clip state: %r" % error)
    send(TYPE_CLIP_STATE, encode_clip_state(*resolved))
    return resolved


def _resolve_readable_clip_state(payload, stored, now):
    """resolve_current_clip_state's readable half: payload is (kind, data) or
    None for a definitely-empty clipboard."""
    if payload is None:
        return resolve_startup_state(None, None, stored, now)
    current_kind, data = payload
    if not data:
        return resolve_startup_state(None, None, stored, now)
    if current_kind == KIND_IMAGE and len(data) > MAX_IMAGE_BYTES:
        log("not announcing an image of %d bytes: over the image limit" % len(data))
        return resolve_startup_state(None, None, stored, now)
    if current_kind == KIND_TEXT and len(data) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
        log("not announcing a clip of %d bytes: over the text limit" % len(data))
        return resolve_startup_state(None, None, stored, now)
    return resolve_startup_state(sha256_hex(data), current_kind, stored, now)


def classify_read(clipboard):
    """Tri-state read for boundaries that must tell empty from unreadable.
    Doubles predating v3.5 answer through read(): None meant empty then and
    keeps meaning empty here."""
    classified = getattr(clipboard, "read_classified", None)
    if classified is not None:
        return classified()
    read = clipboard.read()
    if read is None:
        return READ_EMPTY, None
    return READ_CONTENT, read


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


UNLOCK_HOLD_SECONDS = 1.0


class LockGate:
    """Folds LockMonitor.state() (Task 4) into the bool WaylandClipboard.ready()
    consults, with a wall-time debounce on the UNLOCK edge only (spec §3.3). A
    lock closes the gate the instant it is sampled; an unlock reopens it only
    once LockedHint=false has been HELD for UNLOCK_HOLD_SECONDS on the
    SUBSCRIBER's own monotonic clock -- never by counting run()-loop samples,
    which collapse to milliseconds under frame traffic (run()'s select at
    :667 returns instantly while frames are arriving).

    Needs no timer of its own for the hold: LockMonitor's `since` does not
    move on a repeated same-value observation (Task 4, pinned), so `now() -
    since`, read fresh on every call, already measures how long THIS unlock
    has been held.

    All access is from the run()-loop thread, via ready() (:681-682) -- no
    lock here: LockMonitor's own cache carries its own lock (Task 4) and
    hands state() out as an immutable tuple, so nothing below is ever
    shared across threads.
    """

    def __init__(self, state, now=time.monotonic):
        self._state = state
        self._now = now
        self._saw_locked = False   # True once ANY lock has been observed
        self._was_open = True      # drives both edges: True->closing, False->opening
        self._lock_edge = None     # `since` of the most recent CLOSING transition
        self._banked = None        # a just-ended stretch, waiting for stretch_just_ended()

    def open(self):
        locked, since = self._state()
        if locked is None:
            # Fail-open: unmeasurable is never locked, and never debounced
            # either -- there is no trustworthy `since` to hold against.
            if not self._was_open:
                # Falling open FROM a locked stretch, not from already-open
                # (spec §6: "a stretch that ends in fail-open still
                # reports") -- bank it now, lock-edge to THIS moment: a
                # None reading carries no trustworthy `since` of its own,
                # so the wall clock is the only bound the stretch's end
                # can be measured against. `_was_open` guards this to fire
                # once per transition, the same shape the real-unlock
                # branch below already has -- a second, third, ... None
                # sample while still fail-open must not re-bank.
                ended = self._now()
                self._banked = (ended - self._lock_edge, "fell-open",
                                self._lock_edge, ended)
            self._was_open = True
            return True
        if locked:
            self._saw_locked = True
            if self._was_open:
                log("session locked; holding clips (latest wins)")
                self._lock_edge = since
                self._was_open = False
            return False
        if not self._saw_locked or self._was_open:
            # Never locked yet, or already past the hold for this unlock --
            # either way, open with no further wait (spec §3.3 "Edges").
            self._was_open = True
            return True
        if self._now() - since >= UNLOCK_HOLD_SECONDS:
            self._banked = (since - self._lock_edge, "unlocked",
                            self._lock_edge, since)
            self._was_open = True
            return True
        return False

    def stretch_just_ended(self):
        """The just-banked (seconds, ended_by, lock_edge, ended_at) of the
        stretch that just ended, consumed -- None once already taken, or
        when no locked stretch has ended yet. ended_by is "unlocked" for a
        real LockedHint=false held past the debounce, or "fell-open" when
        the monitor instead lost the session mid-lock (spec §6). lock_edge
        and ended_at are the two transition stamps the duration was
        measured between, on this object's own clock."""
        stretch, self._banked = self._banked, None
        return stretch


class WaylandClipboard:
    def __init__(self, lock_gate=None):
        self._read_timeout_logged = False
        self._first_read_done = False
        self._lock_gate = lock_gate

    def ready(self):
        # Socket first: the short-circuit means the gate's own edge
        # bookkeeping (the lock-edge log, the debounce clock) only ever
        # advances while the Wayland session actually exists -- a socket
        # flap before login must not also count as a spurious lock edge.
        return (os.path.exists(wayland_socket_path())
                and (self._lock_gate is None or self._lock_gate.open()))

    def lock_stretch_ended(self):
        """(seconds, ended_by, lock_edge, ended_at) of the stretch that just
        ended, consumed once -- delegates to the gate; None with no gate
        (this connection predates Task 5, or is a fake) or when no stretch
        just ended. See LockGate.stretch_just_ended for what ended_by,
        lock_edge, and ended_at distinguish."""
        if self._lock_gate is None:
            return None
        return self._lock_gate.stretch_just_ended()

    def read(self):
        """(kind, bytes) for whatever the clipboard holds, or None when nothing
        usable is offered."""
        return self.read_classified()[1]

    def read_classified(self):
        """(outcome, payload): READ_CONTENT with (kind, bytes); READ_EMPTY or
        READ_UNKNOWN with None. Empty means the compositor answered "nothing" --
        including wl-paste's own no-selection exit; unknown means the question
        could not be asked. The announce path persists the first and must never
        persist the second: empty null-announcements are what the freshness
        recovery re-seeds a fresh login from."""
        force_log = self._claim_first_call()
        listed = self._run_wl_paste(["--list-types"], SUBPROCESS_TIMEOUT,
                                    SLOW_READ_SECONDS, force_log)
        outcome = self._classify_result(listed)
        if outcome is not None:
            return outcome, None
        # _classify_result returning None IS the None check: a None result
        # maps to READ_UNKNOWN and returned above. pyright cannot see that
        # across the call, hence the assert.
        assert listed is not None
        types = listed.stdout.decode("utf-8", "replace").splitlines()
        kind = choose_kind(types)
        if kind is None:
            return READ_EMPTY, None
        if kind == KIND_TEXT:
            result = self._run_wl_paste(
                ["-n", "--type", "text/plain;charset=utf-8"], SUBPROCESS_TIMEOUT,
                SLOW_READ_SECONDS, force_log)
        else:
            result = self._run_wl_paste(
                ["--type", "image/png"], IMAGE_SUBPROCESS_TIMEOUT,
                SLOW_IMAGE_READ_SECONDS, force_log)
        outcome = self._classify_result(result)
        if outcome is not None:
            return outcome, None
        assert result is not None  # same gate as above
        if not result.stdout:
            return READ_EMPTY, None
        return READ_CONTENT, (kind, result.stdout)

    @staticmethod
    def _classify_result(result):
        """READ_UNKNOWN / READ_EMPTY for a failed CompletedProcess, None when
        the call succeeded and the caller should look at stdout."""
        if result is None:
            return READ_UNKNOWN
        if result.returncode != 0:
            stderr = result.stderr or b""
            if WL_PASTE_NO_SELECTION in stderr:
                return READ_EMPTY
            return READ_UNKNOWN
        return None

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
        # stdin=PIPE above means Popen set .stdin; typeshed types it
        # Optional anyway.
        assert process.stdin is not None
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

def _fake_clipboard_requested():
    return os.environ.get("CLIPWIRE_FAKE_CLIPBOARD") == "never-ready"


def _select_clipboard(lock_gate=None):
    if _fake_clipboard_requested():
        return NeverReadyClipboard()
    return WaylandClipboard(lock_gate=lock_gate)


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


def gpaste_element_kind(uuid, run=subprocess.run):
    """The item's GPaste kind ('Text', 'Image', 'Uris', 'Password'), or None
    when NOT MEASURED -- only an answered 'Text' may route a body through the
    text tier; every other answer, None included, is the wl-clipboard path."""
    try:
        result = run(
            ["gdbus", "call", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH,
             "--method", "%s.GetElementKind" % GPASTE_INTERFACE, uuid],
            capture_output=True, timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    parts = result.stdout.decode("utf-8", "replace").split("'")
    if len(parts) < 2 or not parts[1]:
        return None
    return parts[1]


def gpaste_select(uuid, run=subprocess.run):
    """True iff GPaste accepted Select(uuid). Every failure shape is False:
    the caller has nothing to fall back to, only a log line not to write."""
    try:
        result = run(
            ["gdbus", "call", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH,
             "--method", "%s.Select" % GPASTE_INTERFACE, uuid],
            capture_output=True, timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


# The add's own timeout: the measured novel-Add cost was 1.37-1.41s flat on
# 2026-08-05, and 14-35ms on 2026-08-17 on the same machine -- the two
# readings are not reconciled, so the timeout stays sized to the worse day
# rather than to either measured cost.
GPASTE_ADD_TIMEOUT_SECONDS = 5


class GPasteTextTier:
    """v3.4: text bodies through GPaste's D-Bus instead of wl-clipboard, so
    neither direction spawns the focus-taking surface. Fail-open everywhere:
    read_text None / write_text None mean NOT DONE, and the caller runs the
    wl-clipboard path unchanged."""

    def __init__(self, run=subprocess.run, popen=subprocess.Popen,
                 read_history_uuid=gpaste_history_uuid,
                 read_element_kind=None, monotonic=time.monotonic):
        self._run = run
        self._popen = popen
        self._read_history_uuid = read_history_uuid
        self._read_element_kind = (gpaste_element_kind if read_element_kind is None
                                   else read_element_kind)
        self._monotonic = monotonic
        self.last_confirmed_write = None   # (uuid, monotonic stamp, sha256 hex)
        self.last_mismatch_top = None      # read-back bytes of the last mismatch

    def current_uuid(self):
        return self._read_history_uuid()

    def read_text(self, uuid):
        """Raw bytes of a Text item, or None. The kind gate is not decoration:
        without it an image at the top would travel as the literal display
        string '[Image, 1686 x 1182 (...)]' -- wrong, not missing."""
        if self._read_element_kind(uuid) != "Text":
            return None
        return self._raw_get(uuid)

    def _raw_get(self, uuid):
        # stdin=DEVNULL: gpaste-client reads stdin to EOF before it even
        # dispatches the verb whenever stdin is not a TTY, and under sshd it
        # never is -- without EOF this call hangs forever.
        try:
            result = self._run(
                ["gpaste-client", "--raw", "get", uuid],
                capture_output=True, stdin=subprocess.DEVNULL,
                timeout=GPASTE_CALL_TIMEOUT, env=clipboard_env(),
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout

    def write_text(self, body):
        """uuid of a CONFIRMED write, or None for the caller to fall back.
        Exit 0 alone is not confirmation -- GPaste drops an over-limit clip
        silently with exit 0 -- only a byte-equal read-back is."""
        self.last_mismatch_top = None
        try:
            process = self._popen(
                ["gpaste-client", "add"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=clipboard_env(),
            )
        except OSError:
            return None
        try:
            # communicate writes the body, CLOSES stdin, then waits: EOF is
            # what STARTS the daemon's work, not what ends it -- a body
            # written with stdin left open hangs to any timeout.
            process.communicate(body, timeout=GPASTE_ADD_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            # Killing the client does not cancel an Add already inside the
            # daemon; the caller's wl-copy fallback may then write the same
            # content a second time.
            process.kill()
            process.communicate()
            return None
        except OSError:
            process.kill()
            process.communicate()
            return None
        if process.returncode != 0:
            return None
        uuid = self._read_history_uuid()
        if uuid is None:
            return None
        top = self._raw_get(uuid)
        if top != body:
            self.last_mismatch_top = top
            return None
        self.last_confirmed_write = (uuid, self._monotonic(), sha256_hex(body))
        return uuid


LOGIND_BUS_NAME = "org.freedesktop.login1"
LOGIND_OBJECT_PATH = "/org/freedesktop/login1"
LOGIND_MANAGER_IFACE = "org.freedesktop.login1.Manager"
LOGIND_SESSION_IFACE = "org.freedesktop.login1.Session"
# wayland/x11 only -- "tty" is an ssh or console login, "unspecified" is the
# manager's own bookkeeping session; neither one's LockedHint means anything.
GRAPHICAL_SESSION_TYPES = ("wayland", "x11")


def parse_logind_sessions(text):
    """(object_path, seat) for every session in a ListSessions reply, or []
    when the text isn't one -- gdbus prints a type annotation (`uint32`,
    `objectpath`) on the FIRST array element only, so a parser keyed on a
    token like `objectpath` sees just that one session on every later call."""
    text = text.strip()
    if not (text.startswith("([") and text.endswith("],)")):
        return []
    sessions = []
    for entry in text[2:-3].split("), ("):
        # Fields are (id, uid, user, seat, path); uid is the only unquoted
        # one, so exactly 4 quoted strings survive regardless of which field
        # carries an annotation prefix -- seat and path are always the last two.
        quoted = entry.split("'")[1::2]
        if len(quoted) < 2:
            continue
        sessions.append((quoted[-1], quoted[-2]))
    return sessions


def resolve_graphical_session(run=subprocess.run):
    """Path of the one session a lock-state read should trust, or None when
    the machine offers zero or several candidates -- guessing among several
    is the exact mistake this exists to prevent (spec §3.2, §7.4)."""
    try:
        result = run(
            ["gdbus", "call", "--system", "--dest", LOGIND_BUS_NAME,
             "--object-path", LOGIND_OBJECT_PATH,
             "--method", "%s.ListSessions" % LOGIND_MANAGER_IFACE],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    sessions = parse_logind_sessions(text)
    # Seat only orders the probes (seated sessions first, so the common case
    # exits fast below) -- it never decides; Type and Class decide.
    sessions.sort(key=lambda session: session[1] == "")
    matches = []
    # A None probe is NOT "definitely not a candidate" -- it may be hiding
    # the second session that would turn a clean match into "several", so it
    # poisons the count instead of clearing it (spec §3.2 step 1, amended
    # 726a978): a single match under a poisoned count is not "exactly one".
    poisoned = False
    for path, _seat in sessions:
        session_type = session_property(path, "Type", run=run)
        if session_type is None:
            poisoned = True
            continue
        if session_type not in GRAPHICAL_SESSION_TYPES:
            continue
        session_class = session_property(path, "Class", run=run)
        if session_class is None:
            poisoned = True
            continue
        if session_class != "user":
            continue
        matches.append(path)
        if len(matches) > 1:
            break  # already several -- further probes can't undo that
    return None if poisoned or len(matches) != 1 else matches[0]


def session_property(path, prop, run=subprocess.run):
    """A Session object's string property (Type, Class, ...) with its
    GVariant quoting stripped, or None when NOT MEASURED."""
    try:
        result = run(
            ["gdbus", "call", "--system", "--dest", LOGIND_BUS_NAME,
             "--object-path", path,
             "--method", "org.freedesktop.DBus.Properties.Get",
             LOGIND_SESSION_IFACE, prop],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if result.returncode != 0:
        return None
    text = result.stdout.decode("utf-8", "replace")
    parts = text.split("'")
    if len(parts) < 2:
        return None
    return parts[1]


def session_locked_hint(path, run=subprocess.run):
    """The session's LockedHint, or None when NOT MEASURED -- never render
    None as False; they're opposite evidence (mirrors gpaste_tracking above)."""
    try:
        result = run(
            ["gdbus", "call", "--system", "--dest", LOGIND_BUS_NAME,
             "--object-path", path,
             "--method", "org.freedesktop.DBus.Properties.Get",
             LOGIND_SESSION_IFACE, "LockedHint"],
            capture_output=True, timeout=SUBPROCESS_TIMEOUT,
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


def parse_logind_monitor_line(line, session_path):
    """"locked"/"unlocked" for a PropertiesChanged LockedHint flip on the
    watched session; "resolve" when the session table itself changed
    (SessionNew/SessionRemoved -- a relogin); else None.

    Substring matching only, SYNTHETIC until Task 2 step 5's real capture
    lands (spec v3.5 §3.2 step 5) -- a real capture can only tighten this,
    never loosen it. `session_path` may be None (unresolved/fail-open): the
    resolve case still matters then, since it is what gives the monitor a
    chance to recover; a LockedHint flip on no watched session does not.

    The path check requires the ": " that follows it in both the synthetic
    fixture and real gdbus monitor output, not a bare substring test:
    logind's bus_label_escape can produce one session's path as a strict
    prefix of another's (session "1" -> .../_31, session "12" -> .../_312),
    and a bare `session_path in line` lets a _312 line flip a monitor
    watching _31 (review-caught false positive)."""
    if (session_path is not None and (session_path + ": ") in line
            and "PropertiesChanged" in line):
        if "'LockedHint': <true>" in line:
            return "locked"
        if "'LockedHint': <false>" in line:
            return "unlocked"
    session_new = "%s.SessionNew" % LOGIND_MANAGER_IFACE
    session_removed = "%s.SessionRemoved" % LOGIND_MANAGER_IFACE
    if session_new in line or session_removed in line:
        return "resolve"
    return None


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
    """Event-driven: shells out to `gdbus monitor` (no stdlib D-Bus binding),
    backed by a fast-tier uuid poll and a slow-tier PollingWatcher safety net.
    v3.6: with suppress_healthy_probes on (make_watcher always passes it), the
    safety net stops forking wl-paste while GPaste is trusted -- see
    _wl_probe_suppressed for the contract and its accepted blind spot."""

    # Declared for pyright alone. A bare annotation puts nothing on the
    # class, so hasattr()/vars() on instances stay exactly as the tests
    # pin them (test_watcher_wiring: the attribute "does not exist at
    # all" until make_watcher raises it on a promoted instance); every
    # consumer keeps reading it through getattr with a default. Quoted so
    # nothing is evaluated at class-creation time either.
    tier: "GPasteTextTier | None"

    def __init__(self, clipboard,
                 safety_net_interval_seconds=None,
                 degraded_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None, on_idle_tick=None,
                 read_history_uuid=gpaste_history_uuid,
                 fast_interval_seconds=None, slow_interval_seconds=None,
                 read_idle_gate=None, read_tracking=None,
                 reprobe_interval_seconds=DIVERGENCE_REPROBE_SECONDS,
                 suppress_healthy_probes=False):
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
        self._untracked_change = threading.Event()
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
        # OFF by default so the judge's own tests keep driving probes
        # through healthy watchers; make_watcher -- the one production
        # assembly point -- always switches it on.
        self._safety_net = PollingWatcher(
            clipboard,
            degraded_interval_seconds if degraded else self._safety_net_interval,
            on_tick=self._observe_tick, event=self._event,
            on_idle_tick=on_idle_tick,
            should_probe=None if read_idle_gate is None else self._slow_tier_should_probe,
            probe_suppressed=self._wl_probe_suppressed if suppress_healthy_probes else None)

    def _slow_tier_should_probe(self):
        """Idle gate for a slow tick: False skips the probe, None (not measured) proceeds. Always True once degraded, when this tier is the only detector left."""
        if self._degraded:
            return True
        # Wired as should_probe only when read_idle_gate came in non-None
        # (see __init__), so this cannot be None by construction.
        assert self._read_idle_gate is not None
        return self._read_idle_gate() is not False

    def _wl_probe_suppressed(self):
        """v3.6: while GPaste is trusted, the safety net must not fork
        wl-paste -- on GNOME every fork is a transient Wayland window, a
        focus blink on a timer (and an "is ready" banner when mutter
        denies it focus). Trust ends when the connection is degraded or
        the uuid tier has given up; both flips already retune the safety
        net's interval, and this predicate is re-read on every tick, so
        the change needs no extra plumbing. The price, accepted by design:
        while suppressed the token-vs-uuid judge cannot run, so clips
        GPaste refuses to track (password managers, excluded apps, the
        shield) no longer sync at the slow cadence -- v3.3 §4.2's promise,
        repealed: the old probe was BYPASSING those privacy mechanisms."""
        return not self._degraded and not self._uuid_tier_failed

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
        if read_ok and token_moved and uuid_frozen:
            # The selection moved while GPaste's history top stood still:
            # GPaste did not record this clip (an excluded app, a password
            # manager, its own image re-offer). Only wl-paste can see it --
            # the tier would serve the OLD top: wrong, not missing. v3.3 §4.2
            # promised these still sync at the slow cadence; this is what
            # kept that true while the probe ran. v3.6 repeals §4.2 on a
            # trusted connection -- no probe, so no tick reaches this judge
            # -- and this branch stays live only where the probe does:
            # degraded, or after the uuid tier has given up.
            #
            # RAISED AFTER THE WAKE, not before it: PollingWatcher's pump
            # sets the shared event a few statements above this call, on the
            # same tick, so the worker can in principle read the guard before
            # this line runs. Nothing here can close that -- the ordering is
            # the poller's. Losing that race leaves this one observation
            # exactly as it was without the flag and spends it on the NEXT
            # observation, which then takes wl-paste for a clip the tier
            # could have answered: one extra fork, never wrong content.
            self._untracked_change.set()
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
                # Stamped AT the flip: this lifts the healthy suppression,
                # and the judge's first comparison must not span the
                # suppressed stretch, where signals accumulated with no
                # slow ticks to absorb them.
                self._signals_at_last_tick = self._signals
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
            # start() assigned _process (stdout=PIPE) just above, and
            # nothing reassigns it while the pump lives; pyright cannot
            # carry that across the closure boundary.
            assert self._process is not None
            assert self._process.stdout is not None
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

    def consume_untracked_change(self):
        """One-shot: True iff the safety net saw the selection move while the
        history uuid stood still since the last consume. The read guard
        declines the tier on it -- wl-paste is the only reader that can see
        a clip GPaste did not record (v3.3's excluded-clip contract).

        is_set()-then-clear() is not atomic, and does not need to be: the
        observer worker is the only consumer, by the same rule that lets it
        be the only caller of on_change."""
        was_set = self._untracked_change.is_set()
        self._untracked_change.clear()
        return was_set

    def stop(self):
        self._stop.set()
        # NOT JOINED: stop() runs on the main protocol loop, and a worker
        # parked in a clipboard read can take up to 13s (SUBPROCESS_TIMEOUT +
        # IMAGE_SUBPROCESS_TIMEOUT) -- joining would stall that loop.
        self._event.set()
        self._safety_net.stop()
        if self._process:
            self._process.terminate()


# Backoff for the logind pump's self-heal (spec §3.2.3): doubles on each
# respawn, reset the moment a line parses to something meaningful again.
LOGIND_MONITOR_RESPAWN_SECONDS = 1.0
LOGIND_MONITOR_RESPAWN_MAX_SECONDS = 30.0


class LockMonitor:
    """Caches logind's LockedHint for the one resolved graphical session,
    kept current by a `gdbus monitor --system` pump. The TRANSPORT mirrors
    GPasteWatcher's (gdbus monitor, a pump thread, pdeathsig, terminate on
    stop) but the LIFECYCLE is deliberately its opposite. GPaste's pump is
    born in watcher.start() and dies in watcher.stop(), which under v3.5
    happens at every lock -- a lock monitor with that lifecycle would be
    dead by the time the unlock it exists to observe arrives. This class is
    instead a PROCESS SINGLETON: start() once from main()'s real-clipboard
    branch, stop() only when run() returns, and never wired into
    clipboard_lost (review blocker B2) -- it is the unlock's only observer.

    Fail-open: an unresolved session (zero or several graphical candidates,
    or any probe among them unanswered) or an unread LockedHint reports
    locked() as None, never False -- None and False are opposite evidence,
    the same rule resolve_graphical_session and session_locked_hint each
    already carry one level down."""

    def __init__(self, run=subprocess.run, popen=subprocess.Popen,
                 now=time.monotonic):
        self._run = run
        self._popen = popen
        self._now = now
        # Leaf lock: guards only _locked/_session_path/_since/the two
        # *_logged flags (read or assigned), and is never held across a
        # call to resolve_graphical_session, session_locked_hint, popen, or
        # log -- every one of those can block or shell out.
        self._lock = threading.Lock()
        self._locked = None
        self._since = None
        self._session_path = None
        self._fail_open_logged = False
        # Fail-open's SECOND door (spec §6): a resolved session whose Get
        # itself fails, as distinct from _fail_open_logged above (a session
        # that never resolved at all). Cleared in _set_locked, not here or
        # in _read_hint -- a later answer can arrive as either a fresh Get
        # (this thread) or a parsed PropertiesChanged line (the pump
        # thread), and _set_locked is the one funnel both go through.
        self._hint_unreadable_logged = False
        self._stop = threading.Event()
        self._process = None
        self._thread = None

    def start(self):
        # Resolve, THEN subscribe, THEN take the initial Get, THEN start
        # the reader thread that drains the subscription -- in that exact
        # order. Subscribing before the Get (spec §3.2 step 2) is what
        # lets a transition landing in the gap self-correct rather than
        # being lost: the monitor is already capturing it into its own
        # pipe. But that only holds ONE-WAY, and review caught the
        # reverse: starting the reader thread before the Get let the two
        # race, and whichever finished LAST won, even backwards -- the
        # reader could apply a real transition and then the in-flight Get
        # (answered from before the lock) would clobber it back to stale.
        # Doing the Get on THIS thread before the reader thread exists at
        # all makes the sequence single-threaded and total: nothing can
        # land between "the Get returns" and "the reader starts draining
        # whatever the pipe queued up meanwhile", because there is no
        # reader yet to race it. This also removes the one other race a
        # thread-order fix could have left behind: a churn-triggered
        # _reresolve() on the reader thread swapping _session_path out
        # from under this method's own stale local `path` -- with the
        # reader not yet running, that swap cannot happen concurrently
        # with the line below.
        path = self._resolve_session()
        try:
            self._process = self._spawn()
        except OSError as error:
            # Unlike _pump()'s own respawn (already wrapped, below), THIS
            # spawn runs on the CALLER's thread -- main(), before
            # send_hello -- with no reader thread built yet to catch
            # anything. A missing/unstartable gdbus must not raise out of
            # here: that would kill the agent before the protocol even
            # begins, and the Mac would reconnect forever against a
            # crashing peer (spec §3.2: "a machine where none of this
            # works behaves exactly as today"). Cache stays at __init__'s
            # None (fail-open) and no reader thread starts -- there is
            # nothing for it to read.
            log("lock monitor could not start gdbus (%s): lock gate inactive, "
                "readiness is socket-only" % type(error).__name__)
            return
        self._read_hint(path)
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        # NOT JOINED, mirroring GPasteWatcher.stop(): the caller may be the
        # main protocol loop. terminate() plus the reader noticing EOF is
        # what actually ends the thread, not this call.
        if self._process is not None:
            self._process.terminate()

    def locked(self):
        with self._lock:
            return self._locked

    def state(self):
        """(locked, since) -- `since` is the monotonic stamp of the LAST
        transition, from the injected clock. A plain tuple snapshot, safe
        to read from any thread without the caller taking a lock."""
        with self._lock:
            return self._locked, self._since

    def _spawn(self):
        return self._popen(
            ["gdbus", "monitor", "--system", "--dest", LOGIND_BUS_NAME],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, preexec_fn=_pdeathsig_preexec,
        )

    def _resolve_session(self):
        """Which session to trust, or None -- never called with _lock held,
        since resolve_graphical_session shells out. Logs the fail-open line
        exactly on the transition into it: a resolver that keeps failing
        stays silent after the first line, but a later success re-arms it,
        so a SECOND, separate failure (spec §3.2: a relogin can pass
        through a two-candidate moment more than once) is not mute."""
        path = resolve_graphical_session(run=self._run)
        log_now = False
        with self._lock:
            self._session_path = path
            if path is None:
                log_now = not self._fail_open_logged
                self._fail_open_logged = True
            else:
                self._fail_open_logged = False
        if log_now:
            log("no unique graphical session: lock gate inactive, readiness is socket-only")
        if path is None:
            self._set_locked(None)
        return path

    def _read_hint(self, path):
        """The initial (or post-re-resolve) LockedHint of `path`; a no-op
        when resolution itself failed -- there is nothing to Get.

        A resolved session whose Get itself fails (timeout, non-zero exit,
        an unparseable reply) is fail-open's SECOND door (spec §6): the
        resolver's own "no unique graphical session" line names a
        different fact and must stay silent here -- failing to resolve a
        session and failing to read one that DID resolve are two things an
        incident review needs told apart. Logged once per transition into
        unreadable; the re-arm lives in _set_locked, not here, because the
        transition OUT can arrive as either a later Get succeeding (this
        thread) or a PropertiesChanged line parsing (the pump thread).
        """
        if path is None:
            return
        hint = session_locked_hint(path, run=self._run)
        if hint is None:
            log_now = False
            with self._lock:
                log_now = not self._hint_unreadable_logged
                self._hint_unreadable_logged = True
            if log_now:
                log("lock state unreadable on the watched session: "
                    "lock gate inactive until it answers")
        self._set_locked(hint)

    def _reresolve(self):
        """Re-resolve and re-read, back to back -- unlike start(), there is
        no subscribe step to interleave here: the pump is already
        running."""
        self._read_hint(self._resolve_session())

    def _set_locked(self, value):
        with self._lock:
            if value is not None:
                # A real answer -- a Get or a parsed line alike -- re-arms
                # the unreadable-hint door (spec §6) for its NEXT failure.
                # Unconditional on whether `value` actually moves
                # `_locked`: a repeated same-value answer is still a real
                # answer, and must not leave a stale failure logged mute.
                self._hint_unreadable_logged = False
            if value != self._locked:
                self._locked = value
                self._since = self._now()

    def _watched_path(self):
        with self._lock:
            return self._session_path

    def _pump(self):
        """Reads the gdbus monitor's stdout for the life of the process,
        self-healing on EOF (spec §3.2.3): a systemd-logind restart, or
        anything else that ends the child, is covered by re-spawning and
        re-resolving rather than by watching NameOwnerChanged (retracted in
        review -- a monitor scoped to --dest org.freedesktop.login1 may
        never see one).

        The whole iteration is wrapped: this thread is the unlock's SOLE
        observer (unlike GPasteWatcher's pump, whose spawn runs on the
        CALLER's thread and which has a polling tier behind it regardless),
        so an unanticipated exception here -- e.g. a respawn's Popen
        raising because gdbus went missing -- must not kill the thread and
        freeze the cache at whatever it last held, including True: that is
        the gate wedged closed, silently, forever. Falling open plus
        letting the loop come back around is a deliberate choice over
        parking: the respawn/backoff machinery below already exists and
        already retries on ordinary EOF, so reusing it here for an
        exception costs nothing new, and a transient failure (as opposed to
        a standing one, which will just keep re-raising and re-logging
        behind the same backoff) gets a real chance to self-heal instead of
        wedging the gate for the rest of the process's life."""
        delay = LOGIND_MONITOR_RESPAWN_SECONDS
        while True:
            try:
                # Assigned by start() before this thread exists, then only
                # by the respawn below -- stdout=PIPE both times. Asserted
                # per iteration; pyright sees Optional either way.
                assert self._process is not None
                assert self._process.stdout is not None
                for line in self._process.stdout:
                    if self._stop.is_set():
                        return
                    outcome = parse_logind_monitor_line(line, self._watched_path())
                    if outcome is None:
                        continue
                    if outcome == "locked":
                        self._set_locked(True)
                    elif outcome == "unlocked":
                        self._set_locked(False)
                    elif outcome == "resolve":
                        self._reresolve()
                    delay = LOGIND_MONITOR_RESPAWN_SECONDS
                # EOF: the child exited on its own. stop() sets the flag
                # this honors, both before the wait (already requested) and
                # during it (wait() returns early the instant set() runs
                # elsewhere).
                if self._stop.is_set() or self._stop.wait(delay):
                    return
                delay = min(delay * 2, LOGIND_MONITOR_RESPAWN_MAX_SECONDS)
                self._process = self._spawn()
                self._reresolve()
            except Exception as error:
                self._set_locked(None)
                log("lock monitor pump hit an unexpected %s: %s -- falling open and retrying"
                    % (type(error).__name__, error))


class PollingWatcher:
    """Compare-and-notify against the clipboard: fork wl-paste, take a probe()
    token, signal on change. One `pump` loop serves three roles: GPasteWatcher's
    slow tier, its degraded backoff, and the standalone no-GPaste poller.

    Two independent gates, never merged: `should_probe` (the idle gate) skips
    the whole tick, backstop included; `probe_suppressed` (v3.6, GPasteWatcher's
    trust predicate) skips the fork and the judge but keeps servicing
    on_idle_tick -- while GPaste is trusted the reoffer backstop is the only
    thing a tick still owes."""

    def __init__(self, clipboard, interval_seconds, on_tick=None, event=None,
                 on_idle_tick=None, should_probe=None, probe_suppressed=None):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._on_tick = on_tick
        self._on_idle_tick = on_idle_tick
        self._should_probe = should_probe
        self._probe_suppressed = probe_suppressed
        self._stop = threading.Event()
        self._thread = None
        self._event = threading.Event() if event is None else event
        self._owns_the_worker = event is None
        self._worker = None
        self._retry_interval = None

    def available(self):
        return True

    def _probing_suppressed(self):
        return self._probe_suppressed is not None and self._probe_suppressed()

    def start(self, on_change=None):
        """`on_change` is required standalone; ignored when this poller shares someone else's event (that owner's worker calls the handler)."""
        if self._owns_the_worker:
            self._worker = _start_observer(self._event, self._stop, on_change)

        def pump():
            """Poll loop: probe() on a timer, signal the event on change (or recovery from an unresolved run), and forward tokens to on_tick."""
            previous = None
            try:
                # The baseline is under the suppression too: the watcher is
                # rebuilt on every reconnect, so an ungated baseline is a
                # fork at wake -- the worst moment for a hang and its banner.
                if not self._probing_suppressed():
                    previous = self.clipboard.probe()
            except Exception as error:
                _handle_observer_error(error, "poll")
            while not self._stop.wait(self._retry_interval or self.interval):
                try:
                    if self._should_probe is not None and not self._should_probe():
                        continue
                    if self._probing_suppressed():
                        # AFTER the idle gate, which still skips the whole
                        # tick, backstop included -- a forced read against an
                        # idle/dark session is what it exists to prevent. A
                        # suppressed tick keeps only the backstop: the one
                        # forced read healthy mode still owes, run on the
                        # observer, not here. No probe, no judge -- `previous`
                        # stays None, so the first probe after the suppression
                        # lifts always signals: one extra full read, absorbed
                        # by the content dedup, never a swallowed change.
                        if self._on_idle_tick is not None and self._on_idle_tick():
                            self._event.set()
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
                            read_tracking=gpaste_tracking,
                            suppress_healthy_probes=True)
    if watcher.available():
        watcher.tier = GPasteTextTier()
        if degraded:
            log("watching the clipboard through GPaste, already diagnosed as silent "
                "this connection, so polling every %.1fs" % fallback_interval_seconds)
        else:
            log("watching the clipboard through GPaste; no timed clipboard reads "
                "while healthy, safety-net poll every %gs on fallback"
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
    # LockMonitor + LockGate are built HERE, beside this one call, and not
    # inside _select_clipboard()'s real branch: selftest() above also calls
    # _select_clipboard() (with no gate), and CI's `--selftest` step runs
    # with CLIPWIRE_FAKE_CLIPBOARD unset -- so building the monitor inside
    # _select_clipboard() would give every selftest run a live `gdbus
    # monitor --system` subprocess and its self-healing reader thread, with
    # no caller in a position to stop() either. Spec §3.2: "started once
    # from main()'s real-clipboard branch."
    monitor = None
    lock_gate = None
    if not _fake_clipboard_requested():
        monitor = LockMonitor()
        monitor.start()
        lock_gate = LockGate(monitor.state)
    agent = Agent(
        stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
        clipboard=_select_clipboard(lock_gate=lock_gate),
    )
    try:
        return agent.run()
    except FrameError as error:
        log("protocol error: %s" % error)
        return 2
    finally:
        # The monitor must survive every lock -- it is the unlock's only
        # observer (Task 4) -- so stop() belongs ONLY here, bounded to
        # run() actually returning (by return OR by exception), and
        # NOWHERE else: not in clipboard_lost, not beside the watcher's own
        # teardown (review blocker B2).
        if monitor is not None:
            monitor.stop()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
