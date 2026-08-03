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

# Three separate bounds, and they must stay separate even while two of them
# hold the same number. The frame cap is what the decoder enforces; the two
# content limits are what the senders enforce BEFORE wrapping a body in its
# 8-byte timestamp. v2 used one constant for all of it, which made a
# maximum-size image unsendable while looking like it was within the limit.
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
    # Fix round 1, Finding 2: struct.unpack(">d", ...) decodes ANY 8-byte
    # pattern into a valid IEEE-754 double, including inf/-inf/nan -- there
    # is no bit pattern it rejects, unlike an out-of-range integer literal
    # in JSON (decode_clip_state's own hazard). Guarding it here, at the
    # one place both callers share, follows the same precedent as that
    # earlier fix: _write_clip already swallows ClipPayloadError for a
    # too-short or empty-text payload, so folding a non-finite ts into the
    # SAME exception type here means it is swallowed the same quiet way,
    # with no new call-site-specific handling needed. Left unguarded, a
    # clip queued while pending with e.g. ts=inf would decode fine here,
    # get WRITTEN to the clipboard by _write_clip (its own persistence
    # attempt would fail and be logged, caught there) -- but
    # clipboard_became_ready's own follow-up
    # encode_clip_state(*applied_pending) call reuses that same ts with no
    # local try/except, so ClipStateError would escape uncaught and tear
    # down the whole connection over a clip that had ALREADY been applied.
    if not math.isfinite(ts):
        raise ClipPayloadError("clip payload ts must be finite, got %r" % ts)
    return ts, bytes(payload[TIMESTAMP_BYTES:])


def encode_image_payload(ts, png):
    """type-0x03 payload: [f64 BE ts][PNG bytes].

    Same shape as the text clip and for the same reason -- the receiver has to
    record the PEER's timestamp for content it applies, and it cannot record
    what the wire never carried. The body is opaque here: PNG validity is the
    business of whoever read it off a clipboard, not of the codec.

    Unlike encode_clip_payload, this rejects a non-finite ts on ENCODE too,
    not just decode: encode_clip_payload's two existing callers only ever
    pass a ts that has already been validated finite by an earlier decode or
    a fresh time.time() reading, and this function's own two callers (the
    reconciliation send branch's mine[1], and _local_change's observed_at)
    do too. The guarantee is enforced here, at the source, rather than
    leaned on at each call site, matching what the tests below require --
    written when this function had no callers yet to lean on in the first
    place, and left that way on purpose once it did: the invariant belongs
    to the codec, not to whichever callers happen to exist today.
    """
    if not math.isfinite(ts):
        raise ClipPayloadError("refusing to encode a non-finite ts: %r" % ts)
    return struct.pack(">d", ts) + png


def decode_image_payload(payload):
    """Inverse of encode_image_payload. Checks are ordered the same as
    decode_clip_payload's: length, then ts finiteness, both before the body
    is even looked at -- only the last check differs, since an empty image
    body is never representable (unlike an empty clip TEXT, which
    test_empty_text_is_representable above pins as legal)."""
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

# A hash alone cannot tell the two sides what they are agreeing about, so
# clip-state carries a `kind` alongside `sha256`/`ts` (Task 6). Only two
# kinds exist today; a peer announcing anything else is rejected at decode
# (see decode_clip_state) so an unknown kind can never reach the send
# branch that switches on it (Task 11).
KIND_TEXT = "text"
KIND_IMAGE = "image"
_KNOWN_KINDS = (KIND_TEXT, KIND_IMAGE)


class ClipStateError(FrameError):
    pass


def encode_clip_state(sha256, ts, kind, origin=None):
    """type-0x02 payload: {"sha256": <hex or null>, "ts": <float>, "kind":
    <"text" | "image" | null>} plus, when there is one, "origin": <hex>.

    Refuses a non-finite ts (nan/inf/-inf) rather than emitting one: Python's
    json.dumps would otherwise happily write a bare NaN/Infinity token that
    is not valid JSON, which Swift's JSONDecoder rejects outright -- so a
    non-finite ts stored locally would silently break the *peer's* handshake
    instead of failing here, on the side that produced it.

    `origin` (v3.2) names the hash THIS content was born from: the peer's
    own hash, recorded when this side wrote the peer's bytes to the
    clipboard and read different ones back. It is announced, never deduced
    -- see resolve_provenance below for what reads it, and the v3.2 design
    for why every attempt to infer the same fact from content is dead.

    The key is OMITTED when there is no origin, rather than written as an
    explicit null. Absent and null already mean the same thing to both
    decoders (dict.get returns None for either; Swift's decodeIfPresent
    returns nil for either), so omitting costs nothing in meaning and buys
    two things: every payload this side produces without an origin -- wire
    frame AND store file, since save_clip_state shares this encoder -- stays
    byte-for-byte what it was before v3.2, and the shape matches what
    Swift's synthesized encoder does with a nil Optional. `kind` writing an
    explicit null is not a precedent against this: `kind` is null-iff-null
    with sha256, so its absence would be a shape error rather than a
    default.

    Neither `kind` nor `origin` is validated here: the decode-side rules
    (kind null iff sha256 is null and otherwise one of the known kinds; an
    origin only ever beside a non-null sha256 -- see decode_clip_state)
    exist to police PEER-controlled input arriving off the wire. Every
    caller here is this agent's own code, already holding a state it
    derived correctly a moment earlier; there is no peer to protect against
    on this side of the codec.
    """
    if not math.isfinite(ts):
        raise ClipStateError("refusing to encode a non-finite ts: %r" % ts)
    state = {"sha256": sha256, "ts": ts, "kind": kind}
    if origin is not None:
        state["origin"] = origin
    return json.dumps(state).encode()


def _is_sha256_hex(value):
    """Exactly 64 characters of [0-9a-f] -- the shape, and the ONLY shape,
    hashlib.sha256(...).hexdigest() produces and the wire contract allows.

    This is not defensive typing; it is what keeps the cross-language
    comparison valid. Both sides compare hashes with a plain ordering
    operator, but they do not order strings the same way: Python orders by
    code point, Swift's String by canonical Unicode equivalence. Those two
    coincide over lowercase hex and nowhere else -- verified by execution,
    not assumed: Python puts U+00C5 ABOVE the canonically equivalent
    "A" + U+030A, while Swift calls those exact two strings EQUAL. A peer
    announcing the composed form against a local decomposed one therefore
    makes Swift resolve doNothing while this side resolves WAIT_FOR_PEER --
    both sides wait, and the clip is lost with nothing logged on either
    machine.

    Checked with an explicit alphabet rather than int(value, 16), which
    would accept uppercase, a leading sign, and "_" digit separators, or
    str.isalnum()/isdigit(), which accept whole ranges of non-ASCII digits.
    """
    return len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def decode_clip_state(payload):
    """Inverse of encode_clip_state. Raises ClipStateError — a FrameError,
    so main()'s existing `except FrameError` closes the connection exactly
    as a malformed hello does — on anything that is not a well-formed
    {"sha256": <str or null>, "ts": <finite number>, "kind": <"text" |
    "image" | null>} object, optionally carrying an "origin".

    Returns a (sha256, ts, kind, origin) QUADRUPLE as of v3.2, where every
    caller before it took a triple. An absent "origin" key decodes as None,
    which is what a pre-v3.2 peer's announcement and a pre-v3.2 store file
    both are: the field is optional in the only sense that matters, so a
    mismatched pair degrades to exactly what shipped before rather than
    failing.

    The finiteness check is the load-bearing part: json.loads, unlike
    Swift's JSONDecoder, accepts a bare NaN/Infinity/-Infinity and hands
    back a float that compares False against everything (nan > x, nan < x,
    and nan == nan are all False). Silently letting that reach
    resolve_freshness would compare a non-finite ts against a real one and
    send the decision somewhere neither side expects — so it is rejected
    here, before the value ever reaches a comparison, rather than compared.

    Two more rules police `kind`, both enforced here because the wire is
    peer-controlled input: `kind` must be None exactly when `sha256` is
    None (a hash with no kind, or a kind with no hash, is malformed), and
    otherwise must be one of _KNOWN_KINDS -- an unknown kind must never
    reach the send branch (Task 11), which switches on it. Deliberately
    NOT applied to encode_clip_state: that side only ever emits a triple
    this agent's own code already derived correctly, and there is no peer
    to protect against there.

    Note this is peer-controlled input's decoder, but it is ALSO what
    load_clip_state (below) reuses to read this agent's own store back --
    which is exactly what makes a v2-era store file on disk (a real
    sha256, no "kind" key at all) fail the null-iff-null rule the same way
    a malformed wire payload would, rather than loading silently as "a
    hash of unknown kind" (test_clip_state_store.py's
    TestV2StoreIsRejected).
    """
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
    # `bool` is a SUBCLASS of int in Python, so a bare isinstance(ts, (int,
    # float)) admits JSON `true` and hands back 1.0 -- a real, finite,
    # comparable timestamp from 1970, manufactured out of a field the peer
    # controls, on the one input class this decoder exists to police. Swift's
    # own decoder throws typeMismatch for the same payload, so without this
    # the two sides disagree about whether a clip-state frame is even
    # well-formed: this one reconciles against a fabricated age while the Mac
    # closes on the frame. skew_log_line, further down this file, already
    # spells the same guard out explicitly for the same reason; this mirrors
    # its wording rather than inventing a second spelling of one rule.
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        raise ClipStateError("malformed clip-state payload: ts must be a number")
    try:
        finite = math.isfinite(ts)
    except OverflowError:
        # json.loads parses an integer literal as arbitrary-precision int,
        # unlike a float literal (1e400 already becomes inf, caught by the
        # ordinary isfinite check below). A 400-digit integer ts instead
        # passes the isinstance check above and only fails inside
        # math.isfinite's int-to-float conversion, raising a bare
        # OverflowError -- not a ClipStateError, and therefore not a
        # FrameError, so main()'s `except FrameError` would not catch it.
        #
        # Before this fix there was no call site where that mattered: the
        # one existing caller, load_clip_state, already swallows
        # OverflowError at ITS OWN call site, because that input is this
        # agent's own prior write to its own disk. Agent._on_clip_state
        # (added by this task) decodes a peer-controlled wire payload with
        # no such local guard, deliberately mirroring _on_hello's existing
        # bare `raise FrameError(...)` -- so the fix belongs here, at the
        # source both callers share, rather than duplicated at each site.
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
    # NOT the null-iff-null rule `kind` gets one line above, and copying that
    # shape here would reject every ordinary announcement this protocol has
    # ever sent: content with a hash and no origin is the normal case -- an
    # origin is the rare one. The rule is one-directional. An origin says
    # "what I hold was born from this hash", so it needs something of ours
    # for it to describe; a peer announcing an origin beside a null sha256
    # is claiming an ancestor for content it does not have, which
    # resolve_provenance could only ever compare against nothing.
    if origin is not None and sha256 is None:
        raise ClipStateError(
            "malformed clip-state payload: origin is only admissible beside a sha256"
        )
    # Deliberately NOT run through _is_sha256_hex, unlike sha256 above, and
    # the asymmetry is reasoned rather than overlooked. That check exists
    # because the two sides do not order or compare strings the same way
    # (Python by code point, Swift by canonical equivalence), and both sides
    # must reach the same verdict. Provenance never compares an origin
    # against another origin: every comparison resolve_provenance makes puts
    # an origin beside a sha256, and every sha256 in the comparison is
    # either this side's own sha256_hex output or a peer hash this decoder
    # has already forced through _is_sha256_hex. Canonical equivalence
    # cannot equate a pure-ASCII string with any other sequence -- nothing
    # decomposes to ASCII -- so with one operand guaranteed hex, Swift's ==
    # and Python's == cannot disagree, whatever the other operand is. Should
    # a later rule ever compare an origin against an origin, this reasoning
    # expires and the check has to be added.
    return sha256, float(ts), kind, origin


def resolve_freshness(mine, peer):
    """Decides which side sends once both have announced what they hold.

    `mine` and `peer` are (sha256, ts) pairs. Task 6 grew decode_clip_state
    et al. to a (sha256, ts, kind) triple, and deliberately did NOT grow
    this function to match: SHA-256 of text and of a PNG will not collide,
    so hash equality stays safe, differing hashes are still decided by
    timestamp, and the hex tie-break still works across kinds exactly as
    within one. `kind` is for the send branch (Task 11) and the log (Task
    14), never for this comparison -- callers holding a triple pass only
    its first two elements (see Agent._resolve_clip_state). Mirrors
    Sources/clipwire/Freshness.swift's resolveFreshness one branch at a
    time, including the tie-break, so the two files read side by side as
    one formula rather than a mirrored pair of conditions: mirrored
    conditions drifting apart has already bitten this project twice.

    Timestamps are never compared when either hash is None: that comparison
    is exactly what would put a float next to a None and raise TypeError,
    taking this agent down on every handshake with an empty clipboard --
    which is to say after every PC reboot.
    """
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
    """Is either side's content descended from the other's? True when it is.

    `mine` and `peer` are whole (sha256, ts, kind, origin) records --
    decode_clip_state's output shape -- NOT the (sha256, ts) pairs
    resolve_freshness takes one function above. That difference is
    deliberate and load-bearing: `resolve_provenance(mine[:2], peer[:2])`,
    copied from the call one line above it, would compare a timestamp
    against a hash, return False forever, and never fail a test. Passing
    a truncated record here raises ValueError instead, loudly, at the
    call site.

    Runs BEFORE resolve_freshness, never inside it. The freshness formula
    is exactly what it has been since v2, pinned by fixtures/freshness.json,
    and this release does not perturb it -- provenance is a separate
    question asked first: two machines can hold the same picture in
    different bytes, and no ordering of two timestamps can say so.

    ONE function holding BOTH comparisons, not one per side. "The Mac's
    rule" and "the PC's rule" is fine as prose and fatal as code: two
    implementations of one idea drifting apart is this project's recorded
    defect shape, which is why this shares fixtures/provenance.json with
    Sources/clipwire/Freshness.swift's resolveProvenance, the way
    resolve_freshness shares fixtures/freshness.json with resolveFreshness.
    Note the rule is symmetric -- swapping mine and peer cannot change the
    answer -- which is the point: both sides stand down together, and
    neither waits for a clip the other has already decided not to send.

    EQUALITY COUNTS ONLY BETWEEN TWO VALUES THAT ARE BOTH PRESENT. `None ==
    None` is True in Python, and nil == nil is true for a Swift Optional,
    so the naive spelling of this rule fires on an empty clipboard against
    a peer with no origin -- a locked PC against an ordinary Mac, which
    happens daily -- and stands both sides down. That would kill
    resolve_freshness's (_, None) -> SEND_MINE recovery, the one that hands
    a peer back the clipboard it lost, for EVERY kind of content rather
    than for images. Hence `is not None` on both operands of both
    comparisons: the hash half is redundant in Python (a str never equals
    None) and it is written out anyway, so the rule the fixture's nil rows
    exercise is stated here rather than inferred, and so this reads line
    for line as the same rule Swift's `if let ... let ...` spells out.
    """
    mine_hash, _mine_ts, _mine_kind, mine_origin = mine
    peer_hash, _peer_ts, _peer_kind, peer_origin = peer
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

# How much later than the peer's own timestamp a consumed image re-offer is
# recorded (_consume_image_reoffer). It exists to break one specific tie,
# and the tie is unavoidable: after a Mac->PC image the two machines hold
# DIFFERENT BYTES for the same picture -- the Mac has the PNG it sent, this
# side has GPaste's re-encode of it -- while both record the SAME peer
# timestamp. resolve_freshness then falls through to its hex tie-break, and
# the winner is decided by hash bytes. One ordering has this side push the
# re-encode once and converge; the other has the Mac re-send the original,
# GPaste re-encode it back to exactly what was there before, and the next
# reconnect repeat it identically. A loop with no exit, chosen by a coin.
#
# A millisecond, and both bounds are deliberate:
#
#   * Large enough to survive every serialization on the path. At epoch
#     magnitude (~1.75e9) a double's ULP is about 2.4e-7 s, so a millisecond
#     is thousands of representable steps -- and json.dumps/json.loads and
#     struct.pack(">d") are all exact round trips, as is Swift's own Double
#     coding, so `>` on the far side sees it. A 1-ULP nudge would also
#     survive today, but it would vanish under any future formatting that
#     rounds, and it reads as a rounding artifact rather than a decision.
#
#   * Small enough that it can never leapfrog real content. A clip genuinely
#     copied after the image carries the moment its own watcher observed it,
#     which is at minimum a poll interval later -- hundreds of milliseconds
#     on the Mac, whole seconds here. Nothing a user can do lands inside one
#     millisecond of the applied image's own timestamp.
#
# Degenerate case, stated rather than guarded: for an absurd (but finite)
# peer ts above ~4.5e12 the addition is lost to floating-point precision and
# the tie-break decides again, exactly as it did before. That is the old
# behaviour, not a new failure, and no real clock reaches it.
REOFFER_TS_NUDGE_SECONDS = 0.001


def log(message):
    """Diagnostics go to stderr. stdout carries frames and nothing else."""
    print(message, file=sys.stderr, flush=True)


# Above this, the two clocks disagree by enough that a freshness comparison
# between them can pick the wrong side. Compared with `>`: five seconds
# exactly is the boundary, not a warning. Written into the message below as
# a literal rather than interpolated, so the Swift twin of that line does not
# have to reproduce a second float-formatting bridge byte for byte; the tests
# pin the literal against this constant.
SKEW_WARN_SECONDS = 5.0


def skew_log_line(peer_sent_at, now):
    """The peer's clock offset from ours, as a log line -- or None when it
    cannot be measured.

    Measures abs(now - sent_at) from the HELLO, never the age of a clip: a
    clip legitimately copied this morning is hours old, so warning on that
    would fire on nearly every handshake and teach everyone to ignore the log.

    Missing, null, non-numeric and non-finite sent_at all mean the same
    thing -- skew is not measurable -- and all return None. An unmeasurable
    peer clock is not a protocol violation, so this neither warns nor raises;
    raising would tear down the connection over a field nothing depends on.

    Mirrors Sources/clipwire/main.swift's skewLogLine one branch at a time,
    including the exact text of both outcomes, the way the two "over the
    frame cap" lines already match.

    The finiteness guard is load-bearing and is this file's third instance of
    the same cross-language codec asymmetry (see decode_clip_state and
    encode_clip_state above): json.loads, unlike Swift's JSONDecoder, accepts
    the bare literals NaN/Infinity/-Infinity, and abs(now - nan) > 5.0 is
    False -- so without it a broken peer would log `peer clock skew nan` and
    silently never warn. OverflowError is caught for the same reason
    decode_clip_state catches it: json.loads parses a 400-digit integer
    literal as an arbitrary-precision int, which passes the isinstance check
    and then cannot be converted to a float at all -- by math.isfinite here,
    and by the subtraction immediately below it, which is the one that would
    otherwise crash the agent on a peer-controlled value.
    """
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
        # Which codec pending_clip's wire payload belongs to. A companion
        # field rather than a (kind, payload) pair, so pending_clip keeps
        # holding the raw payload exactly as it always has -- and read ONLY
        # alongside a non-None pending_clip, which is why nothing resets it
        # when that one is drained.
        #
        # It has to exist, and cannot be inferred later: the two payloads are
        # the SAME shape on the wire ([f64 BE ts][body]), so decoding a text
        # payload as an image succeeds and writes the user's text to the
        # clipboard as image/png -- a wrong kind here is silent at every
        # layer below it. Superseded together with pending_clip by the
        # "keep only the newest" rule, for the same reason.
        self.pending_clip_kind = KIND_TEXT
        self._write_lock = threading.Lock()
        self._watcher = None
        self._last_written = None
        self._write_gen = 0
        # None in production: load_clip_state/save_clip_state then fall
        # back to the real XDG state path (clip_state_path()) on their own.
        # Tests inject a temp path here so no test run ever touches that
        # real location.
        self._clip_state_path = clip_state_path
        # Whether THIS process's one-shot clip-state announcement has
        # already gone out. Unlike the Mac side's ClipStateAnnouncement,
        # there is no reset() here: this agent lives exactly one connection
        # (sshd spawns a fresh process per SSH connection), so "once per
        # connection" and "once per process" are the same thing -- there is
        # no reconnect-within-the-same-process to re-arm for.
        self._clip_state_sent = False
        # A peer clip-state announcement received before OUR OWN side has
        # reconciled and sent its own (i.e. before clipboard_became_ready
        # has run at least once this connection) -- resolved immediately
        # after that reconciliation instead, in clipboard_became_ready
        # itself. Keep only the newest, exactly like pending_clip, for the
        # same reason: a well-behaved peer only ever announces once per
        # connection, so this is a rare, one-shot handoff, not a queue.
        # See _on_clip_state's own doc comment for why resolving against
        # the store before it has been reconciled is unsafe.
        self._pending_peer_clip_state = None
        # Whether the GPaste event source has already been diagnosed as
        # silently dead THIS CONNECTION. Lives here rather than on the
        # watcher, because clipboard_lost() discards the watcher and
        # clipboard_became_ready() builds a fresh one: a mid-connection
        # Wayland flap (a logout/login with the SSH channel still up) would
        # otherwise re-log the diagnosis and put PC->Mac sync back on the
        # 30-second detection budget for another full cycle, on an
        # installation already known to be broken. And re-enabling the
        # gnome-shell extension -- the one thing that actually fixes a dead
        # source -- does not tear down the Wayland session, so a flap is no
        # evidence at all that the source recovered.
        #
        # One process per SSH connection (see _clip_state_sent above), so an
        # Agent-level flag is connection-scoped by construction, which is what
        # "for the rest of the connection" means. Set from the watcher's poll
        # thread via _note_event_source_degraded and read on run()'s thread in
        # clipboard_became_ready: a plain bool with a single writer, so no lock.
        self._event_source_degraded = False
        # Persistent memory of what was last synced between the two
        # machines -- set in _write_clip (content arriving FROM the peer),
        # after a successful send in _local_change (content sent TO the
        # peer), and in _consume_image_reoffer (the peer's own image, as the
        # PC's clipboard re-encoded it). Unlike _last_written/_write_gen (a
        # ONE-SHOT echo suppression, consumed by the very next observed
        # change), this never expires on its own: it is what the Mac already
        # holds, for as long as neither side has genuinely changed it. See
        # _local_change for why a one-shot echo alone is not enough.
        #
        # A (kind, hash) pair, or None -- never the content itself. One
        # rule for both kinds, not text remembered by value and images by
        # hash: a second comparison branch is exactly the kind of mirrored
        # drift this project has already been bitten by twice, and holding
        # a hash instead of bytes is what keeps a synced image's pixels out
        # of memory here. _observe_local_change compares against this field
        # under BOTH kinds: KIND_TEXT on its own text path, and KIND_IMAGE
        # through _consume_image_reoffer, which needs it to tell our own
        # write's echo apart from the re-offer that follows it. So the kind
        # half is load-bearing rather than merely ready. FIVE sites produce a
        # KIND_IMAGE pair: _write_clip (an applied image), _consume_image_reoffer
        # (GPaste's re-encode of it), clipboard_became_ready's connect-time
        # seed, _observe_local_change's own image send, and
        # _resolve_clip_state's SEND_MINE branch, which is the one an earlier
        # count of "four" missed -- it was added with the send branch's
        # verification, one task after this comment was written.
        #
        # "Never the content itself" includes text, which Task 9 briefly kept
        # a companion _last_seen_text for, so _resolve_clip_state's SEND_MINE
        # branch could send remembered bytes without re-reading the clipboard.
        # Task 11 made that branch VERIFY the clipboard against what was
        # announced before sending it -- the very re-read the companion
        # existed to avoid -- so the field went with it, and the rule above
        # is unqualified again.
        self._last_seen = None
        # What this side needs to remember about an image it has just
        # applied and is still waiting to see GPaste re-offer -- or None
        # when no re-offer is expected. A (peer_ts, peer_sha256, written_at)
        # TRIPLE since v3.2, where it was the bare peer timestamp before;
        # see _write_clip, which produces all three from the one write, for
        # what each is for. Set by _write_clip on an image write, spent by
        # _consume_image_reoffer, and cleared four ways: by a TEXT WRITE
        # (_write_clip's own `ts if kind == KIND_IMAGE else None` -- an
        # applied text clip replaces the selection, so the image's re-offer
        # will never come, and a stale expectation would swallow the next
        # image instead), by a text OBSERVATION (_observe_local_change: the
        # user moved on, same reasoning from the other direction), by
        # clipboard_became_ready (a fresh connection or a Wayland flap: the
        # session the write went into is gone), and -- since v3.2 -- by
        # _consume_image_reoffer's DISARM, when the clipboard has gone on
        # offering our own bytes for longer than the detection budget, which
        # is what "there is no GPaste on this machine" looks like from here.
        # All four clear it for one reason -- the re-offer this was armed
        # for can no longer arrive -- and the write is easy to miss
        # precisely because it is the same assignment that arms it.
        #
        # It exists because the PC's clipboard does not necessarily hold what
        # was put in it. Measured on the live machine: a 105,700-byte PNG
        # written here read back identical at t+1s, then as a DIFFERENT
        # 180,287-byte PNG at t+4s and t+7s, stable from then on -- GPaste
        # takes over the selection a few seconds after the write and
        # re-encodes the image. Text is unaffected. Nothing in the protocol
        # can prevent that, so the agent has to recognise the re-offer as its
        # own write rather than as a fresh local clip; without it every
        # screenshot costs a guaranteed extra round trip back to the Mac, and
        # -- worse -- the store holds a hash the clipboard does not offer, so
        # EVERY reconnect resolves "clipboard changed while apart", stamps
        # ts=now, and lets a stale image win reconciliation against fresher
        # content on the Mac.
        #
        # Deliberately NOT a timer: the takeover was measured between one and
        # four seconds, on one machine, on one day, so any fixed wait is a
        # race dressed as a constant. The expectation is spent by an EVENT
        # instead -- the first image observation whose hash differs from
        # _last_seen -- which is the echo guard's own shape, one door over.
        # v3.2's disarm does not weaken that: `written_at` is never a
        # deadline for the re-offer to beat, it only bounds how long an
        # UNCHANGED clipboard keeps the expectation alive, and an
        # observation that differs is consumed whatever the clock says.
        #
        # It holds the peer's timestamp rather than a bare flag because the
        # store write it authorises needs one, and it must be derived from
        # the PEER's: the re-offer is not a new clip, it is the applied one
        # re-encoded, so stamping it with the moment it was observed would
        # make content the Mac sent us look freshly copied here and beat
        # anything the Mac copied in between. What _consume_image_reoffer
        # actually stores is this value plus REOFFER_TS_NUDGE_SECONDS,
        # because the peer's value EXACTLY leaves the two sides at an
        # identical ts holding different bytes -- see that constant. Tested
        # with `is not None`, never for truth: the record is a tuple now,
        # but its first element is still a ts of 0.0 for a clip copied at
        # the epoch, and the habit of checking the field for truth is what
        # that would break.
        self._expect_reoffer = None
        # Guards _last_written/_write_gen/_last_seen/_expect_reoffer only. A
        # separate lock from _write_lock (which guards stdout) on purpose:
        # nesting them would invite a deadlock later, and this one is held
        # across nothing that ever blocks.
        self._echo_lock = threading.Lock()
        # Serializes _local_change against ITSELF. A third lock rather than a
        # wider _echo_lock, deliberately: this one IS held across a wl-paste
        # round trip and a send, and widening _echo_lock to cover those would
        # block _write_clip on run()'s thread for the whole round trip --
        # exactly what _local_change's own comment explains it must not do.
        # Nothing on run()'s thread ever acquires this one, so it blocks only
        # the two watcher threads against each other. Acquired first and
        # released last within _local_change, and never held while acquiring
        # it, so the lock order _observe_lock -> _echo_lock -> _write_lock is
        # total and acyclic.
        self._observe_lock = threading.Lock()

    # --- outbound -------------------------------------------------------

    def hello_payload(self):
        # sent_at is this side's clock at the moment of sending; the peer
        # measures its own clock against it (our own twin of that is
        # _on_hello below). Read fresh on every call (not cached at import
        # time) since "the moment of sending" is exactly when this method
        # runs.
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
            # Through the same funnel as TYPE_CLIP, carrying the kind rather
            # than duplicating the phase check and the queue-the-newest rule
            # in a second method: an image arriving before the Wayland
            # session appears must queue exactly as text does, and two copies
            # of that rule is how the two drift.
            self._on_clip(payload, KIND_IMAGE)

    def _on_hello(self, payload, now=None):
        """`now` is injectable for the same reason handleFrame's is on the
        Mac side: skew is a comparison against this side's clock, and a test
        that read the real one could only assert vaguely, or sleep."""
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
        # Matched peers only: a clock reading from one we are about to hang
        # up on is noise next to the mismatch itself.
        line = skew_log_line(peer.get("sent_at"), now)
        if line is not None:
            log(line)

    def _on_clip(self, payload, kind=KIND_TEXT):
        """Both inbound clip types land here: TYPE_CLIP as KIND_TEXT (the
        default, which is why on_frame's own text branch passes nothing) and
        TYPE_IMAGE_CLIP as KIND_IMAGE. `kind` says which codec `payload`
        belongs to; it is not read from the payload, because the two wire
        shapes are identical and nothing in the bytes distinguishes them."""
        if not payload:
            return
        if self.phase != PHASE_READY:
            # Keep only the newest. Replaying a backlog into clipboard history
            # once the session appears is noise, not a feature. The kind is
            # superseded with it -- see pending_clip_kind on why a stale one
            # would be applied silently rather than failing to decode.
            self.pending_clip = payload
            self.pending_clip_kind = kind
            return
        self._write_clip(payload, kind)

    def _on_clip_state(self, payload):
        """Decodes an incoming clip-state announcement, then either
        resolves it immediately or stashes it for clipboard_became_ready to
        resolve once our own side has reconciled.

        decode_clip_state is called bare, not wrapped in a local try/except
        -- one deliberate divergence from Sources/clipwire/main.swift's
        handleFrame .clipState case, which logs the error and drops the
        frame rather than closing the channel. That is correct THERE
        because Channel exposes no way to force-close the ssh process from
        that side -- but this agent, spawned fresh per SSH connection by
        sshd, both CAN and already DOES close the connection on a
        malformed/mismatched hello (_on_hello's own bare `raise
        FrameError(...)`, unchanged by this task). A malformed clip-state is
        the same class of peer violation, so it is handled the same way:
        propagate ClipStateError (a FrameError) up through main()'s `except
        FrameError`, which is exactly what closing the OverflowError hole
        in decode_clip_state makes safe to do.

        Fix round 1, Finding 1: run()'s real loop dispatches every complete
        frame on stdin BEFORE it ever checks clipboard.ready() in that same
        iteration (see run()'s own comment) -- so this can be called while
        still PHASE_PENDING, before clipboard_became_ready has EVER run
        this connection. Unlike Swift, where announceClipState reconciles
        and persists the store SYNCHRONOUSLY inside the same matched-hello
        handler that then answers a peer's own announcement -- guaranteed
        by hello arriving first on an ordered stream -- this agent's own
        reconciliation is bound to a LATER, independent event (Wayland
        session readiness). Resolving against load_clip_state() before that
        reconciliation has run risks exactly the store being stale: content
        predates this process (the ordinary case this whole design exists
        for -- ANY reconnect, not only a reboot), and nothing has yet
        compared it against what the clipboard actually holds right now.
        A peer's announcement that happens to still match that stale value
        would resolve doNothing and never be reconsidered -- v1's silent
        loss, reintroduced through an event-ordering race instead of the
        multi-process one this design was built to close. Stashing here and
        resolving in clipboard_became_ready, once the store is known-fresh,
        mirrors pending_clip's identical pattern for the identical class of
        ordering problem.
        """
        peer = decode_clip_state(payload)
        if not self._clip_state_sent:
            self._pending_peer_clip_state = peer
            return
        self._resolve_clip_state(peer)

    def _resolve_clip_state(self, peer, mine=None):
        """The resolution logic _on_clip_state defers until our own side
        has reconciled -- see its own doc comment for why. Called either
        directly (a clip-state arriving after clipboard_became_ready has
        already run once this connection) or from clipboard_became_ready
        itself (a clip-state that arrived before it and was stashed).

        `mine` is that second caller's own just-computed (sha256, ts, kind)
        state, passed in rather than re-derived. It is the authoritative value by
        construction: clipboard_became_ready computed it one line earlier
        and ANNOUNCED IT TO THIS VERY PEER. Re-loading the store instead
        only diverges when the store cannot be read back -- and since every
        save_clip_state call site swallows its failure, an
        unwritable state directory is exactly that, silently. The fallback
        below would then rebuild the pair from a fresh clipboard read
        stamped time.time(), so the value we reconcile with would not be
        the value on the wire: an age we invented, inflated past the one we
        announced, able to win a comparison it should have lost.

        The direct caller passes nothing: a clip-state arriving after this
        connection already reconciled has no just-computed pair to offer,
        and the store is the right source there."""
        if mine is None:
            mine = load_clip_state(path=self._clip_state_path)
        if mine is None:
            # load_clip_state should already reflect our own current state --
            # from this connection's own announcement, or an ordinary
            # local-change/applied-clip save since -- so this fallback only
            # matters if an earlier save failed. It must still resolve a REAL
            # state from the live clipboard rather than a bare None-hash
            # placeholder: a wrong None here would make both sides resolve
            # waitForPeer against each other's (correctly announced) state
            # and silently lose the clip, reintroducing v1's bug through the
            # fallback path instead of the main one.
            mine = resolve_current_clip_state(self.clipboard, None, time.time())
        # PROVENANCE FIRST, and if it fires the freshness formula is not
        # consulted at all. This is the call site v3.2's first plan draft
        # left unowned -- the rule, the wire field, the recording, the
        # persistence and the disarm were all built, and NOTHING invoked
        # them. A capability built and never connected is this project's
        # signature planning defect; it has now cost it twice, and the
        # remedy is that this branch exists rather than that it is tidy.
        #
        # WHOLE RECORDS, not the `mine[:2]` slice the freshness call one
        # line below takes, and the difference is the whole hazard: the
        # slice reads (sha256, ts), so copying it here would put this side's
        # TIMESTAMP where an origin belongs, compare a float against a hex
        # string, answer False forever and fail nothing. resolve_provenance
        # raises ValueError on a short record to make that loud instead --
        # see its docstring, and do not truncate to satisfy it.
        #
        # Every producer of `mine` above is already a quadruple:
        # load_clip_state, resolve_current_clip_state (via
        # resolve_startup_state, four elements on every branch),
        # announce_clip_state's return and _write_clip's.
        if resolve_provenance(mine, peer):
            # A suppression that leaves no trace is indistinguishable from a
            # bug, and this one fires exactly when the user expects
            # something to happen: a screenshot they copied on the Mac does
            # not come back, and nothing anywhere says why. So the line
            # names WHICH SIDE'S content descended from which, not merely
            # that something did.
            #
            # The direction is read off `mine`'s origin, and that is a
            # LABEL rather than a second copy of the rule: the verdict was
            # already reached above, and getting this if wrong could only
            # ever mislabel a line. Written as an explicit `is not None`
            # anyway, matching resolve_provenance's own spelling, so nothing
            # here rests on how None compares to a hash.
            #
            # No interpolated values in either sentence, the convention
            # `clipboard changed before the send` already follows, so this
            # side and Sources/clipwire/HandleFrame.swift's twin cannot
            # drift apart in formatting -- and in production both land in
            # the same file, since Channel.attempt pipes this agent's stderr
            # into the Mac's log with a `remote: ` prefix.
            if mine[3] is not None and mine[3] == peer[0]:
                log("what we hold descends from the peer's clipboard: standing down")
            else:
                log("the peer's clipboard descends from what we hold: standing down")
            # DO_NOTHING, and it flows through the ordinary reporting and
            # send-guard below rather than returning from here: the
            # acceptance checklist requires EVERY reconciliation outcome in
            # the log, and the harness reads this connection's decision out
            # of that one line. An early return would satisfy "sends no
            # frame" and silently drop the connection's only verdict.
            decision = DO_NOTHING
        else:
            # resolve_freshness's formula is unchanged by Task 6 and takes only
            # (sha256, ts) -- kind plays no part in the comparison (see its own
            # docstring) -- so only the first two elements of each record go in.
            # Unchanged by v3.2 as well: `peer` is now a (sha256, ts, kind,
            # origin) quadruple off the wire and `mine` is whichever shape its
            # source produced, and this slice reads the same two elements from
            # either. resolve_provenance, which does read the fourth, takes the
            # WHOLE record for exactly that reason -- see its own docstring on
            # why a [:2] here and a [:2] there would not mean the same thing.
            decision = resolve_freshness(mine[:2], peer[:2])
        # Every reconciliation outcome is reported, not only the interesting
        # ones. Acceptance item 2 requires the conflict to appear in the log,
        # and the design's accepted trade-off -- with both clipboards changed
        # while apart, the more recently born agent wins -- is only tolerable
        # because it is visible here rather than mysterious.
        #
        # The decision word is the shared vocabulary: SEND_MINE /
        # WAIT_FOR_PEER / DO_NOTHING are the same three strings Swift's
        # FreshnessDecision uses as raw values, so the two sides' lines are
        # byte-identical without a formatting bridge -- the convention the
        # frame-cap and skew lines already follow, which has caught drift
        # twice. In production both sides' lines even land in the same file:
        # Channel.attempt pipes this agent's stderr into the Mac's log,
        # prefixed with `remote: `.
        #
        # Task 14: the decision word alone is not enough. "reconciled with
        # the peer: sendMine" with the two sides holding different kinds is
        # undiagnosable after the fact -- "why did a picture overwrite my
        # text" has no answer in the line above this comment. mine[2] and
        # peer[2] are None, KIND_TEXT or KIND_IMAGE; `or "none"` is safe
        # here specifically because _KNOWN_KINDS admits no falsy string, so
        # the only value that ever reaches the fallback is a real None, not
        # an empty-but-real kind masquerading as one. Byte-identical to the
        # Swift side's own suffix, the same convention the decision word
        # itself already follows.
        log("reconciled with the peer: %s (mine=%s peer=%s)"
            % (decision, mine[2] or "none", peer[2] or "none"))
        if decision != SEND_MINE:
            # Hashes equal means we agree -- not a signal to resend. A peer
            # that is fresher means we wait. Conflating either with SEND_MINE
            # reintroduces a clobber or a ping-pong.
            return
        # Verify before sending: read the clipboard, hash what came back,
        # and require it to match mine on BOTH halves -- the kind mine
        # records and the hash mine records -- before a single byte of it
        # goes out. `mine` is an ANNOUNCEMENT, made at some earlier moment;
        # the clipboard is free to have moved on since, and this branch is
        # the one place that sends content it did not itself observe
        # changing. Without the check it sends whatever it happens to find
        # under the announced timestamp: the wrong kind, or the right kind
        # at a stale age. Either is a clobber the receiver cannot detect,
        # because everything it can see about the frame is well-formed and
        # consistent -- the Mac applies any incoming clip unconditionally.
        #
        # A mismatch sends NOTHING, and that is the whole remedy for the case
        # this rule exists for: the user copied something new between our
        # announcement and this frame. The change was a real local one, so
        # _local_change is carrying it to the peer on its own path -- for an
        # image as well as for text now -- and re-announcing it from here
        # would be a second, racier copy of a job already being done
        # correctly.
        #
        # This replaces, rather than complements, the "trust bytes we
        # remembered writing and skip the read" fast path this branch used
        # to take (Task 9's _last_seen_text, deleted with this task). The
        # two are mutually exclusive by construction: a rule that the
        # clipboard must be re-read cannot be satisfied by not re-reading
        # it.
        #
        # Accepted trade-off, stated rather than left to be discovered. The
        # hazard that fast path existed for was a read racing
        # WaylandClipboard.write()'s asynchronous, detached wl-copy spawn,
        # which returns as soon as its stdin pipe closes and long before
        # wl-copy owns the selection. Such a read disagrees with mine and is
        # refused here, where before it was pre-empted -- and in THAT case
        # nothing carries the content afterwards: the eventual GPaste Update
        # for our own write is suppressed by _write_clip's echo bookkeeping,
        # correctly, since it is not a local change. So the clip stays on
        # this side until something genuinely changes. Reaching it needs a
        # peer that announces an EMPTY clipboard (or an older state) right
        # after having sent us a clip of its own, which is narrow; and the
        # alternative -- sending bytes we cannot confirm the clipboard holds
        # -- is a silent clobber, which is worse than a silent no-op. There
        # is no text re-offer mechanism to fall back on either; Task 10 added
        # one only for images, where the clipboard genuinely rewrites what it
        # was given.
        #
        # A read of None counts as a mismatch, not as a special case. An
        # emptied clipboard genuinely no longer holds what we announced,
        # and a transient wl-paste failure reads back the same way -- so
        # "we cannot confirm it" and "it changed" are one branch, and both
        # resolve to sending nothing.
        read = self.clipboard.read()
        if read is None or (read[0], sha256_hex(read[1])) != (mine[2], mine[0]):
            # Byte-identical to Sources/clipwire/main.swift's own line in
            # the .sendMine branch, the convention the frame-cap and skew
            # lines already follow: no interpolated values, so the two
            # cannot drift apart in formatting. In production both land in
            # the same file -- Channel.attempt prefixes this agent's stderr
            # with `remote: `.
            log("clipboard changed before the send")
            return
        # `body`, not `text`: this branch sends either kind now, and the
        # verification above is what licenses trusting `kind` -- it proved
        # the live clipboard agrees with mine[2] as well as with mine[0].
        # mine[2] can be KIND_IMAGE because resolve_startup_state no longer
        # hardcodes KIND_TEXT, so an image-only clipboard resolves a real
        # (hash, ts, KIND_IMAGE) triple instead of a None hash, which is what
        # makes SEND_MINE reachable for it in the first place.
        kind, body = read
        if not body:
            # Defensive rather than reachable: resolve_current_clip_state
            # records a None hash for an empty clipboard, so `mine` could
            # only carry the empty string's digest if something else wrote
            # the store. Refused here anyway, since neither codec below has
            # anything to say about zero bytes.
            return
        # Both sends below use mine[1] (our stored ts), never now: the
        # content has not changed, only been re-announced, so its recorded
        # age must be preserved. Sending with now would perpetually refresh
        # it and let it win every future reconciliation regardless of what
        # happens next. And both use mine[0] rather than re-hashing `body`
        # for the _last_seen update -- the verification above already proved
        # the two are the same string, which is exactly what nothing had
        # established before Task 11: resolve_freshness only ever compares
        # mine[0] against peer[0], never against what the clipboard holds, so
        # trusting it here used to be unfounded and the hash was recomputed.
        # It is founded now, and a body can be megabytes, so hashing it twice
        # per send is pure cost. The kind is mine[2] by the same proof.
        #
        # The _last_seen update itself is the same bookkeeping _local_change's
        # own send path does, for the same reason: _last_seen is "what the
        # peer already holds", and after this send the peer does (soon) hold
        # `body` too. Sources/clipwire's own .clipState case has no
        # EchoGuard-equivalent update here, but harmlessly so -- the Mac's
        # PasteboardWatcher is changeCount-driven and never fires on a
        # non-change. The PC's GPaste watcher DOES fire on non-changes (a
        # history deletion emits Update too) -- the entire reason _last_seen
        # exists on this side at all -- so skipping this update would let a
        # later spurious signal see the clipboard still holding `body`,
        # wrongly conclude a genuine local change happened, and resend it:
        # wasteful at best, and a silent clobber of a real Mac-side change
        # made in the meantime at worst, since the Mac applies any incoming
        # clip frame unconditionally.
        if kind == KIND_IMAGE:
            # Compared against the bare body, unlike the text guard below,
            # and this is the one place the difference is load-bearing rather
            # than stylistic: MAX_IMAGE_BYTES bounds the IMAGE, so an image
            # at exactly the limit is legal and encodes to a payload eight
            # bytes over it -- which still fits MAX_PAYLOAD_BYTES with 4 MiB
            # to spare. Writing this guard the way the text one is written
            # would refuse a maximum-size screenshot that the protocol
            # explicitly makes room for. Same verdict clause as every other
            # site reporting this limit -- three more on this side
            # (_observe_local_change's, _consume_image_reoffer's and
            # resolve_current_clip_state's) and three on the Mac, enumerated
            # in full at Sources/clipwire/Pasteboard.swift's own image guard:
            # one clause for one limit, so seven sites cannot drift into seven
            # names for it.
            if len(body) > MAX_IMAGE_BYTES:
                log("skipping an image of %d bytes: over the image limit" % len(body))
                return
            self.send(TYPE_IMAGE_CLIP, encode_image_payload(mine[1], body))
            with self._echo_lock:
                self._last_seen = (KIND_IMAGE, mine[0])
            return
        if kind != KIND_TEXT:
            # The honest default for whatever third kind may arrive later --
            # choose_kind picks no other today, so this is unreachable rather
            # than dead. Silent: a kind this agent cannot encode is a gap in
            # this file, not an event worth a line on every reconnect.
            return
        # The size bound matches _local_change's own send-side guard: this
        # branch reads the live clipboard independently, and without it,
        # winning a reconciliation over content at or beyond the TEXT limit
        # would build a payload that exceeds MAX_TEXT_BYTES once wrapped in
        # its 8-byte timestamp prefix. This is the text-content limit, not
        # the (larger) MAX_PAYLOAD_BYTES wire cap decode_frame enforces --
        # since Task 4 the two are separate, and a send this size would
        # still fit inside the frame cap; it is refused here purely as a
        # matter of the policy text clips are held to.
        if len(body) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
            log("skipping a clip of %d bytes: over the text limit" % len(body))
            return
        self.send(TYPE_CLIP, encode_clip_payload(mine[1], body))
        with self._echo_lock:
            self._last_seen = (KIND_TEXT, mine[0])

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
        # while the channel was down. GPasteWatcher's safety-net poll does
        # keep a baseline of its own, but it is not a substitute for this
        # one: the pump can fire long before that poll's first tick, so the
        # seed here is still what stands between a spurious connect-time
        # signal and a clobber. Read outside the lock, same as _local_change:
        # clipboard.read() is up to TWO wl-paste round trips, and on an image
        # clipboard that is SUBPROCESS_TIMEOUT (3s, --list-types) plus
        # IMAGE_SUBPROCESS_TIMEOUT (10s, the body) -- 13 seconds, not the 3
        # this line claimed while read() was still text-only.
        #
        # Trade-off, accepted deliberately: re-copying on the PC to force a
        # push no longer works as the FIRST action after a connect. That is
        # correct, not a regression: it makes the two sides symmetric,
        # since the Mac does not resend its own clipboard on reconnect
        # either. The previous asymmetry ran in the destructive direction,
        # which is worse than losing a convenience. Do not "fix" this back.
        read = self.clipboard.read()
        # _last_seen is a (kind, hash) pair (Task 9), and the seed covers
        # BOTH kinds. It was text-only until this side learned to send a
        # locally-observed image, at which point the gap became the same
        # clobber the seed exists to prevent: with no image baseline, the
        # first spurious signal after connect reads an image the PC already
        # held as a fresh local clip and pushes it at the Mac, which applies
        # any incoming clip unconditionally -- destroying a copy made there
        # while the channel was down. The kind comes straight from read()'s
        # own pair rather than being assumed, the same rule
        # resolve_current_clip_state follows one call away.
        #
        # An empty body still seeds nothing (`read[1]` falsy): the clipboard
        # is offering a type with no content behind it, which is a transient
        # read failure rather than a baseline worth remembering.
        seed = (read[0], sha256_hex(read[1])) if read is not None and read[1] else None
        with self._echo_lock:
            self._last_seen = seed
            # A pending re-offer expectation does not survive the session it
            # was armed in. This method runs on a fresh connection, and again
            # after a mid-connection Wayland flap (a logout/login with the
            # SSH channel still up) -- and a session that went away cannot
            # re-offer a write made into it. Left armed, it would be spent on
            # the first image the user copies afterwards: absorbed as our own
            # re-offer, stored under the PEER's timestamp -- making a clip
            # born here look as old as the applied one, with the hex
            # tie-break deciding whether the Mac's stale copy clobbers it --
            # and never sent.
            #
            # Cleared HERE, with the seed and before the pending clip below
            # is applied, so a queued IMAGE re-arms it on its way through
            # _write_clip. Clearing it after that apply would wipe the
            # expectation the apply had just armed, and GPaste's re-encode of
            # the peer's own image would go straight back to the peer.
            self._expect_reoffer = None
        applied_pending = None
        if self.pending_clip is not None:
            # Supersedes the seed above with the more authoritative value:
            # once a queued clip from the Mac has actually been applied,
            # both sides genuinely hold ITS content, not whatever the PC's
            # clipboard held a moment earlier.
            applied_pending = self._write_clip(self.pending_clip, self.pending_clip_kind)
            self.pending_clip = None
            if applied_pending is not None:
                # And it supersedes any STASHED announcement from that same
                # peer too, which is why this cannot wait for the drain
                # below. A clip frame is strictly newer information than an
                # earlier clip-state from the same peer: the announcement
                # describes what the peer held BEFORE it sent the clip. Left
                # in place, the drain resolves that superseded announcement
                # (old ts) against the clip we just applied (the peer's own,
                # newer ts), reads SEND_MINE, and sends the peer its own clip
                # straight back -- deterministically, in the reboot flow.
                #
                # The harm is bounded (_write_clip armed the echo
                # suppression before writing, so the peer's own guard
                # discards the bounce and the content converges), so this
                # costs one redundant frame and one redundant clipboard
                # write rather than a loop. Cleared here rather than inside
                # the `not self._clip_state_sent` block below so a
                # mid-connection Wayland flap -- which re-enters this method
                # with the announcement already sent, skipping that block
                # entirely -- cannot leave a superseded stash behind either.
                self._pending_peer_clip_state = None
        if not self._clip_state_sent:
            # Sent exactly once per connection (== once per process here --
            # see __init__), after the store has been consulted, and after
            # any queued pending_clip above has already been applied.
            self._clip_state_sent = True
            if applied_pending is not None:
                # _write_clip just wrote to and persisted state for a
                # pending clip -- we know EXACTLY what we now hold and how
                # old it is, straight from that call's own return value.
                # Deliberately NOT calling announce_clip_state (which would
                # re-derive this via a fresh clipboard read) here: wl-copy is
                # spawned detached and _write_clip's own write() returns as
                # soon as its stdin pipe is closed, long before wl-copy
                # necessarily registers as the Wayland selection owner. A
                # read issued immediately afterward can see stale
                # (pre-write) content, or none at all -- and
                # announce_clip_state would then PERSIST that wrong state
                # OVER the correct entry _write_clip just saved, silently
                # clobbering it and announcing the wrong age to the peer.
                # Using the known-correct value directly sidesteps that
                # race entirely; there is nothing left for a fresh read to
                # tell us that _write_clip does not already know.
                self.send(TYPE_CLIP_STATE, encode_clip_state(*applied_pending))
                announced = applied_pending
            else:
                announced = announce_clip_state(
                    self.send, self.clipboard, path=self._clip_state_path)
            if self._pending_peer_clip_state is not None:
                # Fix round 1, Finding 1: a peer clip-state that arrived
                # (via _on_clip_state) before we ever reached this point
                # this connection was stashed rather than resolved, because
                # load_clip_state() could still have been stale then --
                # unreconciled against what the clipboard actually holds
                # right now. The two announce branches just above (either
                # one) have now brought the store up to date, so it is safe
                # to resolve it here, immediately -- exactly mirroring how
                # pending_clip is applied above before anything else in
                # this method depends on the store being current.
                peer = self._pending_peer_clip_state
                self._pending_peer_clip_state = None
                # `announced` -- the pair we just put on the wire -- rather
                # than a re-read of the store. Whichever branch above ran,
                # it is the authoritative value, and it is what this peer
                # was told we hold.
                self._resolve_clip_state(peer, mine=announced)
        if self._watcher is None:
            # The degraded verdict is handed back IN on a rebuild and reported
            # back OUT when it is first reached, so it belongs to the
            # connection rather than to whichever watcher happened to reach it.
            self._watcher = make_watcher(
                self.clipboard,
                degraded=self._event_source_degraded,
                on_degrade=self._note_event_source_degraded,
                # Handed to BOTH watcher shapes by make_watcher, and the
                # standalone poller is the one that matters: it is what a
                # machine with no GPaste gets, which is precisely the
                # machine whose re-offer never comes. See _reoffer_pending.
                on_idle_tick=self._reoffer_pending,
            )
            self._watcher.start(self._local_change)

    @staticmethod
    def _reoffer_is_overdue(expectation):
        """Has an armed expectation waited longer than the detection budget?
        THE one place that rule is written, and every reader of it is a
        reader of THIS: _reoffer_pending, which asks whether an observation
        is worth signalling at all; _consume_image_reoffer's disarm, which
        spends the answer when the clipboard reads back as our own bytes;
        and _give_up_on_reoffer, which spends it when the clipboard reads
        back as nothing usable. Two spellings of one threshold drifting
        apart is this project's recorded defect shape, and this one would
        drift in the direction where the poll stops looking before the
        disarm can fire. Add a reader, not a second threshold: a caller
        needing an extra condition gates on it ALONGSIDE this call, the way
        the two spenders gate on `gen`.

        A static method taking the record rather than reading the field,
        because its callers hold _echo_lock differently: _reoffer_pending
        takes it here, the two spenders are already inside it, and
        _echo_lock is not reentrant.

        SAFETY_NET_POLL_SECONDS is the CONSTANT, never the interval the poll
        is running at -- see the disarm's own comment for the mode where the
        difference decides the outcome.
        """
        return (expectation is not None
                and time.time() - expectation[2] >= SAFETY_NET_POLL_SECONDS)

    def _reoffer_pending(self):
        """Is there an image re-offer whose absence is now worth looking at?
        Asked by the poll on every tick that saw NO change, and answering
        True makes it signal an observation anyway.

        This is what keeps the disarm honest on the machine it exists for.
        The poll signals on a change to its probe() TOKEN, and on an
        installation with no GPaste there is no second change to see: our
        own wl-copy write moves the token once, and from then on the
        clipboard sits perfectly still. Without this the expectation is
        never looked at again, _consume_image_reoffer never runs, and the
        disarm is a branch nothing reaches -- inert, in exactly the
        installation Task 5 exists for, which is how the previous two fixes
        for this bug shipped.

        So the poll's no-change branch asks, and the WORKER reads. This
        thread neither reads the clipboard nor decides anything: one place
        still decides what a local change means, and only one thread is ever
        inside it. A second reader here would be a parallel observation path
        with its own copy of the echo rules, which this file has already
        fixed two races in.

        OVERDUE, not merely armed, and the difference is a cost the file
        already ruled on. An observation means a full clipboard.read() --
        --list-types plus the whole image body -- and "armed" stays true from
        the write until the disarm, which in degraded mode is thirty ticks a
        second apart: up to MAX_IMAGE_BYTES down a pipe thirty times per
        applied image, which is precisely the expense probe() was introduced
        to delete. Nothing is lost by waiting: a real re-offer changes the
        offered type list, so it arrives through the CHANGE branch and never
        needed this one, and the disarm needs exactly one look -- the first
        tick past the budget. The only case that shifts is a re-offer whose
        type list happens to match, absorbed at the budget rather than a few
        seconds in; still absorbed, still decided by content.

        "EXACTLY ONE LOOK" IS A PROPERTY OF THE OBSERVER, NOT OF THIS
        PREDICATE, and it was measured false once already. Nothing here
        stops the asking; the expectation going away is the only thing that
        does, and a look that reaches no decision used to leave it armed --
        so every tick past the budget provoked a fresh full read, forever.
        What makes the sentence above true is that EVERY path out of
        _observe_local_change now spends an overdue expectation: the disarm
        and the consume in _consume_image_reoffer, the text path's outright
        clear, and _give_up_on_reoffer on every early return that reaches no
        decision at all. Whoever adds another owes this predicate the same
        call.

        Runs on the poll thread and takes _echo_lock, which is held across
        nothing that blocks; it acquires no other lock, so the file's
        _observe_lock -> _echo_lock -> _write_lock order is untouched.
        """
        with self._echo_lock:
            return self._reoffer_is_overdue(self._expect_reoffer)

    def _note_event_source_degraded(self):
        """Called once by the watcher when it diagnoses a dead event source, so
        the verdict outlives the watcher that reached it. Runs on the safety
        net's poll thread; see _event_source_degraded on why that needs no
        lock."""
        self._event_source_degraded = True

    def clipboard_lost(self):
        self.phase = PHASE_PENDING
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _write_clip(self, payload, kind=KIND_TEXT):
        """Single place where we touch the local clipboard, so echo
        bookkeeping cannot be forgotten on one of the paths.

        `payload` is the wire-format [ts][body] encoding for `kind` --
        encode_clip_payload's for KIND_TEXT, encode_image_payload's for
        KIND_IMAGE -- not a bare body, since every v2+ clip frame carries its
        own timestamp; both call sites (_on_clip's immediate-apply path and
        clipboard_became_ready's pending_clip-apply path) pass the raw frame
        payload through unchanged, so it is decoded here, once.

        `kind` defaults to KIND_TEXT so on_frame's TYPE_CLIP path can leave
        it unsaid; TYPE_IMAGE_CLIP travels through the same two call sites
        with KIND_IMAGE. It is a parameter rather than something derived
        from `payload` because the two wire shapes are identical -- the
        text codec would decode an image payload without complaint and hand
        PNG bytes to a text/plain write.

        There is deliberately no MAX_IMAGE_BYTES (or MAX_TEXT_BYTES) guard
        on this apply path. The file's three bounds split the job the way
        the header comment states: the frame cap is what the DECODER
        enforces, and the two content limits are what the SENDERS enforce.
        A body that got here already fit inside MAX_PAYLOAD_BYTES, so memory
        is bounded, and refusing it would be a silent loss of content a peer
        went to the trouble of sending -- the same reading
        Sources/clipwire/main.swift's own .clip case takes, which applies
        whatever decoded without consulting MAX_TEXT_BYTES either. What is
        left is an asymmetry (this side can apply an image larger than it
        could ever send back), not an unbounded read.

        A payload that fails to decode, or decodes with an empty body,
        touches neither the suppression nor the clipboard -- swallowed
        quietly, mirroring Sources/clipwire/main.swift's handleFrame .clip
        case (`try? ... !decoded.text.isEmpty`). This is a deliberate
        asymmetry with _on_clip_state, which lets a malformed clip-state
        propagate and close the connection: Swift's OWN .clip case swallows
        too, and nothing in this task asks for a clip payload's
        malformed-content behaviour to change. decode_image_payload raises
        the same ClipPayloadError decode_clip_payload does, so the image path
        joins that family without a second except clause.

        Runs on the main thread. _local_change() (below) runs on the
        watcher's background threads -- two of them since the safety-net poll
        was added -- and reads this same bookkeeping, so the two fields are
        only ever touched under _echo_lock.

        Returns the (sha256, ts, kind, origin) record that was applied and
        (best-effort) persisted, or None if the payload never decoded or
        decoded with an empty body and nothing was applied.
        clipboard_became_ready uses this to announce a just-applied pending
        clip's state DIRECTLY, rather than re-deriving it through a fresh
        clipboard read -- see that method's own comment for why a read
        immediately after this call cannot be trusted to reflect it yet.

        FOUR elements as of v3.2, with a literal None origin, and that is
        load-bearing rather than cosmetic. This return value travels to
        _resolve_clip_state as `mine` (via clipboard_became_ready's
        `announced`), where resolve_provenance takes whole records and
        raises ValueError on a short one. The None is also the honest
        answer: content applied FROM the peer is byte-identical to what the
        peer holds, so the two agree by hash and have no ancestry to
        declare. An origin is born one door over, in
        _consume_image_reoffer, when the clipboard hands back something
        else.
        """
        decode = decode_image_payload if kind == KIND_IMAGE else decode_clip_payload
        try:
            ts, body = decode(payload)
        except ClipPayloadError:
            return None
        if not body:
            return None
        # Hashed once, here, and reused by every site below. An image body can
        # be up to MAX_IMAGE_BYTES, so the two SHA-256 passes this method used
        # to make of a (wire-capped) text body are no longer free enough to be
        # worth leaving as they were.
        sha256 = sha256_hex(body)
        with self._echo_lock:
            # The one-shot echo suppression is compared against a TEXT read
            # (_observe_local_change's `body == expected`), so it holds bytes
            # only for a text write and is cleared for an image one, for two
            # reasons: nothing would ever compare it against an image, and a
            # 4 MiB body held here until the next observation is exactly the
            # memory _last_seen exists as a hash to avoid. It is the only
            # piece of echo bookkeeping that holds content rather than a
            # hash. An image write's own echo is suppressed by _last_seen
            # instead, which carries the kind alongside the hash (Task 9).
            self._last_written = body if kind == KIND_TEXT else None
            self._write_gen += 1
            self._last_seen = (kind, sha256)
            # An image write is the one case where what we hand the clipboard
            # and what it later offers back are not the same bytes: GPaste
            # takes over the selection a few seconds later and re-encodes it.
            # Arm the expectation here, spend it in _consume_image_reoffer.
            # A text write clears it rather than leaving it: text reads back
            # unchanged, and a stale expectation would swallow a later image.
            #
            # A TRIPLE since v3.2, and all three elements come from this one
            # write:
            #
            #   * `ts` -- the peer's own timestamp, which is what the store
            #     entry the consumer authorises has to be stamped with.
            #   * `sha256` -- THE PEER'S CANONICAL HASH, and the whole of
            #     provenance's premise. It is not plumbed in from anywhere:
            #     the bytes handed to the clipboard one line above are the
            #     bytes the peer hashed and announced, so their digest --
            #     already computed here, already assigned to _last_seen -- IS
            #     the peer's hash. Recorded now because the process that
            #     knows it dies with this connection.
            #   * the wall-clock moment of the write, which is what lets
            #     _consume_image_reoffer tell "GPaste has not acted YET" from
            #     "there is no GPaste on this machine at all". Not a deadline
            #     and not compared against any interval in force -- see that
            #     method's disarm branch.
            self._expect_reoffer = (
                (ts, sha256, time.time()) if kind == KIND_IMAGE else None
            )
        self.clipboard.write(kind, body)
        # WaylandClipboard.write() spawns wl-copy DETACHED (Popen(...,
        # start_new_session=True)) and returns as soon as its own stdin pipe
        # is closed -- a hand-off, not a confirmation that wl-copy has
        # actually registered as the Wayland selection owner yet. The write
        # above and the save_clip_state below are therefore not "the clipboard
        # now reads this" -- they are "this is what we just told the clipboard
        # to hold, and it is authoritative regardless of when (or whether)
        # wl-copy finishes taking ownership." A caller that instead re-read
        # the clipboard to find out what was just written would race that
        # handoff.
        #
        # That is also the one place this method's hash legitimately describes
        # bytes handed to the write tool rather than bytes read back, and it
        # is not an exception to this task's rule so much as the only thing
        # knowable at this instant: the clipboard genuinely holds `body` right
        # now (wl-copy owns the selection), any read here would race the
        # handoff, and the re-offer has not happened yet. _consume_image_reoffer
        # corrects the store the moment it does. Saving here is still required
        # rather than merely early -- on an installation with no GPaste the
        # re-offer never comes at all, and a store that recorded nothing would
        # resolve "clipboard changed while apart" on the next connect for an
        # image it had applied correctly.
        #
        # The peer's timestamp, never now: this is the entire reason it
        # travels in the frame. Stamping it with now would make applied
        # content look freshly copied here and win the next reconciliation
        # against the machine it actually came from. A local disk failure
        # here is not the peer's fault and must not undo the write above or
        # propagate as a FrameError and tear down the channel.
        #
        # No origin: what this side now holds IS the peer's bytes, so there
        # is nothing to declare an ancestor for. Written as the default
        # rather than passed, alongside every other store write in this file
        # except the one in _consume_image_reoffer.
        try:
            save_clip_state(sha256, ts, kind, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        return sha256, ts, kind, None

    def _local_change(self):
        """The single funnel for an observed local change, and the one entry
        point BOTH watcher threads use.

        Serialized against itself. Until the safety-net poll was added exactly
        one thread ever entered here -- the gdbus pump or the fallback poll,
        never both -- and this method structurally cannot dedupe against a
        sibling: it snapshots _last_seen before the clipboard read and only
        advances it after the send, so two observations of ONE copy that
        overlap inside that window both find `stale` false, both find the text
        different from a now-stale `last_seen`, and both send a TYPE_CLIP frame
        with its own observed_at, behind two competing clip-state writes.

        The snapshot block below is what closes it: taken while this lock is
        held, it reads a _last_seen the winner has already advanced, so the
        sibling recognises the content as already synced and returns at the
        `(KIND_TEXT, sha256) == last_seen` check. There is deliberately no
        separate re-read -- the existing snapshot IS the read-after-acquire.

        The image branch is closed by this same lock and by the same
        _last_seen rule, but reaches it a different way: it does not lean on
        the snapshot at all. _consume_image_reoffer compares against the LIVE
        _last_seen and _expect_reoffer under _echo_lock, and the winner
        advances _last_seen before releasing _observe_lock -- so a sibling
        observing that one copy finds either the expectation already spent or
        the hash already recorded, whichever way it lost the race, and both
        answers stop it before the send.

        It BLOCKS rather than skipping, which matters: PollingWatcher's
        `previous` has already advanced past the change it is reporting, so a
        skipped observation is a clip LOST until the next change, not one
        merely deferred.
        """
        with self._observe_lock:
            self._observe_local_change()

    def _observe_local_change(self):
        # Snapshot what we expect and the generation it belongs to BEFORE
        # reading the clipboard. clipboard.read() is up to TWO wl-paste round
        # trips -- on an image clipboard, SUBPROCESS_TIMEOUT (3s,
        # --list-types) plus IMAGE_SUBPROCESS_TIMEOUT (10s, the body), so 13
        # seconds rather than the 3 this line claimed while read() was still
        # text-only -- and _write_clip() can land on the main thread at any
        # point during that window. Holding _echo_lock across the read would
        # block _write_clip() for the whole round trip, so it is released
        # before the read and re-acquired only to compare afterward. The
        # longer that window got, the more this mattered, not less.
        #
        # This method is NOT vulnerable to the wl-copy async-write race that
        # clipboard_became_ready's announce step and _on_clip_state's
        # SEND_MINE branch were fixed for (see both), even though it also
        # reads the clipboard shortly after a _write_clip call can occur:
        # this method is only ever invoked BY a watcher observing a signal,
        # and both watchers' signals causally follow the underlying
        # Wayland-level change they are reporting. GPasteWatcher's pump only
        # calls this in reaction to GPaste's own DBus Update signal, which
        # GPaste cannot emit before it has itself observed the new content
        # -- so by the time this method's own read runs, the change has
        # already stabilized. PollingWatcher's coarse interval (on the
        # order of a second, far longer than a subprocess spawn) means a
        # tick that races _write_clip's wl-copy and probes a stale token
        # simply finds nothing changed (not a false "revert") and fires on
        # a LATER tick once wl-copy has settled -- self-healing rather than
        # sending anything wrong. That argument is about the LOOP's timing,
        # not about what it reads, so moving it from the body to a token
        # left it intact. Neither path needs the _last_seen-hash
        # preference the other two call sites use.
        with self._echo_lock:
            expected = self._last_written
            gen = self._write_gen
            last_seen = self._last_seen

        read = self.clipboard.read()
        # The moment of OBSERVATION -- as close to the read as possible --
        # not whenever the rest of this method happens to run afterward.
        # Persisted and sent below, once we know this is a genuine change.
        observed_at = time.time()
        if read is None:
            # The clipboard did not answer, so there is nothing to judge --
            # but an OVERDUE expectation must not survive a look it was
            # asked for, or the poll asks again on the very next tick and on
            # every tick after it. See _give_up_on_reoffer.
            self._give_up_on_reoffer(gen)
            return
        if read[0] == KIND_IMAGE:
            # Indexed off `read` rather than unpacked so the text path below
            # keeps `text` as its own name for the body.
            png = read[1]
            if not png:
                # A type offered with no bytes behind it: a wl-paste that
                # failed rather than a clip. Nothing to consume, nothing to
                # send -- and decode_image_payload would refuse an empty body
                # on the far side anyway. Same reason as the None read above:
                # a look reaching no decision must still spend an overdue
                # expectation, or the poll re-provokes this read forever.
                self._give_up_on_reoffer(gen)
                return
            # Hashed once, here, and handed to both paths below. An image body
            # can be up to MAX_IMAGE_BYTES and this method runs on every
            # observed clipboard change, so a second SHA-256 pass over it is
            # the cost _write_clip's own single-hash comment already refuses.
            sha256 = sha256_hex(png)
            # ASK THE RE-OFFER CONSUMER FIRST, AND SEND ONLY IF IT DECLINES.
            # Most image observations on this machine are not local changes
            # at all: GPaste takes over the selection a few seconds after
            # this agent writes an image and re-encodes it (105,700 bytes in,
            # 180,287 out, measured), raising an Update for content the peer
            # already holds in its original form. A send placed ABOVE this
            # call bounces every single Mac->PC image straight back and undoes
            # that whole guard -- see _consume_image_reoffer, and
            # test_lifecycle.py's TestImageReofferIsOurOwnWrite, which goes
            # red if these two are swapped.
            if self._consume_image_reoffer(png, sha256, gen):
                return
            # Everything below mirrors the text path further down, in the same
            # order and for the same reasons; only the codec, the frame type
            # and the limit differ.
            if len(png) > MAX_IMAGE_BYTES:
                # The verdict clause is byte-identical to the re-offer path's
                # own oversize line and to the announce path's
                # (resolve_current_clip_state's), which is the whole reason
                # they exist as separate sentences rather than one: they
                # report different events (an image read back after our write,
                # one the user copied, one there is simply nothing to announce
                # for) about the same limit, and the shared clause is what
                # keeps them from drifting into different names
                # for it. The sentence shape mirrors the text-skip line the
                # two text call sites already share.
                #
                # Compared against the bare body, not body + TIMESTAMP_BYTES:
                # MAX_IMAGE_BYTES bounds the IMAGE, which is the whole point
                # of splitting it from the frame cap -- an image at exactly
                # the limit encodes to a payload 8 bytes over it and still
                # fits MAX_PAYLOAD_BYTES with room to spare.
                #
                # Skipped BEFORE the store is touched, unlike the re-offer
                # path, which records an oversized read-back anyway. That is
                # not an inconsistency: the re-offer describes content this
                # side has ALREADY applied from the peer, so the store must
                # follow the clipboard or every reconnect resolves "clipboard
                # changed while apart". This one was never sent and never
                # applied from anywhere; recording it as synced would tell
                # the next reconciliation that the peer holds an image it has
                # never seen.
                log("skipping an image of %d bytes: over the image limit" % len(png))
                return
            # Persisted before the send and unconditional on its outcome, for
            # the same reason the text path does it: what we hold and how old
            # it is changed the instant it was observed, whether or not the
            # peer ever receives it.
            try:
                save_clip_state(sha256, observed_at, KIND_IMAGE, path=self._clip_state_path)
            except (OSError, ClipStateError) as error:
                log("could not persist clip state: %r" % error)
            self.send(TYPE_IMAGE_CLIP, encode_image_payload(observed_at, png))
            # Only after a successful send, exactly as on the text path: if
            # send() raises on a dead channel, _last_seen must not advance to
            # content the peer never received. _last_written is deliberately
            # NOT touched -- it is the one-shot echo value for an unobserved
            # TEXT write, and this observation is neither.
            with self._echo_lock:
                self._last_seen = (KIND_IMAGE, sha256)
            return
        kind, text = read
        if kind != KIND_TEXT or not text:
            # Task 7 made read() kind-aware -- before it, a non-text
            # clipboard could only ever read back as None, so this method
            # was already, structurally, text-or-nothing. Keeping every
            # other kind that way here is a scope boundary, not an
            # oversight: KIND_IMAGE is the only other kind read() can
            # currently return (choose_kind picks nothing else), so this
            # guard is reached today only by an empty body, and remains as
            # the honest default for whatever third kind may arrive later.
            # This is now the ONLY read() call site that is kind-restricted
            # at all, and only because its image case was handled and
            # returned above. The two others Task 7 touched both act on
            # either kind: clipboard_became_ready's connect-time seed, and
            # _resolve_clip_state's SEND_MINE branch, which builds a
            # TYPE_IMAGE_CLIP frame for a verified image rather than
            # declining to send it.
            #
            # Another early return that must spend an overdue expectation,
            # and the one it is easiest to miss because it is not obviously
            # a failed read: this branch never reaches the clearing block
            # below, so without this call an empty text body -- or whatever
            # a later third kind reads back as -- leaves the poll asking on
            # every tick. See _give_up_on_reoffer.
            self._give_up_on_reoffer(gen)
            return

        # Consume the suppression on the FIRST observed change, whatever it is —
        # not only on a match. A TEXT write produces exactly one change event; if
        # we observe a different one instead, ours is already gone, and a
        # lingering hash would silently swallow the user's later deliberate copy
        # of the same text. Mirrors EchoGuard.shouldSend on the Swift side, where
        # the match-only variant was found to be a real defect.
        #
        # "A text write", not "our write": an IMAGE write arms no one-shot value
        # at all (_write_clip sets _last_written only for KIND_TEXT -- see its own
        # comment) precisely because it does NOT produce exactly one change event.
        # It produces two: our wl-copy handoff raises an Update, and GPaste's
        # takeover raises a second one seconds later with re-encoded bytes. A
        # one-shot consumed by the first of those would be spent before the one
        # that matters, so an image write arms _expect_reoffer instead, and
        # _consume_image_reoffer decides which of the two it is looking at by
        # comparing against _last_seen. This block is reached only by a text
        # observation anyway -- the kind guard above sends every image to that
        # method instead.
        #
        # The pending image re-offer goes with it, and for a stronger reason
        # than symmetry: a text copy means the user moved on, so the re-offer
        # will never come, and an expectation left armed would silently
        # swallow the next image they copy -- days later, with nothing in the
        # log. Cleared HERE, before the `text == expected` and `last_seen`
        # early returns below, because those return on observations that are
        # not changes at all and would otherwise leave it armed forever.
        with self._echo_lock:
            stale = self._write_gen != gen
            if not stale:
                self._last_written = None
                self._expect_reoffer = None

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
        # very next observed TEXT change, whatever that change is (the block
        # above -- an image observation never reaches it), and is otherwise
        # None, including for the whole life of an image write, which arms
        # _expect_reoffer instead. Signals here are deliberately
        # unfiltered (a real GPaste Update can be a history deletion, not a
        # clipboard change at all; a polling tick can follow a transient
        # read() timeout that returned None instead of the real content) --
        # so once the one-shot value is spent, ANY fired signal whose
        # content merely differs from it looks like a fresh local change.
        # _last_seen has no such expiry: it is what the peer already holds,
        # for as long as neither side has genuinely changed it, and catches
        # exactly the non-change signals the one-shot value cannot.
        #
        # Compared as (kind, hash), not text by value -- one rule for both
        # kinds, per Task 9. This call only ever reaches here with
        # KIND_TEXT (the guard above already returned for any other kind),
        # so the kind half of THIS side is always KIND_TEXT. last_seen's own
        # kind is not fixed at all: an applied image, a re-offer, a locally
        # sent image, the connect-time seed and _resolve_clip_state's own
        # SEND_MINE branch each put a KIND_IMAGE pair there -- five sites,
        # enumerated in full at _last_seen's own comment in __init__. Comparing kind alongside hash, rather than hash alone, is
        # what keeps a hash coincidence between two DIFFERENT kinds from
        # silently reading as no-change.
        # Hashed once, here, and reused below for both the persisted state
        # and the updated value -- text can be up to MAX_TEXT_BYTES, and
        # this method runs on every observed clipboard change.
        sha256 = sha256_hex(text)
        if (KIND_TEXT, sha256) == last_seen:
            return
        # This text is wrapped in encode_clip_payload before it reaches the
        # wire, so the cap must account for the 8-byte timestamp prefix --
        # text at exactly MAX_TEXT_BYTES would otherwise encode to a payload
        # 8 bytes over the text-content limit. MAX_TEXT_BYTES, not the
        # (larger) MAX_PAYLOAD_BYTES the decoder enforces: since Task 4 the
        # two are separate constants, and content between the two would
        # still fit inside a frame -- this guard is the text-specific
        # policy limit, not a wire-safety necessity.
        if len(text) + TIMESTAMP_BYTES > MAX_TEXT_BYTES:
            log("skipping a clip of %d bytes: over the text limit" % len(text))
            return
        # Persisted before the send, unconditional on the send's outcome:
        # what we hold and how old it is changed the instant it was
        # observed, regardless of whether the peer ever receives it. A local
        # disk failure here is not the peer's fault and must not prevent the
        # send below.
        #
        # KIND_TEXT unconditionally: `text` above came from self.clipboard.read()
        # (WaylandClipboard, a text/plain read) and is about to go out as a
        # TYPE_CLIP frame -- the text-clip type -- two lines down.
        try:
            save_clip_state(sha256, observed_at, KIND_TEXT, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        self.send(TYPE_CLIP, encode_clip_payload(observed_at, text))
        # Only after a successful send: if send() ever raises (e.g. a dead
        # channel), _last_seen must not advance to content the peer never
        # actually received.
        with self._echo_lock:
            self._last_seen = (KIND_TEXT, sha256)

    def _give_up_on_reoffer(self, gen):
        """Spends an OVERDUE expectation on a look that could not be judged,
        so the poll stops asking for another one.

        THE INVARIANT THIS EXISTS FOR: no path out of _observe_local_change
        may leave an overdue expectation armed. Nothing stops
        _reoffer_pending answering True except the expectation going away,
        so a look that reaches no decision does not merely cost one
        observation -- the next tick asks again, and every tick after it,
        each one a full clipboard.read() (--list-types plus the whole image
        body) provoked to learn nothing. On a GPaste-less machine the poll
        runs at DEGRADED_POLL_SECONDS, so that is a wl-paste pair PER SECOND
        for the life of the connection: bounded (one process per SSH
        connection) and lossless, but it is the exact expense probe() was
        introduced to delete, arriving through the one branch that bypasses
        it. The early returns above reach here by name -- a read that
        failed, an image type offered with no bytes, and the kind guard on
        the text path -- and one added later without this call is the same
        defect again.

        GIVING UP IS A DISARM, not a deferral, and that is the file's own
        ranking of the harms rather than a preference. An expectation left
        armed absorbs the user's next image copy as the re-offer that never
        came, and under v3.2 a clip so labelled is not sent late, it is
        never sent at all (see save_clip_state's origin note). A look
        deferred on a clipboard that is not answering is a look that may
        never come. So the expectation is spent here, and
        _reoffer_pending's "exactly one look" is true because of this
        method rather than in spite of it.

        WHAT IT COSTS when it is wrong is exactly what _consume_image_reoffer's
        disarm branch already prices, and by the same mechanism: _last_seen
        is deliberately NOT touched here -- this look learned nothing about
        what the clipboard holds -- so a re-offer that does arrive
        afterwards finds no expectation, falls through to the local-change
        send, and goes to the Mac as a genuine local clip with no origin
        recorded. That is the original bug for that image. Recoverable by
        copying again, and the alternative is the permanent loss above.

        OVERDUE, not merely "the read failed", which is the whole reason
        this is gated at all: GPaste's takeover was measured between one
        and four seconds, and a transient wl-paste failure inside that
        window is no evidence about the machine. Gated on `gen` for this
        file's recorded defect shape -- the read is in flight for two
        wl-paste round trips, and a _write_clip landing on run()'s thread
        inside that window arms a brand-new expectation this observation
        never looked at.

        Logged outside the lock, and with its own sentence rather than the
        disarm's: "the clipboard would not say what it holds" and "the
        clipboard still holds our own bytes" are different observations
        about the machine, and a reader chasing a missing screenshot needs
        to know which one fired.
        """
        with self._echo_lock:
            if self._write_gen != gen:
                return
            if not self._reoffer_is_overdue(self._expect_reoffer):
                return
            self._expect_reoffer = None
        log("the clipboard did not read back as an image within %gs of writing one: "
            "giving up on the re-offer" % SAFETY_NET_POLL_SECONDS)

    def _consume_image_reoffer(self, png, sha256, gen):
        """Recognises the one image observation that is this agent's own
        write coming back changed, and records what the clipboard actually
        offers instead of what was handed to the write tool.

        Measured on the live PC, and the reason this method exists: a
        105,700-byte PNG written here read back identical at t+1s, then as a
        DIFFERENT 180,287-byte PNG at t+4s and t+7s. GPaste takes over the
        selection a few seconds after the write and re-encodes the image;
        the result is stable afterwards, but it is not what was written, and
        it was 70% larger. The same probe on text returns the written bytes
        unchanged, which is why no equivalent exists for KIND_TEXT.

        Called only for a non-empty image observation. `sha256` is that
        observation's digest, computed by the caller and passed in rather
        than recomputed here: the caller needs it too on the path this
        method declines, and an image body can be up to MAX_IMAGE_BYTES.

        RETURNS TRUE WHEN THE OBSERVATION IS SPOKEN FOR -- when the caller
        must NOT treat it as a new local clip -- and False only when it is a
        genuine local image the caller has to carry to the peer. That is
        deliberately not "did I consume the expectation": of the three ways
        this returns True, only the first consumes anything.

        * The re-offer itself: a write of ours was still waiting for it
          (_expect_reoffer), the read is not stale, and the bytes DIFFER from
          _last_seen. Consumed here, and nothing is sent -- this content is
          already on the peer, in its original encoding. What changes is what
          this side REMEMBERS holding, and both halves of that memory must
          come from the bytes read back: _last_seen so the re-offer is never
          mistaken for a new clip, and the store so a later reconnect finds
          the hash the clipboard will actually report and does not resolve
          "clipboard changed while apart" on every Mac wake. The stored
          timestamp is the PEER's, carried on _expect_reoffer from the write,
          plus REOFFER_TS_NUDGE_SECONDS: the re-offer is not a new clip but
          the applied one re-encoded, so stamping the moment it was observed
          would make content the Mac sent us look freshly copied here and
          beat anything the Mac copied in between -- while stamping the
          peer's value EXACTLY leaves the two sides at an identical ts
          holding different bytes, which hands the next reconnect to the hex
          tie-break. See that constant's own comment for the tie, and the
          note below for what the nudge costs.

          AND THE ORIGIN, recorded here and NOWHERE ELSE in this file. The
          store entry names the peer's own hash -- carried on
          _expect_reoffer from the write, where it was the digest of the
          bytes the peer sent -- so the next reconnect can say "what I hold
          was born from what you hold" instead of leaving two machines to
          infer it from content that no longer matches. Every attempt to
          infer it is measured dead (see the v3.2 design); this side is the
          only witness, and until now it discarded its own testimony.

        * A newer write landed while the read was in flight (`gen`) -- the
          same staleness rule the text path applies, and needed for the same
          reason: the read could predate that write. Consuming the
          expectation with pre-write bytes would record the wrong hash and
          clobber the newer write's own store entry; SENDING them would hand
          the peer back the image it had just sent us, as though the user had
          copied it here.

        * The bytes match _last_seen, so nothing changed. Our own wl-copy
          write raises an Update of its own, seconds before GPaste's takeover
          raises the second one, so the first image observation after a write
          is normally the bytes we wrote. Spending the expectation on it --
          the echo guard's own "consumed by the first change whatever it is"
          rule, correct there -- would leave the real re-offer to be read as
          a fresh local clip. This is also the plain already-synced guard the
          text path applies one branch down, and the one the connect-time
          seed relies on.

          THE DISARM LIVES ON THIS BRANCH (v3.2), and only on it: the
          clipboard is still offering our own bytes, and it has been doing
          so for at least SAFETY_NET_POLL_SECONDS, so there is nothing on
          this machine that re-encodes clipboard images and the expectation
          is dropped. See the code for why the position is the rule.

        False, then, means exactly one thing: nothing was expected. An image
        the USER copied, which the caller sends.

        THE HAZARD THAT USED TO BE STATED RATHER THAN CLOSED, and what
        v3.2 had to do about it. On an installation with no GPaste the
        re-offer never comes at all, so an expectation armed by an applied
        image stayed armed until the user's NEXT image copy, which was
        absorbed here as though it were the re-offer. That cost one image
        arriving a connection LATE: the store carried it at the peer's ts
        plus the nudge, so the PC won the next reconnect and delivered it.
        With an origin stamped on it the same clip is instead RECOGNISED AS
        THE PEER'S OWN DESCENDANT, both sides stand down, and it is never
        sent at all -- a late clip becomes a lost one. v3 weighed a timer
        against that cost and rejected it correctly; v3.2 inverted the cost,
        so the ruling had to be re-taken rather than inherited.

        It is still not a timer, because a wall-clock deadline loses to a
        phase race: the takeover happens at one to four seconds, but with
        the event source dead it is OBSERVED only by the safety-net poll, up
        to a full interval later, and a deadline and the tick carrying the
        observation then arrive neck and neck. What decides both outcomes
        here is a single READ instead -- the caller's, the one already made
        -- and the two branches are mutually exclusive by CONTENT, so no
        ordering remains to race. What is left of the hazard is the window
        before the threshold, unchanged from v3 and bounded by it.

        The flap and reconnect cases, where the expectation can be cleared
        outright rather than waited out, are still handled in
        clipboard_became_ready.

        Nothing here drives its own clock, which is the other half of why
        this is not a timer: this method only ever runs when something
        observed the clipboard. On a machine whose event source is silent,
        the observations that reach the disarm are the safety-net poll's --
        see Agent._reoffer_pending and PollingWatcher's no-change branch,
        which is what keeps a static clipboard from going unobserved while
        an expectation is armed.

        THE SECOND REMAINING COST, ALSO STATED RATHER THAN CLOSED, and it is
        what REOFFER_TS_NUDGE_SECONDS buys. After a Mac->PC image the two
        machines hold different bytes for the same picture and can never
        agree by hash: the Mac has the PNG it sent, this side has the
        re-encode, and nothing short of one of them telling the other can
        make those equal. So a reconnect cannot resolve doNothing, whatever
        is recorded here -- what it CAN do is resolve DETERMINISTICALLY. The
        nudge makes this side win, push the re-encode once, and converge;
        from then on both hold the same hash at the same ts and every later
        reconnect really does resolve doNothing.

        The price is one image frame back to the Mac on the first reconnect
        after each Mac->PC image, and the Mac's clipboard then holding
        GPaste's re-encode rather than its own original. That is worth
        naming plainly, because it is a weakened form of the very failure
        this method exists to prevent -- deferred from "immediately, on every
        screenshot" to "once, at the next reconnect", and bounded instead of
        permanent. The alternative is not zero frames: it is the same frame
        on a coin flip, and on the losing half an identical exchange on
        every reconnect for as long as that image stays on the clipboard.
        """
        # Compared against the LIVE _last_seen under the lock rather than
        # against _observe_local_change's pre-read snapshot: both values this
        # decision turns on are written by _write_clip on run()'s thread, so
        # reading them as late as possible is what makes the staleness check
        # above meaningful in the first place -- a snapshot would answer for
        # the moment before the read, which is exactly the moment `gen` exists
        # to reject.
        disarmed = False
        with self._echo_lock:
            if self._write_gen != gen:
                return True
            if (KIND_IMAGE, sha256) == self._last_seen:
                # THE DISARM, and it lives here -- inside the branch that
                # already established the clipboard still holds exactly what
                # we wrote -- rather than above the dispatch, because the
                # position IS the rule. Reached only when the bytes are
                # ours, so an expectation is never dropped on a tick that
                # has the re-offer in its hand: the two outcomes are
                # separated by CONTENT first and consult a clock only after,
                # which is what leaves no ordering to race. Hoisted above
                # this comparison instead, it would disarm on a re-offer
                # first observed after the threshold -- the phase case, and
                # the exact failure a wall-clock deadline produces.
                expectation = self._expect_reoffer
                if self._reoffer_is_overdue(expectation):
                    # SAFETY_NET_POLL_SECONDS, the CONSTANT, never whatever
                    # interval the poll happens to be running at. After the
                    # dead-source verdict that interval is
                    # DEGRADED_POLL_SECONDS -- one second, shorter than the
                    # takeover this waits for -- so a window meaning "the
                    # current interval" would disarm deterministically
                    # BEFORE the re-offer, in the mode a connection stays in
                    # for the rest of its life once the verdict ever fired.
                    #
                    # What it costs when it is wrong is re-priced honestly
                    # by v3.2 and is why the threshold is generous: a false
                    # disarm no longer costs one extra frame, it costs the
                    # original bug for that image -- the re-encode delivered
                    # live as a local change, no origin recorded, and the
                    # reconnect degraded. Still better than the alternative
                    # it replaces (an expectation armed forever absorbs the
                    # user's next image copy, and under provenance that clip
                    # is lost PERMANENTLY rather than arriving one
                    # connection late), but the margin is load-bearing now.
                    self._expect_reoffer = None
                    disarmed = True
                else:
                    return True
            else:
                expectation = self._expect_reoffer
                if expectation is None:
                    # `is None`, never a truth test: the expectation carries
                    # a ts of 0.0 for a clip copied at the epoch, and a
                    # falsy check on the record would send its re-offer back
                    # to the peer.
                    return False
                self._expect_reoffer = None
                self._last_seen = (KIND_IMAGE, sha256)
        if disarmed:
            # Logged because a disarm is a decision about the machine, not
            # about this clip: it says GPaste did not take the selection
            # within the detection budget, so nothing on this installation
            # is going to re-offer anything, and the next image the user
            # copies is theirs rather than ours. Silent, it is
            # indistinguishable from the expectation having been consumed
            # correctly -- and those two have opposite consequences for the
            # image after this one.
            log("no re-offer within %gs of writing an image: "
                "nothing on this machine is re-encoding the clipboard"
                % SAFETY_NET_POLL_SECONDS)
            return True
        ts, origin, _written_at = expectation
        if len(png) > MAX_IMAGE_BYTES:
            # The size limit belongs on the read-back body too, because
            # re-encoding INFLATES: 105 KB became 180 KB on the live machine,
            # so an image comfortably under the limit going in can exceed it
            # coming out. Applied as a log line rather than as a refusal to
            # record. Nothing is sent from here either way, and declining to
            # record would leave the store holding a hash the clipboard no
            # longer offers -- reopening the false "clipboard changed while
            # apart" on every wake, for exactly the images least able to
            # afford being resent. Logged because an image this side can
            # never forward on is otherwise a silent skip, and a silent skip
            # is how a user concludes the tool is broken. Compared against the
            # bare body, not body + TIMESTAMP_BYTES: MAX_IMAGE_BYTES is the
            # limit on the image itself, which is the whole point of Task 4
            # separating it from the frame cap.
            log("the clipboard re-offered an image of %d bytes: over the image limit"
                % len(png))
        # `origin` -- the peer's own hash, carried here from the write --
        # is the one store write in this file that records one, and this is
        # the one branch of this method that may. The other two return True
        # without consuming: an observation that changed nothing, and a read
        # that predates a newer write. Stamping either with an origin would
        # label a clip THE USER COPIED as a derivative of the peer's
        # content, and under v3.2 a clip so labelled is not merely sent late
        # -- resolve_provenance stands both sides down and it is never sent
        # at all.
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

    def probe(self):
        # The poll loop's entry point (see WaylandClipboard.probe). Nothing
        # here ever becomes ready, so no watcher is ever built and nothing
        # calls this today -- present because the clipboard contract has two
        # methods now, and a double missing one is how a future change to
        # this file fails with an AttributeError on the PC instead of here.
        return None

    def write(self, kind, data):
        pass


# ============================================================================
# 6. Clipboard state — XDG paths, the store, resolution, announcing
# ============================================================================

import subprocess

SUBPROCESS_TIMEOUT = 3
# Text reads have already been observed timing out at SUBPROCESS_TIMEOUT in
# production. An image body can be up to MAX_IMAGE_BYTES (4 MiB) flowing
# through a pipe, not a few kilobytes of text, so it gets its own, longer
# bound rather than inheriting the text one. WaylandClipboard._run_wl_paste
# logs a read's duration against whichever of these two bounds applies to
# it -- see SLOW_READ_SECONDS/SLOW_IMAGE_READ_SECONDS just below for when.
IMAGE_SUBPROCESS_TIMEOUT = 10

# Fix round 1: the coordinator caught that logging EVERY read's duration
# unconditionally -- this file's own first cut at "the next time this bound
# is wrong there is evidence, not a guess" -- floods the log at production
# scale rather than serving it. PollingWatcher's safety net (composed by
# GPasteWatcher, or standalone when GPaste is unavailable) forks wl-paste on
# EVERY tick for as long as the connection lasts, whether or not anything
# changed. At the time that was clipboard.read(), up to two invocations, so
# at DEGRADED_POLL_SECONDS=1.0: 86400 x 2 = 172,800 duration lines a day,
# every one of them crossing the SSH channel into the Mac's log -- not
# instrumentation, the log being destroyed as a diagnostic. The final fix
# wave moved that loop onto clipboard.probe(): two invocations still for
# text, so that ceiling is unchanged and this gate is still the only thing
# holding it down, but ONE for an image, and never the body. That last part
# closed a hole the gate could not: a large image body genuinely IS slower
# than SLOW_IMAGE_READ_SECONDS, so it MET the threshold on every single tick
# and logged legitimately, once a second, forever.
# Sources/clipwire/main.swift's own skewLogLine (mirrored by
# skew_log_line above) already rejected logging the wrong quantity on
# exactly this ground: measuring clip age instead of clock skew "would fire
# on nearly every handshake and teach everyone to ignore the log". A
# duration line on every poll tick fails the same test, in the one release
# whose whole purpose is making this log worth reading.
#
# So a read's duration is logged only when it clears one of these two
# thresholds -- gated, not measured differently; the file still tries every
# wl-paste call and always KNOWS its duration, it just does not always say
# so. Two thresholds, not one, because the two bounds above are not one
# number either: holding an image body to the text threshold would spam on
# every normal-sized screenshot, and holding text to the image threshold
# would hide a genuinely slow text read for 3 extra seconds. Both sit at
# roughly the same fraction of their own timeout (~1/3), which is the
# reasoning that carries over from one to the other; this project has no
# live Wayland machine to measure real read latencies against (every read
# in this suite is a mocked, near-instant subprocess.run), so this is
# proportional reasoning tied to SUBPROCESS_TIMEOUT/IMAGE_SUBPROCESS_TIMEOUT
# themselves, not an empirical measurement -- said plainly rather than
# implied.
#
# A threshold alone would still leave a reader with nothing to judge an
# outlier against, so the connection's FIRST clipboard call logs
# unconditionally, establishing at least one baseline duration in the log
# before the gate takes over. First of either method, not of read()
# specifically: _claim_first_call is shared by read() and probe() and
# consumes one flag between them, so whichever runs first claims the
# baseline and the other does not re-claim it. In production that is the
# connect-time seed's read(), which runs before any watcher is built --
# but nothing depends on that ordering, and a baseline duration from a
# probe is worth exactly as much.
SLOW_READ_SECONDS = 1.0
SLOW_IMAGE_READ_SECONDS = 3.0


def _xdg_dir(var_name, default, env=None):
    """`$<var_name>` when the caller's environment sets it, else `default`
    (already resolved to an absolute path by the caller). This is the
    env-var-with-fallback shape runtime_dir() below needs for
    XDG_RUNTIME_DIR -- shared here, rather than typed out a second time
    with different literals, so clip_state_path()'s own fallback can never
    drift from it independently.
    """
    env = os.environ if env is None else env
    return env.get(var_name) or default


def runtime_dir(env=None):
    return _xdg_dir("XDG_RUNTIME_DIR", "/run/user/%d" % os.getuid(), env)


def clip_state_path(env=None):
    """`$XDG_STATE_HOME/clipwire/clip-state.json`, falling back to
    `~/.local/state` -- the directory XDG itself specifies as the default
    for XDG_STATE_HOME when unset -- through the same _xdg_dir helper
    runtime_dir() above uses for XDG_RUNTIME_DIR.

    Deliberately independent of /run, unlike runtime_dir()'s own fallback
    just above: XDG_RUNTIME_DIR's default (/run/user/<uid>) is commonly
    tmpfs, cleared on reboot -- fine for a Wayland socket that only needs
    to outlive one login session. This file has to outlive far more: sshd
    spawns a brand-new agent process on every SSH connection, and the
    channel drops on every Mac sleep/wake cycle, so the entire reason this
    store exists is to answer "how old is what I hold" for content that
    predates the process asking, which is the ordinary case here, not an
    edge one. A store that itself lived under /run would forget
    everything on the one occasion -- a real reboot -- it would matter
    most.
    """
    base = _xdg_dir("XDG_STATE_HOME", os.path.expanduser("~/.local/state"), env)
    return os.path.join(base, "clipwire", "clip-state.json")


def load_clip_state(path=None):
    """(sha256, ts, kind, origin) last persisted by save_clip_state, or None.

    The record is decode_clip_state's own shape, because this IS
    decode_clip_state -- so it grew the fourth element with the wire in
    v3.2. A missing key reading as None is what makes an existing store
    load rather than fail: a v3.1 file has no "origin" key at all, and
    neither does one written since by a save_clip_state call site with no
    origin to record -- encode_clip_state omits the key rather than writing
    an explicit null, so those files are byte-for-byte what they were before
    this release. That is the same "absent means what it meant before" rule
    the wire follows, applied to disk by the single decoder both share.

    TWO writers can put an origin on this file, and only one of them ever
    LEARNS one. Agent._consume_image_reoffer is the only place in either
    codebase that witnesses the clipboard hand back something other than
    what it was given, so it is the only place an origin is born.
    announce_clip_state writes one too, by forwarding whatever
    resolve_startup_state's matched branch preserved -- and that second
    write is the reason the first is not inert, since the process that
    learned it is dead by the connection that has to say so. The comment at
    that call says what breaks if it is "tidied" into an explicit None.

    None covers three distinct failure reasons identically, on purpose: no
    file has ever been written, the file exists but cannot be opened as a
    regular file (permissions, or -- as this module's own tests cover
    directly -- a directory sitting where the file should be), and the
    file opens fine but does not decode as a valid clip state (a write
    torn by a mid-rename crash, or anything else malformed). Every one of
    those collapses to the same "nothing stored" branch in
    resolve_startup_state below, so no caller would ever treat them
    differently -- and raising here instead would turn every fresh install
    (no file yet) into a crash. Reuses decode_clip_state's existing
    malformed-payload checks rather than a second, file-specific copy of
    the same JSON shape.
    """
    target = clip_state_path() if path is None else path
    try:
        with open(target, "rb") as handle:
            payload = handle.read()
    except OSError:
        return None
    try:
        return decode_clip_state(payload)
    except (ClipStateError, OverflowError):
        # json.loads parses an integer literal as arbitrary-precision int,
        # unlike a float literal (1e400 already becomes inf, which
        # decode_clip_state's own isfinite check turns into a
        # ClipStateError). A 400-digit integer ts instead passes
        # decode_clip_state's isinstance(ts, (int, float)) check and only
        # fails inside math.isfinite's int-to-float conversion, raising
        # OverflowError -- a different exception type than the malformed
        # payloads above, but the same "nothing usable was stored" outcome.
        return None


# Serializes the whole encode-write-replace below. save_clip_state has five
# call sites in four functions, and they run on up to three threads:
# Agent._write_clip and announce_clip_state on run()'s thread,
# Agent._observe_local_change (once on each of its image and text paths) and
# Agent._consume_image_reoffer on the watcher threads. All five derive the SAME
# `target + ".tmp"`, so two concurrent savers truncate one another's temp file
# and whichever os.replace runs second finds it already consumed -- observed
# directly as a FileNotFoundError, and in the worse interleaving as a
# half-written file renamed into place.
#
# That failure is not cosmetic: a temp moved into place mid-write makes
# load_clip_state return None, so the NEXT connection stamps ts=now on old
# content and wins a reconciliation it should lose -- a silent clipboard
# clobber, the exact failure protocol v2 exists to prevent. All five call
# sites swallow the exception, so nothing would surface either.
#
# Module-level because these are module functions, not a store object: it is
# the direct mirror of ClipStateStore.save's NSLock on the Swift side, which
# was fixed for this in an earlier task while the Python half was missed.
# load_clip_state needs no lock of its own for the same reason it does not
# there: os.replace is an atomic rename, so once the temp file itself cannot be
# torn, any reader sees the whole old file or the whole new one. Process-wide,
# not machine-wide -- and it does not need to be, since sshd spawns exactly one
# agent per connection.
_clip_state_write_lock = threading.Lock()


def save_clip_state(sha256, ts, kind, origin=None, path=None):
    """Persists (sha256, ts, kind, origin) atomically: encode, write to a
    `.tmp` sibling, then os.replace it over the real path. os.replace is an
    atomic rename on POSIX, so load_clip_state above -- quite possibly
    running in an entirely different process, since the PC agent is a new
    process every connection -- can never observe a half-written file.
    Reuses encode_clip_state rather than a second JSON encoding, so its
    non-finite-ts guard protects this path too, and the on-disk format can
    never drift from the wire format of the same shape.

    `origin` (v3.2) is the whole reason this function grew a fourth
    parameter, and the reason is the agent's lifetime rather than the
    record's tidiness: sshd spawns a new agent per connection, so the
    process that watched the clipboard hand back different bytes than it was
    given IS ALREADY DEAD by the announce that needs to say so. An origin
    that lived only in Agent._expect_reoffer would be inert -- the third
    release running in which this bug was fixed and nothing changed on the
    machine. It defaults to None because only one call site ever LEARNS one
    (Agent._consume_image_reoffer) and only one other ever forwards it
    (announce_clip_state, spreading a record resolve_startup_state returned
    whole); the three remaining writes -- Agent._write_clip and
    Agent._observe_local_change's two paths -- are content this side holds
    in its own right, and A STALE ORIGIN IS NOT HARMLESS: the PC
    holding new user content under an origin the Mac still matches makes
    resolve_provenance fire and the PC never send it -- a clip the user
    copied, gone, on an entirely routine path. So the default is the safe
    value and the recording branch is the exception, not the other way
    round.

    Positioned BEFORE `path`, so the callers that spread a resolved record
    (announce_clip_state's `save_clip_state(*resolved, path=path)`) keep
    working by construction as that record grew its fourth element, and
    every other call site -- all of which already pass `path` by keyword --
    is untouched.

    Serialized in full under _clip_state_write_lock: the atomic rename alone
    protects readers, not the shared temp file that concurrent WRITERS both
    build. See that lock's own comment.
    """
    target = clip_state_path() if path is None else path
    with _clip_state_write_lock:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        payload = encode_clip_state(sha256, ts, kind, origin)
        tmp = target + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, target)


def resolve_startup_state(current_hash, current_kind, stored, now):
    """The judgement that makes the wake flow work. `current_hash` and
    `current_kind` are the clipboard's hash and kind *right now*, at
    startup -- both produced by the same clipboard.read() call, never
    derived independently; `stored` is whatever load_clip_state() last
    returned (a (sha256, ts, kind, origin) record since v3.2, or None);
    `now` is the caller's clock. Returns a (sha256, ts, kind, origin)
    record -- FOUR elements on every branch, without exception, because
    resolve_provenance takes whole records and raises ValueError on a short
    one. A branch that returned a triple would not fail here; it would fail
    at a reconciliation this function cannot see.

    THE ORIGIN FOLLOWS THE HASH, which is the whole of the rule and the one
    line of it that is easy to get wrong:

      * MATCHED -- the stored record is returned WHOLE, origin included.
        Rebuilding it around `current_hash` looks identical for the three
        fields anyone thinks to check and quietly drops the fourth, so the
        suppression works on the first reconnect (the origin is still in
        the recording process's memory) and dies on the SECOND, once the
        only copy left is the one on disk. That is the shape of an inert
        fix, and it is why this branch returns `stored` rather than
        re-assembling it.
      * CHANGED, or nothing stored -- `origin=None`, appended explicitly.
        The origin describes bytes that are no longer on this clipboard,
        and carrying it forward is not the harmless half of the mistake:
        the PC holding new user content under an origin the Mac still
        matches means resolve_provenance fires and the PC NEVER SENDS the
        user's clip.
      * A null hash -- `origin=None` too, for the reason decode_clip_state
        already refuses an origin beside a null sha256: an origin with no
        content of ours to describe can only ever be compared against
        nothing.

    A timestamp can come from three places, in precedence order: a local
    change this process watched happen, a clip received from the peer
    (carrying the peer's own timestamp, stored unchanged), and -- the case
    this function exists for -- content that predates this process
    entirely. sshd spawns this agent fresh on every connection, and the
    Mac's channel drops on every sleep/wake cycle, so "predates this
    process" is not an edge case here; it is the ordinary shape of a clip
    copied on one side while the other slept or was disconnected.

    If the hash on disk matches what the clipboard holds now, the content
    has not changed since it was last recorded, so the *stored* timestamp
    and the *stored* kind -- not `current_kind` -- are the real age and
    kind of that content, and are returned unchanged: unchanged content
    did not change what kind of content it is either, and the stored value
    is the one this agent already announced to a peer, possibly on an
    earlier connection. Returning `now` here instead would make every such
    clip look freshly copied, winning it every reconciliation and
    clobbering the peer systematically on every reconnect. If the hashes
    differ, or nothing was ever stored, the content changed (or first
    appeared) while nothing was watching, and only `now` is honest about
    its age -- `current_kind` is equally honest about what it is, since it
    came from that exact same read, and is returned as-is rather than
    guessed.

    Before Task 7 made clipboard.read() itself kind-aware, this parameter
    did not exist and this branch hardcoded KIND_TEXT: resolve_current_clip_state
    below, this function's only non-test caller, could only ever derive
    `current_hash` from a text/plain read, so there was no independent
    "current kind" to thread through yet. Task 6's report on this
    function named the exact failure a purely mechanical fix to THIS
    function's caller (not its own signature) would have left behind: once
    read() became kind-aware, a caller that computed `current_hash` from
    the new (kind, bytes) pair while quietly dropping the kind would run
    clean and pass the whole suite, silently fabricating KIND_TEXT for a
    PNG hash right here -- one frame below the line that actually changed,
    with no call-site failure to point at it, since a same-arity caller
    changing its body is invisible to every test that only checks THIS
    function's behaviour. Fixed by threading the kind through as a real
    parameter instead of leaving it something only the caller could
    derive: a caller that now drops it fails to call this function at all
    (TypeError), everywhere it is tested, rather than silently returning a
    wrong answer only the one production call site would ever produce. See
    resolve_current_clip_state's own docstring for the other half: it is
    what actually derives `current_kind` from clipboard.read()'s pair.

    A None current_hash (the clipboard empty, unreadable, or holding content
    over its kind's limit -- see resolve_current_clip_state, which is what
    turns all three into this one value) always
    wins over whatever is on disk, regardless of what was previously
    stored: resolve_freshness never compares timestamps when either side's
    hash is None, so the timestamp returned here is never actually read.
    Its kind is None too, matching the null-iff-null rule decode_clip_state
    enforces on the wire -- a literal None, not `current_kind`, since a
    caller reporting a None hash (nothing announceable on the clipboard,
    whichever of the three reasons it was) has no real kind to go with it
    either.
    """
    if current_hash is None:
        return None, now, None, None
    if stored is not None and stored[0] == current_hash:
        # `stored`, not a record rebuilt from it: see the docstring's
        # matched-branch note. tuple() rather than the value itself so a
        # caller that hands in a list still gets the record shape every
        # other branch returns, and so nothing downstream can mutate what a
        # caller still holds.
        return tuple(stored)
    return current_hash, now, current_kind, None


import hashlib


def sha256_hex(data):
    """Lowercase hex, no separators -- hashlib.sha256(...).hexdigest()'s
    native format already, and the exact shape every sha256 on the wire
    must match byte for byte against Swift's own sha256Hex.

    The risk this task exists to close is not the hex FORMAT -- Python's
    hexdigest() gives that for free -- it is what gets hashed. `data` must
    always be the clipboard's exact bytes: never the timestamp-prefixed
    [ts][text] wire payload (encode_clip_payload's output), and never a
    str re-encoded independently. Hashing either of those instead would
    leave this function itself passing its own tests while every call
    site that got it wrong took the "hashes differ" branch on every
    reconciliation -- the systematic clobber resolve_startup_state exists
    to prevent. See fixtures/hashes.json, read by both this suite
    (test_fixtures.py) and Swift's (FixtureTests.swift), so neither side
    can drift from the other's idea of what this function should produce;
    and test_watcher.py's TestWriteClipDecodesTheWirePayload /
    TestEchoBookkeeping literal-hash tests, which pin a known digest at
    the actual call sites rather than re-deriving it from this function.
    """
    return hashlib.sha256(data).hexdigest()


def resolve_current_clip_state(clipboard, stored, now):
    """Reconciles what the clipboard holds RIGHT NOW against what was last
    persisted. Mirrors Sources/clipwire/ClipStateStore.swift's
    resolveCurrentClipState: `clipboard.read()` returning None, or a pair
    whose body is empty, is never hashed, matching the wire contract that
    sha256 is null for exactly that clipboard state -- see
    resolve_startup_state for the rule this applies once a current hash is
    in hand.

    `current_kind` -- the OTHER half of resolve_startup_state's signature
    -- comes from this exact same read() call, never derived separately:
    clipboard.read() is Task 7's one canonical read, returning (kind,
    bytes) or None, so the kind of what was just hashed is sitting right
    there in the pair already. This is the one place in the file that
    turns a raw clipboard.read() into a (hash, kind) pair; every caller of
    resolve_startup_state goes through here rather than reading the
    clipboard and computing a kind independently, which is what keeps a
    hash and a kind from ever being paired up wrong.

    Content over its kind's limit resolves a NULL hash, with a log line --
    the third clipboard state, alongside "empty" and "unreadable", that has
    no announceable hash. Without it the announce path was the one path in
    the file with no size guard at all, and the omission was not merely
    untidy: _observe_local_change skips an oversized clip and returns
    BEFORE persisting anything, so the store keeps its older entry, this
    function hashed the oversized body anyway, resolve_startup_state saw a
    hash differing from the store and stamped `now`, and announce_clip_state
    persisted and announced it. That announcement beats anything the peer
    copied earlier -- and then _resolve_clip_state's SEND_MINE branch
    refuses to send it, correctly, at its own size guard. The peer has by
    then resolved WAIT_FOR_PEER and suppressed its own push, so its
    perfectly sendable clip never arrives: the wake flow v2 exists to serve,
    broken by content that cannot travel. A null hash makes the peer win and
    deliver, which is the outcome resolve_freshness already gives it for
    free.

    The two predicates are the SENDERS' own, character for character --
    _observe_local_change's and _resolve_clip_state's -- so "announceable"
    and "sendable" cannot drift apart. That includes their asymmetry:
    MAX_IMAGE_BYTES bounds the IMAGE, so an image at exactly the limit is
    legal here and at every send site, while MAX_TEXT_BYTES bounds the
    encoded text clip and so must leave room for its 8-byte timestamp.

    The verdict clauses (`over the image limit` / `over the text limit`) are
    the ones every other size-limit site already reports, byte for byte; the
    sentences differ because the EVENT differs -- nothing is being skipped
    on its way to the wire here, there is simply nothing to announce. The
    same reason _consume_image_reoffer words its own line differently while
    sharing the clause. Mirrored on the Mac side in
    Sources/clipwire/main.swift's resolveCurrentClipState, which is why that
    one had to grow a `log` parameter.

    One asymmetry with that mirror, stated rather than left to be noticed:
    Swift writes these two as an exhaustive `switch` over ClipKind, so a
    third kind would be a compile error there and is simply hashed unguarded
    here. That is exactly what this function did for BOTH kinds before the
    guards existed, choose_kind picks no third kind today, and inventing a
    behaviour for one would add a branch no test can reach -- so the
    difference is left as the honest one it is.
    """
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
    """Builds and sends this side's one-shot clip-state announcement:
    reconciles whatever the clipboard currently holds against the
    persistent store (so unchanged content keeps its true recorded age
    instead of looking freshly copied -- see resolve_startup_state),
    persists the reconciled value, and sends it.

    The send is unconditional on the save's success -- a local disk
    failure is not the peer's fault, and must not silently disable
    reconciliation for this connection the way gating the send behind the
    save's result would. Mirrors Sources/clipwire/main.swift's
    announceClipState.
    """
    if now is None:
        now = time.time()
    stored = load_clip_state(path=path)
    resolved = resolve_current_clip_state(clipboard, stored, now)
    # The line the design doc mandates by name for exactly this branch --
    # startup reconciliation finding that the content no longer matches what
    # was last recorded, so only `now` is honest about its age. Two things
    # rest on it: the acceptance checklist requires a divergence to be
    # visible in the log, and the design's one accepted trade-off (with both
    # clipboards changed while apart, the side whose agent was born more
    # recently wins) is justified on the grounds of being "visible in the log
    # rather than mysterious" -- which is only true if this line exists.
    #
    # Exactly the negation of resolve_startup_state's "the stored timestamp
    # is authoritative" condition, including the nothing-ever-stored case:
    # content that appeared while nothing was watching is the same judgement
    # as content that changed. A null hash is deliberately silent -- a
    # clipboard that is empty, unreadable, or holding content over its kind's
    # limit (all three resolve one, and the last says so in its own line)
    # never reaches a timestamp comparison at all, so there is no
    # reconciliation judgement to report.
    #
    # Logged HERE rather than inside resolve_current_clip_state, which
    # _resolve_clip_state's own store-failure fallback also calls with
    # stored=None: that call would then report "changed while apart" on every
    # clip-state frame arriving while the store is unreadable, when nothing
    # changed at all. The doc ties this line to startup reconciliation, which
    # is this function.
    if resolved[0] is not None and (stored is None or stored[0] != resolved[0]):
        log("clipboard changed while apart")
    # SPREAD WHOLE, and the fourth element is why. resolve_startup_state
    # returns the stored record intact on a hash match -- origin included --
    # so this is the second of the two writes in this file that can put an
    # origin on disk, and the one that makes the first mean anything.
    #
    # DO NOT tidy this into an explicit `origin=None` on the reasoning that
    # this function has nothing of its own to record. It is true and it is
    # beside the point: sshd spawns a new agent per connection, so by the
    # time any announce needs to say where these bytes came from,
    # Agent._consume_image_reoffer -- the only place that ever finds out --
    # died with an earlier process. Strip the origin here and it survives
    # exactly as long as the connection that learned it, so the suppression
    # works on the FIRST reconnect off in-memory state and dies on the
    # second. That is the shape of an inert fix, and it is the blocker this
    # design was rejected over before a line of it was written -- for the
    # third release running.
    try:
        save_clip_state(*resolved, path=path)
    except (OSError, ClipStateError) as error:
        log("could not persist clip state: %r" % error)
    send(TYPE_CLIP_STATE, encode_clip_state(*resolved))
    # Returned so the caller does not have to re-derive what it just sent.
    # The save above is best-effort, so reading the store back is not a
    # reliable way to recover this -- see Agent._resolve_clip_state's `mine`.
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
    """Which kind to sync, given the clipboard's offered MIME types.

    Text wins. Spreadsheets put a bitmap of the copied cells alongside the
    text, so preferring the image would turn every copied range into a picture
    of a table -- a regression of the primary flow in exchange for the new one.
    Screenshots and "Copy image" carry no text/plain, so they still arrive as
    images.

    Only image/png is considered, and on the PC that costs nothing: GPaste
    re-offers whatever image it holds in a long list of types, PNG among them,
    verified on the live machine down to a JPEG reading back as valid PNG.
    """
    if any(t.startswith("text/plain") or t in ("UTF8_STRING", "STRING", "TEXT")
           for t in types):
        return KIND_TEXT
    if "image/png" in types:
        return KIND_IMAGE
    return None


class WaylandClipboard:
    def __init__(self):
        # Set once a wl-paste call times out (or otherwise fails as an
        # OSError) and cleared the moment a call completes normally,
        # whatever its returncode -- so a hung selection owner in polling
        # mode logs the hang once, not once per tick for as long as it
        # lasts, while a later, separate hang still gets its own
        # first-occurrence log line once this one clears. Shared by every
        # wl-paste invocation read() or probe() makes (see _run_wl_paste):
        # this flag tracks "wl-paste is currently hanging", not which of the
        # calls a single read() or probe() makes is the one that hung.
        self._read_timeout_logged = False
        # Whether this WaylandClipboard has completed a read() or probe()
        # call yet. Checked and set once per such call (not once per
        # wl-paste invocation, and not once per METHOD -- see
        # _claim_first_call), so the very FIRST one of a connection logs the
        # duration of ALL its wl-paste calls unconditionally -- a real
        # baseline for both call shapes (--list-types and a body fetch),
        # not just whichever one happens to run first. See
        # SLOW_READ_SECONDS's own comment for why an unconditional first
        # reading matters: a threshold alone gives a reader outliers with
        # nothing to judge them against.
        self._first_read_done = False

    def ready(self):
        return os.path.exists(wayland_socket_path())

    def read(self):
        """(kind, bytes) for whatever the clipboard currently holds, or
        None when nothing is offered, choose_kind picks neither kind this
        agent syncs, or the chosen read comes back empty.

        The one canonical read. The startup seed, clipboard_became_ready,
        the reconciliation send branch and the watcher's HANDLER
        (Agent._local_change) all go through this single method, so which
        content wins when more than one kind is on offer is decided in
        exactly one place (choose_kind) instead of reimplemented at each
        call site. The one thing that does NOT come through here is the
        poll LOOP's change detection, which asks probe() below for a cheap
        token instead and only ever gets here through the handler it wakes.

        Two wl-paste invocations. The first, `--list-types`, is cheap and
        says what is on offer; choose_kind picks a kind from the answer --
        text over image, see its own docstring -- and the second asks for
        the body of exactly that kind, nothing else. A non-zero exit from
        either invocation means an empty or unreadable selection, matching
        the single-read behaviour this replaces: a normal state, not an
        error.
        """
        force_log = self._claim_first_call()
        listed = self._list_kind(force_log)
        if listed is None:
            return None
        return self._read_body(listed[0], force_log)

    def probe(self):
        """A cheap CHANGE TOKEN for the poll loop: compare it against the
        previous one, and only fetch the actual content when it differs.
        Never content in its own right -- nothing may hash, send or write
        what this returns.

        THE POINT, which is a spec requirement the plan dropped rather than
        an optimisation. PollingWatcher.pump used to call read() on every
        tick, and since read() became kind-aware that means forking
        `--list-types` AND `--type image/png` and pulling the entire body.
        With a screenshot sitting on the clipboard, a HEALTHY install --
        the safety net runs on every connection, not only degraded ones --
        forked two processes and piped up to MAX_IMAGE_BYTES every
        SAFETY_NET_POLL_SECONDS for the life of the connection, held two
        4 MiB buffers resident in `previous` and `current`, and memcmp'd
        them each tick. In degraded mode that is the same work every
        DEGRADED_POLL_SECONDS. And any read slow enough to cross
        SLOW_IMAGE_READ_SECONDS logged a duration line EVERY TICK -- up to
        86,400 lines a day across the SSH channel, which is the exact flood
        the logging gate was added to prevent, arriving through the one
        call site the gate cannot help.
        In v2 none of this existed: the read was text-only, so an image
        clipboard read back as nothing and cost nothing.

        So for an image the token is the offered TYPE LIST -- one cheap
        wl-paste call, a few hundred bytes -- and the body is never
        fetched here. For text the token is the body, unchanged from
        before: text has no equivalent cheap proxy, its cost is v2's and
        is not what the spec objects to, and keeping it identical means
        every text path behaves exactly as it always has.

        THE ADDED LATENCY, which the spec asks to have documented, and
        stated at the accuracy the evidence supports. The poll sees an
        image change only when the offered type list changes. It therefore
        catches every text<->image transition and every arrival on an empty
        clipboard, and it detects one image REPLACING another only if the
        two selections offer different type lists. On this installation
        they are expected to: GPaste takes over the selection a few seconds
        after any copy and re-offers the image under its own long list of
        types, so consecutive copies normally alternate between the source
        application's list and GPaste's. That expectation is not verified
        -- this project has no live Wayland machine to check it against --
        so if it does not hold, two image copies from the SAME source
        landing inside GPaste's takeover window (measured at one to four
        seconds) look identical to this loop and the second waits for the
        next type-list change. In healthy mode nothing is lost either way:
        GPaste's Update signal carries that change and the poll is only a
        net. In degraded mode it is a real gap, bounded by the next
        transition.

        RETURNS None on the same failures read() does, with one deliberate
        difference: an image whose BODY fetch would have failed or come
        back empty is not fetched at all here, so it reports a live token
        where read() would have reported None. That difference is an
        improvement rather than a compromise, and _observe_tick is the
        reason it has to be reasoned about at all -- its `read_ok` clause
        treats None as "no evidence either way" and resets an armed
        verdict. A transient image-body failure used to produce TWO
        spurious transitions (value->None->value), each signalling the
        worker and disturbing the verdict; the type list is stable across
        it, so now it produces none.

        The returned pair is deliberately NOT the shape read() returns for
        the same clipboard: (KIND_IMAGE, tuple-of-str) here against
        (KIND_IMAGE, bytes) there. They can never compare equal, so
        confusing the two is loud rather than silent -- do not "unify"
        them.
        """
        force_log = self._claim_first_call()
        listed = self._list_kind(force_log)
        if listed is None:
            return None
        kind, types = listed
        if kind == KIND_IMAGE:
            # SORTED, and that is a correctness guard rather than tidiness.
            # PollingWatcher.pump compares this token with !=, so its ORDER
            # would be as load-bearing as its membership -- and nothing in
            # this project can establish that `wl-paste --list-types` prints
            # a stable order for an unchanged selection, because there is no
            # live compositor to check it against. If it does not, the
            # consequence is exactly the defect this release exists to fix:
            # pump signals a change nobody made, _observe_tick arms, and one
            # more silent tick CONFIRMS -- a healthy install declared dead
            # and dropped to 1-second polling for the rest of the connection.
            # Under the body comparison this replaced, the bytes were stable,
            # so this is a NEW input class rather than an inherited risk.
            # Sorting removes the assumption instead of betting on it, and
            # costs nothing: every real change alters the membership, so an
            # order-insensitive token is not a change-insensitive one.
            #
            # A tuple, not the list: this value is kept as `previous` across
            # ticks, and a mutable one invites a caller to hold a reference
            # to something that later changes underneath the comparison.
            return kind, tuple(sorted(types))
        return self._read_body(kind, force_log)

    def _claim_first_call(self):
        """Whether this is the connection's first clipboard call, consuming
        the flag as it answers.

        Captured once per call and handed to every wl-paste invocation that
        call goes on to make, so ALL of them log unconditionally on the
        first one -- not just whichever happens to run first. See
        _first_read_done's own comment.

        Shared by read() and probe(), so "first" means the first of either.
        In production read() still wins: clipboard_became_ready's seed runs
        before any watcher is built. But the flag belongs to the clipboard,
        not to one method, and a baseline duration from a probe is worth
        exactly as much as one from a read.
        """
        force_log = not self._first_read_done
        self._first_read_done = True
        return force_log

    def _list_kind(self, force_log):
        """(kind, types) for what the clipboard currently offers, or None
        when the listing fails or choose_kind picks neither kind.

        The half of read() that probe() also needs, factored out rather
        than written twice: which kind wins for a given type list is
        choose_kind's single answer, and two callers deriving it from two
        copies of this code is exactly the drift that put four answers in
        four call sites before Task 7.
        """
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
            # No -n here: verified against the wl-clipboard manual that
            # --no-newline is applied automatically for any non-text MIME
            # type, so passing it explicitly would be redundant, never
            # incorrect. Left off so this call visibly differs from the
            # text one above, rather than carrying a flag that does nothing.
            result = self._run_wl_paste(
                ["--type", "image/png"], IMAGE_SUBPROCESS_TIMEOUT, SLOW_IMAGE_READ_SECONDS, force_log)
        if result is None or result.returncode != 0 or not result.stdout:
            return None
        return kind, result.stdout

    def _run_wl_paste(self, args, timeout, slow_after, force_log):
        """One wl-paste invocation, shared by every call read() OR probe()
        makes -- `--list-types` and both body-fetch shapes -- so the failure
        handling and the duration-logging gate live in exactly one place
        rather than duplicated per call site. probe() reaches this through
        _list_kind and, for text, _read_body; there is no wl-paste call in
        this class that does not come through here.

        `slow_after` and `force_log` together decide whether this call's
        duration gets a log line -- see SLOW_READ_SECONDS's own comment for
        why logging every call unconditionally is not an option at
        production scale (172,800 lines a day in degraded mode). Gated on
        every path that actually reaches the subprocess: success, a
        timeout, or any other OSError. Skipped entirely (not even measured
        against the gate) only when wl-paste is not installed at all, where
        there is no meaningful duration to report and it would just be
        noise next to the "not installed" line.

        The "wl-paste failed" line below is deliberately NOT subject to
        this gate -- it is already deduplicated by _read_timeout_logged, a
        different mechanism for a different purpose (an ongoing hang logs
        once, not the rate of successful reads), so it stays unconditional
        on the first occurrence of a hang the way it always has.

        Returns the CompletedProcess, or None on a missing binary, a
        timeout, or any other OSError -- all three already logged here, so
        callers only need to treat None as "nothing to read".
        """
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
        """The gate itself: log iff this is forced (the connection's first
        read -- see _first_read_done) or the read actually took long enough
        to be worth a line. A timeout always satisfies the threshold on its
        own (duration is at least `timeout`, always chosen well above
        `slow_after`), so the exception path above needs no special case
        here beyond calling this the same way the success path does.

        Deliberately worded without the literal text "wl-paste" in the
        line itself, unlike the hang log above -- so a log-line-counting
        test that greps for that word keeps counting failures, not reads.
        """
        if force_log or duration >= slow_after:
            log("clipboard read (%s) took %.3fs" % (" ".join(args), duration))

    def write(self, kind, data):
        """wl-copy does not exit — it stays resident as the selection owner.

        It must be spawned detached with its pipes closed. Waiting on it, or
        holding its fds, hangs the agent.
        """
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
    # available() never touches the clipboard, but GPasteWatcher takes one for
    # its safety net, so hand it the same one production would use rather than
    # a None that a future start() call could trip over.
    gpaste = GPasteWatcher(_select_clipboard())
    log("info gpaste: %s" % ("available" if gpaste.available() else "unavailable, will poll"))

    return 0 if ok else 1


# ============================================================================
# 9. Watchers and entry — GPaste, polling, the safety net, main, __main__
# ============================================================================

import threading

GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
# The BUS name is org.gnome.GPaste; org.gnome.GPaste2 is the INTERFACE name on
# that object. Verified on the target machine: --dest org.gnome.GPaste2 has no
# owner, so probing it would make available() always false and silently leave
# the watcher on the polling fallback forever.
GPASTE_BUS_NAME = "org.gnome.GPaste"


def _env_seconds(name, default):
    """An interval overridden from the environment, or `default`.

    Exists for PairingHarness, which needs sub-second tiers to exercise in
    seconds what production does in minutes. Production sets none of these, so
    the constants below are what a real agent runs -- verified by grepping the
    whole repository (not just this file) for CLIPWIRE_, and by reading the
    launchd plist and the ssh arguments production actually invokes: neither
    sets an environment variable, and ssh's own default AcceptEnv/SendEnv
    forwards none either, so there are two independent reasons a real deploy
    never sees one of these set, not one.

    Anything unparseable or non-positive returns the default rather than
    raising: a typo in a harness must not produce a zero-second poll that spins
    a core, and must not take down an agent on a machine where the variable was
    never meant to be read. No env= parameter, unlike runtime_dir() and
    clip_state_path() above: those compose a dict for a subprocess call and
    are exercised with fabricated environments in tests, where a real dict
    argument earns its keep. This reads a single scalar out of the one
    environment this process actually has, the same shape _select_clipboard()
    already uses just above for CLIPWIRE_FAKE_CLIPBOARD -- another
    harness-only override nothing in production ever sets.
    """
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# available() below probes the bus NAME, but GPaste tracks the clipboard
# through a gnome-shell extension: a GNOME upgrade can leave the daemon
# running and the bus answering while the extension is disabled, so Update
# never fires, PC->Mac sync is silently dead, and the polling fallback does
# not engage because it is keyed on the bus being unreachable rather than on
# events being absent. This is the detection budget for that state -- an
# acceptable worst case for NOTICING a broken subscription.
SAFETY_NET_POLL_SECONDS = 30.0
# What the safety net polls at once it has concluded the event source is dead.
# Deliberately NOT SAFETY_NET_POLL_SECONDS: 30 seconds is a detection budget,
# and leaving it as the OPERATING interval would keep PC->Mac sync half a
# minute behind while reporting itself as working. This is also make_watcher's
# fallback interval, shared from one place so degraded mode and the
# never-had-GPaste mode cannot drift apart.
DEGRADED_POLL_SECONDS = 1.0


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


try:
    import ctypes
except ImportError:
    # ctypes is standard library but it is an EXTENSION module, so a
    # stripped or unusual build can genuinely lack it -- and this file is
    # copied to whatever Python the PC happens to have. Everything ctypes
    # buys here is one belt on top of stop()'s existing braces, so an agent
    # that cannot import it must still start; taking the whole module down
    # at import over an optional hardening would be far worse than losing
    # the hardening.
    ctypes = None
import signal

PR_SET_PDEATHSIG = 1


def _load_libc():
    """libc for prctl, or None where it is unavailable.

    Returns None on macOS and anywhere else without a usable libc: the agent
    only ever runs on the PC, but the test suite runs on both, and an
    import-time failure would take the whole module down.

    The `ctypes is None` check is not defensive typing, it is the other half
    of the guarded import above -- and the guard that is easy to get wrong.
    With `ctypes` bound to None, `ctypes.CDLL(...)` raises AttributeError,
    which is neither ImportError nor OSError: written without this line, the
    module-level try/except would look like it handled a missing ctypes while
    actually converting an ImportError into an AttributeError two lines
    later, and the module would still fail to import.

    OSError alone below, and nothing else: `ctypes.CDLL` reports a library it
    cannot load as OSError. An unavailable ctypes is handled at the import,
    which is where that failure actually lives, rather than caught a second
    time here where it can no longer arrive.
    """
    if ctypes is None:
        return None
    try:
        return ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None


# Resolved ONCE, here, at module import time -- on the only thread that
# exists at that point, long before GPasteWatcher.start() ever forks
# anything. _pdeathsig_preexec reads this global instead of calling
# _load_libc() itself: lazy would be an import inside preexec_fn, which can
# deadlock a forked child in a threaded process. preexec_fn runs in a forked
# child of a process that is genuinely multi-threaded by the time start()
# runs (the previous watcher's pump thread, the safety-net poll thread), and
# Python's import machinery takes a lock that fork() does not release --
# fork() only clones the calling thread, so if some other thread held that
# lock at the instant of fork, the child inherits it permanently held and
# hangs forever trying to import. That wedges the gdbus child before it ever
# execs: the same symptom this task removes, reintroduced by a subtler path.
# Resolving here means the import already happened before any thread or fork
# existed, so _pdeathsig_preexec itself touches no lock at all.
_LIBC = _load_libc()
# And the SYMBOL, resolved here too, for the same reason and against the same
# hazard one layer down. ctypes binds a CDLL's symbols LAZILY: `_LIBC.prctl`
# performs a dlsym on first access and caches the result on the library
# object, so with the lookup left inside _pdeathsig_preexec the FIRST forked
# child was the one paying for it -- inside preexec_fn. dlsym takes the
# dynamic loader's lock; fork() clones only the calling thread and releases
# nothing another thread holds, so a child forked at the wrong instant
# inherits that lock held forever and wedges before it ever execs. That is
# precisely the stuck-gdbus-child symptom this whole mechanism exists to
# remove, reintroduced by a subtler path than the import _LIBC already
# closed. Resolved on the only thread that exists at import time, so
# _pdeathsig_preexec touches no lock at all.
#
# None exactly when _LIBC is None, so the function below has one thing to
# check rather than two.
_PRCTL = _LIBC.prctl if _LIBC is not None else None


def _pdeathsig_preexec():
    """Ask the kernel to SIGTERM this child when its parent dies.

    Runs between fork and exec. This is what actually reaps the gdbus child:
    stop() cannot be relied on, because sshd kills the agent outright when the
    channel drops and no cleanup path runs. terminate() is also not enough on
    its own -- glib installs SIG_IGN for SIGPIPE, so the orphan survives its
    stdout closing and lingers until the session ends.

    Reads _PRCTL, never `_LIBC.prctl`: every operation in this function has
    to be one that a forked child of a threaded process can safely perform,
    and an attribute lookup on a CDLL is not one of them. See _PRCTL.
    """
    if _PRCTL is None:
        return
    _PRCTL(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    # The parent can die between the fork above and the prctl call just made,
    # in which case the signal we just asked for will never be delivered and
    # this child would outlive it anyway. getppid() == 1 means exactly that.
    if os.getppid() == 1:
        os._exit(0)


# For the guards below. A handler exception with no traceback is nearly
# undebuggable from the PC, where a log line is all anyone gets.
import traceback


def _handle_observer_error(error, what):
    """The one place a watcher thread decides whether an exception is worth
    dying for. Shared by every such thread so the rule cannot drift between
    them -- two copies of this decision is how one of them quietly becomes the
    lenient one.

    Fatal: the channel is gone. ValueError is what a CLOSED stdout raises on
    write, and Agent.send writes straight to it; log() writes to stderr, which
    dies with the same channel. Mirrors run()'s rule for stdin EOF -- this
    agent is one process per connection, and exiting IS how it reports a dead
    channel. Carrying on would leave a process syncing into a pipe nobody
    reads.

    Everything else is disposable: the next observation re-reads the clipboard
    anyway rather than replaying anything. Logged with its traceback rather
    than swallowed -- a thread that dies quietly is the defect this whole split
    exists to remove, and a thread that swallows quietly is the same defect one
    debugging session later.
    """
    if isinstance(error, (BrokenPipeError, ValueError)):
        log("%s stopping, the channel is gone: %r" % (what, error))
        os._exit(0)
    log("%s error: %s" % (what, traceback.format_exc()))


def _start_observer(event, stop, on_change):
    """Start the ONE thread allowed to call the clipboard handler.

    Every other thread in a watcher is a reader -- the gdbus pump, the poll
    loop -- and readers only ever set `event`. A reader that called the handler
    could block on Agent._observe_lock and could die of anything the handler
    raised, silently and for the rest of the connection; that is the production
    defect this shape removes by construction. There is exactly one of these
    per watcher tree, which is also what keeps "one observation path" true: one
    place decides what a local change means, and only one thread is ever inside
    it.

    No queue, deliberately. The handler reads clipboard STATE, not the contents
    of any event -- GPaste's Update payload carries nothing this agent uses,
    and the poll loop's own signal carries nothing either: it compares a
    probe() token and sets the event, and the token never leaves that loop.
    So signals arriving while the handler runs collapse into one set() and one
    re-read afterwards. That is
    the semantics a clipboard wants; a queue would hold nothing and only add a
    way to fall behind.
    """
    def observe():
        while not stop.is_set():
            event.wait()
            if stop.is_set():
                return
            # Cleared BEFORE the handler runs, so a signal arriving DURING it
            # re-arms the event and earns its own re-read afterwards. Clearing
            # afterwards would drop exactly the change that landed while we
            # were busy looking at the previous one.
            event.clear()
            try:
                on_change()
            except Exception as error:
                _handle_observer_error(error, "observer")

    worker = threading.Thread(target=observe, daemon=True)
    worker.start()
    return worker


class GPasteWatcher:
    """Event-driven. Python has no stdlib DBus binding, so this shells out to
    gdbus monitor and parses its output line by line.

    Composes a PollingWatcher as a slow safety net (see
    SAFETY_NET_POLL_SECONDS): a subscription that has silently stopped
    delivering is indistinguishable from an idle clipboard until something
    else actually looks at the clipboard. What that poller looks at is a
    probe() TOKEN, not the content -- for an image, the offered type list --
    which is enough to tell "something changed" from "nothing did", and is
    all this class ever asked of it.

    `clipboard` is only used by that safety net -- available() never touches
    it -- but it is a required argument rather than a defaulted one, because a
    GPasteWatcher built without one would look healthy and silently have no
    safety net at all, which is the exact failure this class exists to catch.
    """

    def __init__(self, clipboard,
                 safety_net_interval_seconds=SAFETY_NET_POLL_SECONDS,
                 degraded_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None, on_idle_tick=None):
        self.clipboard = clipboard
        self._process = None
        self._thread = None
        # The observer thread and the only thing that wakes it. Both readers --
        # the gdbus pump and the safety-net poll below -- set this event and
        # neither calls the handler, so the pump can neither block on
        # Agent._observe_lock nor die of an exception the handler raised, and
        # the poll cannot either. See _start_observer.
        self._worker = None
        self._event = threading.Event()
        self._stop = threading.Event()
        # Accepted Update lines. Written ONLY by the gdbus pump thread and read
        # only by the safety net's poll thread, so `+= 1` has a single writer
        # and cannot lose an update; no lock is needed, and the worst the
        # reader can do is see the previous value a moment longer -- which is
        # exactly the window the ordering in start()/PollingWatcher.start is
        # arranged to absorb.
        self._signals = 0
        # Only ever touched by _observe_tick, i.e. by that one poll thread.
        self._signals_at_last_tick = 0
        # The first half of the two-tick verdict: a divergence is waiting to be
        # confirmed or cleared by the next tick. Deliberately NOT carried across
        # a watcher rebuild the way _degraded is -- a Wayland flap restarts the
        # run from scratch, exactly as _signals_at_last_tick already does,
        # because the counter it would be compared against starts over too.
        self._armed = False
        # Latch, mirroring Agent._clip_state_sent's shape AND its lifetime:
        # assigned in exactly one place (_observe_tick), never cleared, and
        # carried across a watcher rebuild by Agent._event_source_degraded,
        # which is where the connection-scoped copy lives and why `degraded`
        # is an argument here at all. A watcher-only latch would re-arm the
        # detection budget on every mid-connection Wayland flap -- see that
        # attribute's own comment.
        self._degraded = degraded
        self._degraded_interval = degraded_interval_seconds
        # Reports the diagnosis back to whoever owns the connection-scoped
        # copy. Called at most once, from _observe_tick, behind the same latch
        # that gates the log line.
        self._on_degrade = on_degrade
        # PollingWatcher is defined below this class: resolved at call time
        # from module globals, so the forward reference is fine -- nothing
        # constructs a GPasteWatcher until main() runs.
        #
        # Already-degraded watchers start ON the degraded interval: coming up
        # on the detection budget would leave PC->Mac 30 seconds behind for
        # another full cycle on an installation already diagnosed.
        #
        # Handed THIS watcher's event, so the composed poller signals the same
        # worker the gdbus pump does and starts none of its own. That is what
        # keeps "one observation path" true now that neither reader calls the
        # handler: one place decides what a local change means, and only one
        # thread is ever inside it.
        #
        # `on_idle_tick` is passed straight through rather than consulted
        # here: it belongs to the poll loop's no-change branch, and this
        # class's own tick observer (_observe_tick) judges the EVENT SOURCE
        # from probe tokens and must keep doing only that. Composing them
        # would put a second question inside the one function whose silence
        # is what the safety net exists to break.
        self._safety_net = PollingWatcher(
            clipboard,
            degraded_interval_seconds if degraded else safety_net_interval_seconds,
            on_tick=self._observe_tick, event=self._event,
            on_idle_tick=on_idle_tick)

    def _observe_tick(self, previous, current):
        """Judge the event source from one safety-net tick.

        Runs on the safety net's own poll thread, after it has already
        SIGNALLED any change -- see PollingWatcher.start, and read the grace
        period note there before trusting `signals` to be current.

        Order within a tick is load-bearing and free: the clipboard is
        probed FIRST, in PollingWatcher's loop, and the counter is
        snapshotted below only afterwards. That wl-paste fork hands a signal
        still in flight a millisecond of grace before the first strike is
        recorded. Still a fork after the loop moved from read() to probe():
        probe() is fewer wl-paste calls, never zero. Do not reorder it for
        tidiness; the second tick covers the tail, but this costs nothing
        and shortens the tail.

        `previous`/`current` are probe() TOKENS, not content -- for an image
        they are the offered type list. This function only ever compares
        them and tests them against None, which is all a token supports, and
        all it ever needed.
        """
        signals = self._signals
        # A TOKEN difference ALONE does not prove the signal path missed it.
        # When GPaste is healthy the user copies something, the signal fires
        # and is handled -- and then this tick also sees a token differing from
        # the poll's own baseline, because the poll keeps one. Concluding
        # "dead" from the difference alone would degrade every healthy
        # installation to polling on the user's first copy, which is worse than
        # the bug this safety net exists to fix. The count of accepted signals
        # is the discriminator: the source is dead only if the clipboard moved
        # while no signal arrived to report it.
        #
        # "Token", not "content", throughout this function: for an image these
        # values are the offered type list, so what this tick observes is that
        # the SELECTION changed, not what it changed to. That is the only
        # question asked here -- and it is why a change the token cannot see
        # (an image replacing an identically-typed one) is not a false verdict
        # either: no divergence is observed, so nothing arms.
        #
        # Benign for the VERDICT, not free. When that invisible change is an
        # image re-offer, the poll's idle branch signals for it and the
        # worker absorbs it, while this tick records no strike -- so the
        # design's "health accounting outranks absorption" rule is met on the
        # change path and not on the idle one. See PollingWatcher.pump's
        # no-change branch for the whole of that gap and why it is disclosed
        # rather than closed; a reader who arrives here first should not
        # conclude the case is simply harmless.
        #
        # Both probes must also have SUCCEEDED. probe() returns None for a
        # failed wl-paste (WaylandClipboard keeps a dedicated one-shot log line
        # for exactly that timeout) and for a genuinely empty selection, and
        # neither involves a selection change -- so the counter is GUARANTEED
        # not to have moved, and this needs no race at all to misfire: one
        # flaky wl-paste on a healthy, idle system would permanently degrade
        # the connection while blaming the gnome-shell extension for it, twice
        # over, since the failure and its recovery both look like changes.
        # _local_change returns early on empty text anyway, so a None
        # transition can never produce a sync and is no evidence of one being
        # missed. Cost: with an empty clipboard at connect, the first
        # None->text change is still REPORTED but is not counted as evidence,
        # so the verdict waits for the change after it -- deferred, never lost.
        #
        # There are strictly FEWER such transitions since the loop moved to
        # probe(): a transient failure of the image BODY fetch used to produce
        # two of them (value->None->value), each one signalling the worker and
        # disturbing this verdict, and probe() does not fetch a body at all.
        # A failing --list-types still produces them, which is the case this
        # clause was written for.
        read_ok = previous is not None and current is not None
        # "Still" as in since the previous tick: _signals_at_last_tick is
        # assigned at the bottom of every one.
        still_silent = signals == self._signals_at_last_tick
        # ONE diverging tick is not a verdict, and this is a reversal of v2's
        # rule rather than an accident -- see the design doc, "The verdict now
        # needs two consecutive ticks". v2 judged from a single tick, which was
        # safe only because the poll loop called the handler SYNCHRONOUSLY:
        # _local_change always forks wl-paste, so milliseconds always elapsed
        # between the content read and this comparison, enough for a signal
        # already in flight to be counted. v3 decoupled the loop from the
        # handler for reasons of its own and deleted that grace period with it.
        # Nobody had named it load-bearing; the ruling outlived its premise.
        #
        # What replaces it is hysteresis, because the alternative -- an
        # acknowledgement from the worker -- would re-couple this thread to it
        # and restore the wedge path the decoupling exists to remove.
        #
        # A divergence ARMS; the next tick CONFIRMS if the counter is still
        # unmoved. What may clear an armed run is the crux, and the first
        # version of this rule got it wrong: the armed state is a claim about
        # the EVENT SOURCE, not about the clipboard, so only evidence about the
        # source may clear it.
        #
        #   - the counter MOVED: the source is alive. A signal that was merely
        #     in flight when the run was armed lands here, which is exactly the
        #     race this shape exists to absorb. Reset.
        #   - a probe FAILED: probe() returns None for a timed-out wl-paste and
        #     for an empty selection alike, so it is evidence either way about
        #     nothing at all. Reset.
        #   - the token SETTLED: says nothing whatever about the source. It
        #     must NOT reset, and a rule that cleared the run here looked
        #     symmetrical and was not: a dead source on any machine whose copies
        #     fall more than one tick apart would arm, clear, arm, clear and
        #     never once be diagnosed.
        #
        # DO NOT restore the one-tick verdict without restoring the synchronous
        # call. Two ticks will look like one too many to a reader who cannot
        # see the premise that died.
        #
        # The cost is worst-case detection moving from one tick to two --
        # intended, and not symmetrical with the error it prevents: a false
        # positive permanently degrades a HEALTHY machine, while a late true
        # positive costs one more interval on a machine that is already not
        # syncing. Every change is still REPORTED throughout either way; only
        # the interval the safety net polls at is at stake.
        if not read_ok or not still_silent:
            confirmed = False
            self._armed = False
        elif self._armed:
            confirmed = True
            self._armed = False   # the run is spent, whatever is done with it
        else:
            confirmed = False
            self._armed = previous != current
        if confirmed and not self._degraded:
            self._degraded = True
            # Report what was observed, not a diagnosis this thread cannot
            # make. A production line once read "GPaste is not reporting
            # clipboard changes (is the gnome-shell extension enabled?)" on a
            # machine where the extension WAS enabled and active, the bus name
            # was owned, and a direct probe caught three Update signals for
            # three copies -- the line asserted a cause it could not know and
            # carried no evidence, so the real mechanism was never found. The
            # fields below are exactly what this tick knows: the accepted-
            # signal count, its value one tick ago, and whether each thread on
            # the observation path is still alive. worker_alive is a dead/not-
            # dead bit ONLY -- see its own docstring for what it cannot prove.
            #
            # %g, not %.1f: this is exercised with millisecond-scale intervals
            # in tests, where %.1f renders "0.0s" and the line would misstate
            # what the code actually did.
            log("GPaste reported no clipboard change while the content changed "
                "(signals=%d signals_at_last_tick=%d pump_alive=%s worker_alive=%s); "
                "the gnome-shell extension being disabled is one possible cause. "
                "Polling every %gs for the rest of this connection."
                % (signals, self._signals_at_last_tick,
                   self._thread.is_alive() if self._thread else False,
                   self.worker_alive(),  # dead, not wedged -- see worker_alive()
                   self._degraded_interval))
            self._safety_net.interval = self._degraded_interval
            if self._on_degrade is not None:
                # Last, so this watcher's own state is fully consistent before
                # the verdict escapes it.
                self._on_degrade()
        self._signals_at_last_tick = signals

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
            # Two statements per accepted line, deliberately. This thread must
            # never block and never raise: it is the only thing that keeps
            # _signals honest, and _signals is what the safety net uses to tell
            # a dead event source from a live one. Calling the handler here --
            # as this did until v3 -- made it able to block on
            # Agent._observe_lock and able to die of any exception the handler
            # raised, silently and for the rest of the connection, while the
            # gdbus child stayed alive and went on printing lines nobody
            # counted. Both were candidate causes of a production false
            # positive in which a healthy event source was declared dead;
            # neither was ever proven, and this shape removes both without
            # needing to know which it was.
            #
            # Counting still happens BEFORE the dispatch, and now cannot be
            # delayed by anything the handler does: on_change is
            # Agent._local_change, whose clipboard read is up to two wl-paste
            # round trips -- SUBPROCESS_TIMEOUT (3s) for --list-types plus
            # IMAGE_SUBPROCESS_TIMEOUT (10s) for an image body, so 13 seconds,
            # not the 3 this line claimed while that read was text-only -- and
            # a safety-net tick landing inside that window must not see an
            # unmoved counter on a live source. Thirteen seconds is a large
            # fraction of a 30-second tick, which is the whole argument.
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    self._signals += 1
                    self._event.set()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()
        # The ONE thread allowed to call on_change -- in production
        # Agent._local_change. The safety net below signals the same event
        # rather than being given the handler, so a change only IT catches
        # still goes through one observation, one one-shot echo suppression
        # and one _last_seen. A parallel path there would be a second copy of
        # echo logic this project has already fixed two races in.
        #
        # Its cost, deliberately accepted: _local_change stamps the clip at
        # the moment IT observes the change, so a clip caught only by the
        # safety net carries a timestamp up to one safety-net interval late
        # (30 seconds with the production default).
        self._worker = _start_observer(self._event, self._stop, on_change)
        self._safety_net.start()

    def worker_alive(self):
        """For the safety net's verdict line. A live pump with a dead worker is
        silent failure: the counter keeps climbing, so every observer concludes
        the event source is healthy while nothing is being synced at all.

        Detects a DEAD worker, not a WEDGED one, and that gap is now the
        likely case rather than a corner one: _start_observer wraps the
        handler call in `except Exception`, so the worker thread surviving is
        the normal outcome and outright death is nearly unreachable. A worker
        blocked inside Agent._local_change (a hung wl-paste, say) is still
        `is_alive()` -- busy is not the same as working. So `worker_alive=True`
        in a verdict line rules out "the thread exited"; it proves nothing
        about whether the thread is still doing anything useful."""
        return self._worker is not None and self._worker.is_alive()

    def stop(self):
        """Session teardown (Agent.clipboard_lost), NOT the degrade path.

        Terminating the gdbus child is correct here -- the Wayland session it
        was watching is gone -- and deliberately absent from _observe_tick's
        switch, where the subscription is left alive to recover on its own.
        """
        self._stop.set()
        # The flag alone does not reach a worker parked in _event.wait(): only
        # a signal that will never come would wake it, and Agent.clipboard_lost
        # drops its reference to this watcher the moment stop() returns, so a
        # thread left parked here can never be reached again and every Wayland
        # flap leaks another one. Deliberately not JOINED, though: stop() runs
        # on the main protocol loop, and waiting on a worker that is inside
        # _local_change's clipboard read would stall it for up to
        # SUBPROCESS_TIMEOUT + IMAGE_SUBPROCESS_TIMEOUT -- 13 seconds on an
        # image clipboard, since that read is --list-types followed by the
        # body. This is a promise about how long clipboard_lost() can block
        # the protocol loop, not a passing remark, and 13s of a frozen main
        # loop is a different proposition from the 3s it used to say.
        self._event.set()
        self._safety_net.stop()
        if self._process:
            self._process.terminate()


class PollingWatcher:
    """Degraded mode: forks wl-paste on every tick, so it runs at a slower
    interval than the Mac's in-process poll.

    Change detection goes through clipboard.probe(), NEVER clipboard.read().
    That is a correctness-relevant distinction rather than a tuning one, and
    it is the whole reason probe() exists: read() pulls the entire body, and
    for an image that is up to MAX_IMAGE_BYTES through a pipe on EVERY tick
    of EVERY connection, healthy ones included -- see probe()'s own docstring
    for what that cost was and for the detection latency the cheaper token
    buys it. Whatever probe() returns is compared and thrown away; the
    handler this loop wakes does its own read().

    `interval` is re-read on every iteration, so a caller can change the poll
    rate mid-flight -- GPasteWatcher does exactly that when its safety net
    concludes the event source is dead -- without stopping and restarting the
    loop.

    `on_tick` is an optional pure observer, invoked with (previous, current)
    after EVERY tick, change or not. Those two are probe() TOKENS, never
    content -- for an image, the offered type list -- so an observer may
    compare them and test them for None and nothing else. It exists so this
    stays the file's only compare-and-notify loop, with a single owner of
    `previous`: the safety net judges the event source from these
    observations rather than running a second comparison of its own.

    `on_idle_tick` is an optional PREDICATE, asked only on ticks where the
    token did not move, and answering True makes this loop signal an
    observation anyway. It exists because a token that never changes is not
    the same thing as nothing worth looking at: an image applied from the
    peer leaves an expectation armed (Agent._expect_reoffer) that only an
    observation can resolve, and on a machine with no GPaste the clipboard
    it is waiting on never moves again. It must stay a cheap predicate --
    this thread must not read the clipboard or decide anything, for the same
    reason it signals rather than calling the handler.

    `event` is how this loop reports a change, and it is the whole reason a
    poll loop and a gdbus pump can share one handler. Composed as
    GPasteWatcher's safety net it is handed THAT watcher's event, so both
    readers wake the same single worker; standalone -- what make_watcher
    returns when GPaste is unavailable -- it makes its own and starts its own
    worker, and then `on_change` is required rather than optional.
    """

    def __init__(self, clipboard, interval_seconds, on_tick=None, event=None,
                 on_idle_tick=None):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._on_tick = on_tick
        self._on_idle_tick = on_idle_tick
        self._stop = threading.Event()
        self._thread = None
        # Exactly one worker per watcher tree: whoever owns the event owns the
        # worker. A composed poller starting a second one would put two threads
        # back inside Agent._local_change and give the file two places that
        # decide what a local change means.
        self._event = threading.Event() if event is None else event
        self._owns_the_worker = event is None
        self._worker = None

    def available(self):
        return True

    def start(self, on_change=None):
        """`on_change` is required standalone and ignored when this poller was
        handed someone else's event -- that owner's worker calls the handler,
        and this loop only ever signals."""
        if self._owns_the_worker:
            self._worker = _start_observer(self._event, self._stop, on_change)

        def pump():
            # Guarded from its first statement: this thread is the only caller
            # of _on_tick, so an exception anywhere in it does not merely cost
            # an observation -- it takes the safety net's whole judgement with
            # it, and the watcher then reports itself healthy forever. That is
            # silent failure of the thing that detects silent failure. The
            # baseline read is inside a guard for the same reason: uncaught, it
            # killed the thread before the loop even existed.
            previous = None
            try:
                previous = self.clipboard.probe()
            except Exception as error:
                _handle_observer_error(error, "poll")
            while not self._stop.wait(self.interval):
                try:
                    current = self.clipboard.probe()
                    before = previous
                    if current != previous:
                        previous = current
                        # Signalled, never called: a reader thread that ran the
                        # handler could block on Agent._observe_lock and could
                        # die of anything it raised -- see _start_observer.
                        self._event.set()
                    elif self._on_idle_tick is not None and self._on_idle_tick():
                        # THE NO-CHANGE BRANCH, and the one place in this file
                        # where an observation is asked for without the token
                        # having moved. Agent._reoffer_pending is the only
                        # caller's predicate: while an applied image is still
                        # waiting for GPaste to re-offer it, the clipboard may
                        # sit perfectly still forever -- on a machine with no
                        # GPaste it certainly does -- and an expectation that
                        # is never looked at again is never disarmed, so the
                        # user's next image copy is absorbed as the re-offer
                        # that never came.
                        #
                        # Still a signal, never a call, and still no clipboard
                        # read on this thread: the predicate is a bare flag
                        # test, and the WORKER's single read is what decides
                        # between "the re-offer arrived" and "there is no
                        # GPaste here" -- one read, two outcomes separated by
                        # content. Deliberately NOT `previous = current` (there
                        # was no change to absorb) and deliberately below the
                        # real-change branch, which must always win.
                        #
                        # THE DISCLOSED GAP, health accounting. The v3.2
                        # design requires a re-offer seen by a signal-less
                        # poll to count toward the two-tick dead-source
                        # verdict BEFORE _consume_image_reoffer absorbs it,
                        # or a dead extension whose first unsignalled change
                        # happens to be a re-offer stays undiagnosed -- the
                        # safety net blinded by the very mechanism that
                        # proves it was needed. Through the CHANGE branch
                        # above that holds: _observe_tick compares tokens
                        # against THIS loop's own `previous`, which the
                        # worker never touches, so an absorption downstream
                        # cannot erase a divergence already observed.
                        #
                        # Through THIS branch it does not. A re-offer whose
                        # offered type list happens to match the previous one
                        # produces no token divergence at all, so it arrives
                        # here rather than above, is absorbed by the worker,
                        # and _observe_tick sees a settled token and arms
                        # nothing. NOT CLOSED, deliberately: closing it needs
                        # the WORKER to report what it found back into this
                        # watcher, and that is the coupling this file removes
                        # on purpose -- "signalled, never called" -- which
                        # would hand a wedged worker the power to stop the
                        # poll. The cost is bounded: the verdict is deferred
                        # to the next change the token CAN see, never lost,
                        # and every change is still reported throughout.
                        # Recorded in the v3.2 design's acceptance list as
                        # partially met, so it is inherited rather than
                        # rediscovered.
                        self._event.set()
                    # AFTER the signal, which used to be load-bearing and is
                    # now merely conventional -- say so rather than leave the
                    # old claim standing. While this loop CALLED on_change,
                    # that call (a clipboard read of up to SUBPROCESS_TIMEOUT
                    # + IMAGE_SUBPROCESS_TIMEOUT = 13s on an image clipboard,
                    # --list-types then the body, plus the send) was the grace
                    # period the event source got to deliver the signal for
                    # this very change before _observe_tick judged it missing.
                    # set() takes microseconds, so that grace is gone and the
                    # window is open: a copy landing in the last few
                    # milliseconds before this tick's read can be judged
                    # "missed" while its Update signal is still in flight.
                    #
                    # What still holds: the pump increments _signals BEFORE it
                    # dispatches, so a signal already read from gdbus cannot be
                    # missed by a slow handler. What no longer holds: this
                    # thread waiting for anything at all. Compensating belongs
                    # in the verdict, not here -- confirming `missed` across
                    # two consecutive ticks would restore a grace of one full
                    # interval with no thread coupling, at the cost of doubling
                    # the worst-case detection budget. Reintroducing a wait on
                    # this thread would re-couple the safety net to the handler
                    # and hand a wedged worker the power to stop the poll.
                    #
                    # `before` is passed rather than a bare `changed` flag so
                    # the observer can tell a real change from a failed read
                    # and its recovery -- both are `!=` here, and neither
                    # involves a selection change at all.
                    if self._on_tick is not None:
                        self._on_tick(before, current)
                except Exception as error:
                    _handle_observer_error(error, "poll")

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._owns_the_worker:
            # A worker parked in _event.wait() has nothing else to wake it, and
            # nothing can reach the thread once the caller drops this watcher.
            # Guarded on ownership: a composed poller's stop() must not wake
            # the shared worker, whose owner stops it with its own flag.
            self._event.set()


def make_watcher(clipboard, fallback_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None, on_idle_tick=None):
    """`fallback_interval_seconds` is the ONE knob for "how fast we poll when
    signals cannot be relied on", and there are two ways to arrive there:
    GPaste was never available, or its event source was diagnosed silent. It
    therefore reaches both branches below -- as the GPaste watcher's degraded
    rate and as the plain poller's interval -- so tuning it moves both and
    neither can quietly keep a hardcoded rate the log line then misquotes.

    `degraded`/`on_degrade` carry the dead-event-source verdict in and out, so
    it belongs to the connection rather than to whichever watcher reached it --
    see Agent._event_source_degraded.

    `on_idle_tick` reaches BOTH branches, and that is the point rather than
    symmetry for its own sake. It is what lets an armed re-offer expectation
    be disarmed by observation on a clipboard that never changes again, and
    the machine where that clipboard is guaranteed never to change again is
    the one with no GPaste at all -- the branch below, the plain poller. A
    hook wired only into the GPaste watcher would be inert in exactly the
    installation it exists for."""
    watcher = GPasteWatcher(clipboard, degraded_interval_seconds=fallback_interval_seconds,
                            degraded=degraded, on_degrade=on_degrade,
                            on_idle_tick=on_idle_tick)
    if watcher.available():
        if degraded:
            log("watching the clipboard through GPaste, already diagnosed as silent "
                "this connection, so polling every %.1fs" % fallback_interval_seconds)
        else:
            log("watching the clipboard through GPaste, with a safety-net poll every %.0fs"
                % SAFETY_NET_POLL_SECONDS)
        return watcher
    log("GPaste unavailable, falling back to polling every %.1fs" % fallback_interval_seconds)
    return PollingWatcher(clipboard, fallback_interval_seconds,
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
