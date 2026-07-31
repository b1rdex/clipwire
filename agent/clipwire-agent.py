#!/usr/bin/env python3
"""clipwire PC-side agent. Spawned by sshd; speaks frames on stdin/stdout.

Only frames go to stdout. Everything else goes to stderr — a stray print()
on stdout desynchronises the protocol.
"""

MAX_PAYLOAD_BYTES = 4194304
HEADER_BYTES = 5
TYPE_HELLO = 0x00
TYPE_CLIP = 0x01
TYPE_CLIP_STATE = 0x02
PROTOCOL_VERSION = 2
_KNOWN_TYPES = (TYPE_HELLO, TYPE_CLIP, TYPE_CLIP_STATE)


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


import json
import math

SEND_MINE = "sendMine"
WAIT_FOR_PEER = "waitForPeer"
DO_NOTHING = "doNothing"


class ClipStateError(FrameError):
    pass


def encode_clip_state(sha256, ts):
    """type-0x02 payload: {"sha256": <hex or null>, "ts": <float>}.

    Refuses a non-finite ts (nan/inf/-inf) rather than emitting one: Python's
    json.dumps would otherwise happily write a bare NaN/Infinity token that
    is not valid JSON, which Swift's JSONDecoder rejects outright -- so a
    non-finite ts stored locally would silently break the *peer's* handshake
    instead of failing here, on the side that produced it.
    """
    if not math.isfinite(ts):
        raise ClipStateError("refusing to encode a non-finite ts: %r" % ts)
    return json.dumps({"sha256": sha256, "ts": ts}).encode()


def decode_clip_state(payload):
    """Inverse of encode_clip_state. Raises ClipStateError — a FrameError,
    so main()'s existing `except FrameError` closes the connection exactly
    as a malformed hello does — on anything that is not a well-formed
    {"sha256": <str or null>, "ts": <finite number>} object.

    The finiteness check is the load-bearing part: json.loads, unlike
    Swift's JSONDecoder, accepts a bare NaN/Infinity/-Infinity and hands
    back a float that compares False against everything (nan > x, nan < x,
    and nan == nan are all False). Silently letting that reach
    resolve_freshness would compare a non-finite ts against a real one and
    send the decision somewhere neither side expects — so it is rejected
    here, before the value ever reaches a comparison, rather than compared.
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
    ts = parsed.get("ts")
    if not isinstance(ts, (int, float)):
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
    return sha256, float(ts)


def resolve_freshness(mine, peer):
    """Decides which side sends once both have announced what they hold.

    `mine` and `peer` are (sha256, ts) pairs -- the same shape
    decode_clip_state returns. Mirrors Sources/clipwire/Freshness.swift's
    resolveFreshness one branch at a time, including the tie-break, so the
    two files read side by side as one formula rather than a mirrored pair
    of conditions: mirrored conditions drifting apart has already bitten
    this project twice.

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


def log(message):
    """Diagnostics go to stderr. stdout carries frames and nothing else."""
    print(message, file=sys.stderr, flush=True)


class Agent:
    def __init__(self, stdin, stdout, clipboard, clip_state_path=None):
        self.stdin = stdin
        self.stdout = stdout
        self.clipboard = clipboard
        self.phase = PHASE_PENDING
        self.pending_clip = None
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
        # sent_at is this side's clock at the moment of sending, used later
        # for skew measurement -- nothing reads it back out yet. Read fresh
        # on every call (not cached at import time) since "the moment of
        # sending" is exactly when this method runs.
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

    def _on_clip_state(self, payload):
        """Decodes an incoming clip-state announcement, then either
        resolves it immediately or stashes it for clipboard_became_ready to
        resolve once our own side has reconciled.

        decode_clip_state is called bare, not wrapped in a local try/except
        -- one deliberate divergence from Sources/clipwire/main.swift's
        handleFrame .clipState case, which swallows via `try?`. That is
        correct THERE because Channel exposes no way to force-close the ssh
        process from that side -- but this agent, spawned fresh per SSH
        connection by sshd, both CAN and already DOES close the connection
        on a malformed/mismatched hello (_on_hello's own bare `raise
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

    def _resolve_clip_state(self, peer):
        """The resolution logic _on_clip_state defers until our own side
        has reconciled -- see its own doc comment for why. Called either
        directly (a clip-state arriving after clipboard_became_ready has
        already run once this connection) or from clipboard_became_ready
        itself (a clip-state that arrived before it and was stashed)."""
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
        if resolve_freshness(mine, peer) != SEND_MINE:
            # Hashes equal means we agree -- not a signal to resend. A peer
            # that is fresher means we wait. Conflating either with SEND_MINE
            # reintroduces a clobber or a ping-pong.
            return
        with self._echo_lock:
            last_seen = self._last_seen
        # Prefer what we already know over a fresh clipboard read, exactly
        # as clipboard_became_ready's own announce step does for a just-
        # applied pending clip -- the same race, one door over. If a clip
        # was recently applied (_on_clip's immediate path, or
        # clipboard_became_ready's pending-clip path), _last_seen already
        # holds its exact text; verifying its hash against mine confirms it
        # is still the SAME content mine describes, not a stale or
        # unrelated value (e.g. clipboard_became_ready's own connect-time
        # seed, which has no such relationship to the store). A fresh
        # clipboard.read() here would risk WaylandClipboard.write()'s
        # asynchronous, detached wl-copy spawn: it returns as soon as its
        # own stdin pipe closes, long before wl-copy necessarily registers
        # as the Wayland selection owner, so a read issued shortly after
        # could see stale content while mine (from the store) already
        # correctly reflects the new content -- sending stale text stamped
        # with a timestamp that looks like a valid, fresh reconciliation
        # response.
        if last_seen and sha256_hex(last_seen) == mine[0]:
            text = last_seen
        else:
            text = self.clipboard.read()
        if not text:
            return
        # The size bound matches _local_change's own send-side guard: this
        # branch reads the live clipboard independently, and without it,
        # winning a reconciliation over content at or beyond the cap would
        # build a frame that exceeds MAX_PAYLOAD_BYTES once wrapped in its
        # 8-byte timestamp prefix -- the peer's decode_frame rejects that as
        # oversized and drops the whole channel over a single large clip.
        if len(text) + TIMESTAMP_BYTES > MAX_PAYLOAD_BYTES:
            log("skipping a clip of %d bytes: over the frame cap" % len(text))
            return
        # mine[1] (our stored ts), not now: the content has not changed, only
        # been re-announced, so its recorded age must be preserved. Sending
        # with now would perpetually refresh it and let it win every future
        # reconciliation regardless of what happens next.
        self.send(TYPE_CLIP, encode_clip_payload(mine[1], text))
        # Same bookkeeping _local_change's own send path does, and for the
        # same reason: _last_seen is "what the peer already holds", and
        # after this send the peer does (soon) hold `text` too. Sources/clipwire's
        # own .clipState case has no EchoGuard-equivalent update here either,
        # but harmlessly so -- the Mac's PasteboardWatcher is
        # changeCount-driven and never fires on a non-change. The PC's
        # GPaste watcher DOES fire on non-changes (a history deletion emits
        # Update too) -- the entire reason _last_seen exists on this side at
        # all -- so skipping this update would let a later spurious signal
        # see the clipboard still reading `text`, wrongly conclude a genuine
        # local change happened, and resend it: wasteful at best, and a
        # silent clobber of a real Mac-side change made in the meantime at
        # worst, since the Mac applies any incoming .clip frame
        # unconditionally.
        with self._echo_lock:
            self._last_seen = text

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
        # clipboard.read() is a wl-paste round trip that can take up to
        # SUBPROCESS_TIMEOUT=3s.
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
        applied_pending = None
        if self.pending_clip is not None:
            # Supersedes the seed above with the more authoritative value:
            # once a queued clip from the Mac has actually been applied,
            # both sides genuinely hold ITS content, not whatever the PC's
            # clipboard held a moment earlier.
            applied_pending = self._write_clip(self.pending_clip)
            self.pending_clip = None
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
            else:
                announce_clip_state(self.send, self.clipboard, path=self._clip_state_path)
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
                self._resolve_clip_state(peer)
        if self._watcher is None:
            # The degraded verdict is handed back IN on a rebuild and reported
            # back OUT when it is first reached, so it belongs to the
            # connection rather than to whichever watcher happened to reach it.
            self._watcher = make_watcher(
                self.clipboard,
                degraded=self._event_source_degraded,
                on_degrade=self._note_event_source_degraded,
            )
            self._watcher.start(self._local_change)

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

    def _write_clip(self, payload):
        """Single place where we touch the local clipboard, so echo
        bookkeeping cannot be forgotten on one of the paths.

        `payload` is the wire-format [ts][text] encoding encode_clip_payload
        produces -- not bare text -- since v2's clip frame carries its own
        timestamp; both call sites (_on_clip's immediate-apply path and
        clipboard_became_ready's pending_clip-apply path) pass the raw frame
        payload through unchanged, so it is decoded here, once.

        A payload that fails to decode, or decodes with empty text, touches
        neither the suppression nor the clipboard -- swallowed quietly,
        mirroring Sources/clipwire/main.swift's handleFrame .clip case
        (`try? ... !decoded.text.isEmpty`). This is a deliberate asymmetry
        with _on_clip_state, which lets a malformed clip-state propagate and
        close the connection: Swift's OWN .clip case swallows too, and
        nothing in this task asks for a clip payload's malformed-content
        behaviour to change.

        Runs on the main thread. _local_change() (below) runs on the
        watcher's background threads -- two of them since the safety-net poll
        was added -- and reads this same bookkeeping, so the two fields are
        only ever touched under _echo_lock.

        Returns the (sha256, ts) pair that was applied and (best-effort)
        persisted, or None if the payload never decoded or decoded with
        empty text and nothing was applied. clipboard_became_ready uses
        this to announce a just-applied pending clip's state DIRECTLY,
        rather than re-deriving it through a fresh clipboard read -- see
        that method's own comment for why a read immediately after this
        call cannot be trusted to reflect it yet.
        """
        try:
            ts, text = decode_clip_payload(payload)
        except ClipPayloadError:
            return None
        if not text:
            return None
        with self._echo_lock:
            self._last_written = text
            self._write_gen += 1
            self._last_seen = text
        self.clipboard.write(text)
        # WaylandClipboard.write() spawns wl-copy DETACHED (Popen(...,
        # start_new_session=True)) and returns as soon as its own stdin pipe
        # is closed -- a hand-off, not a confirmation that wl-copy has
        # actually registered as the Wayland selection owner yet. The write
        # above and this sha256_hex/save_clip_state pair are therefore not
        # "the clipboard now reads this" -- they are "this is what we just
        # told the clipboard to hold, and it is authoritative regardless of
        # when (or whether) wl-copy finishes taking ownership." A caller
        # that instead re-read the clipboard to find out what was just
        # written would race that handoff.
        sha256 = sha256_hex(text)
        # The peer's timestamp, never now: this is the entire reason it
        # travels in the frame. Stamping it with now would make applied
        # content look freshly copied here and win the next reconciliation
        # against the machine it actually came from. A local disk failure
        # here is not the peer's fault and must not undo the write above or
        # propagate as a FrameError and tear down the channel.
        try:
            save_clip_state(sha256, ts, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        return sha256, ts

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
        `text == last_seen` check. There is deliberately no separate re-read --
        the existing snapshot IS the read-after-acquire.

        It BLOCKS rather than skipping, which matters: PollingWatcher's
        `previous` has already advanced past the change it is reporting, so a
        skipped observation is a clip LOST until the next change, not one
        merely deferred.
        """
        with self._observe_lock:
            self._observe_local_change()

    def _observe_local_change(self):
        # Snapshot what we expect and the generation it belongs to BEFORE
        # reading the clipboard. clipboard.read() is a wl-paste round trip
        # that can take up to SUBPROCESS_TIMEOUT=3s, and _write_clip() can
        # land on the main thread at any point during that window. Holding
        # _echo_lock across the read would block _write_clip() for the
        # whole round trip, so it is released before the read and
        # re-acquired only to compare afterward.
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
        # tick that races _write_clip's wl-copy and sees stale content
        # simply finds nothing changed (not a false "revert") and fires on
        # a LATER tick once wl-copy has settled -- self-healing rather than
        # sending anything wrong. Neither path needs the _last_seen-hash
        # preference the other two call sites use.
        with self._echo_lock:
            expected = self._last_written
            gen = self._write_gen
            last_seen = self._last_seen

        text = self.clipboard.read()
        # The moment of OBSERVATION -- as close to the read as possible --
        # not whenever the rest of this method happens to run afterward.
        # Persisted and sent below, once we know this is a genuine change.
        observed_at = time.time()
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
        # Since this task wraps the observed text in encode_clip_payload
        # before it reaches the wire, the cap must account for the 8-byte
        # timestamp prefix -- text at exactly MAX_PAYLOAD_BYTES would
        # otherwise encode to a frame 8 bytes over it, which the peer's
        # decode_frame rejects as oversized, dropping the whole channel over
        # a single large-but-not-overlong clip.
        if len(text) + TIMESTAMP_BYTES > MAX_PAYLOAD_BYTES:
            log("skipping a clip of %d bytes: over the frame cap" % len(text))
            return
        # Persisted before the send, unconditional on the send's outcome:
        # what we hold and how old it is changed the instant it was
        # observed, regardless of whether the peer ever receives it. A local
        # disk failure here is not the peer's fault and must not prevent the
        # send below.
        try:
            save_clip_state(sha256_hex(text), observed_at, path=self._clip_state_path)
        except (OSError, ClipStateError) as error:
            log("could not persist clip state: %r" % error)
        self.send(TYPE_CLIP, encode_clip_payload(observed_at, text))
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
    """(sha256, ts) last persisted by save_clip_state, or None.

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


# Serializes the whole encode-write-replace below. save_clip_state has three
# call sites, and they run on up to three threads: Agent._write_clip and
# announce_clip_state on run()'s thread, Agent._local_change on the watcher
# threads. All three derive the SAME `target + ".tmp"`, so two concurrent
# savers truncate one another's temp file and whichever os.replace runs second
# finds it already consumed -- observed directly as a FileNotFoundError, and in
# the worse interleaving as a half-written file renamed into place.
#
# That failure is not cosmetic: a temp moved into place mid-write makes
# load_clip_state return None, so the NEXT connection stamps ts=now on old
# content and wins a reconciliation it should lose -- a silent clipboard
# clobber, the exact failure protocol v2 exists to prevent. All three call
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


def save_clip_state(sha256, ts, path=None):
    """Persists (sha256, ts) atomically: encode, write to a `.tmp`
    sibling, then os.replace it over the real path. os.replace is an
    atomic rename on POSIX, so load_clip_state above -- quite possibly
    running in an entirely different process, since the PC agent is a new
    process every connection -- can never observe a half-written file.
    Reuses encode_clip_state rather than a second JSON encoding, so its
    non-finite-ts guard protects this path too, and the on-disk format can
    never drift from the wire format of the same shape.

    Serialized in full under _clip_state_write_lock: the atomic rename alone
    protects readers, not the shared temp file that concurrent WRITERS both
    build. See that lock's own comment.
    """
    target = clip_state_path() if path is None else path
    with _clip_state_write_lock:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        payload = encode_clip_state(sha256, ts)
        tmp = target + ".tmp"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, target)


def resolve_startup_state(current_hash, stored, now):
    """The judgement that makes the wake flow work. `current_hash` is the
    clipboard's hash *right now*, at startup; `stored` is whatever
    load_clip_state() last returned (a (sha256, ts) pair, or None); `now`
    is the caller's clock.

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
    is the real age of that content and is returned unchanged. Returning
    `now` here instead would make every such clip look freshly copied,
    winning it every reconciliation and clobbering the peer systematically
    on every reconnect. If the hashes differ, or nothing was ever stored,
    the content changed (or first appeared) while nothing was watching,
    and only `now` is honest.

    A None current_hash (clipboard empty or unreadable right now) always
    wins over whatever is on disk, regardless of what was previously
    stored: resolve_freshness never compares timestamps when either side's
    hash is None, so the timestamp returned here is never actually read.
    """
    if current_hash is None:
        return None, now
    if stored is not None and stored[0] == current_hash:
        return current_hash, stored[1]
    return current_hash, now


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
    resolveCurrentClipState: `clipboard.read()` returning None or empty is
    never hashed, matching the wire contract that sha256 is null for
    exactly that clipboard state -- see resolve_startup_state for the rule
    this applies once a current hash is in hand.
    """
    text = clipboard.read()
    current_hash = sha256_hex(text) if text else None
    return resolve_startup_state(current_hash, stored, now)


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
    resolved = resolve_current_clip_state(clipboard, load_clip_state(path=path), now)
    try:
        save_clip_state(*resolved, path=path)
    except (OSError, ClipStateError) as error:
        log("could not persist clip state: %r" % error)
    send(TYPE_CLIP_STATE, encode_clip_state(*resolved))


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
    # available() never touches the clipboard, but GPasteWatcher takes one for
    # its safety net, so hand it the same one production would use rather than
    # a None that a future start() call could trip over.
    gpaste = GPasteWatcher(_select_clipboard())
    log("info gpaste: %s" % ("available" if gpaste.available() else "unavailable, will poll"))

    return 0 if ok else 1


import threading

GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
# The BUS name is org.gnome.GPaste; org.gnome.GPaste2 is the INTERFACE name on
# that object. Verified on the target machine: --dest org.gnome.GPaste2 has no
# owner, so probing it would make available() always false and silently leave
# the watcher on the polling fallback forever.
GPASTE_BUS_NAME = "org.gnome.GPaste"

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


class GPasteWatcher:
    """Event-driven. Python has no stdlib DBus binding, so this shells out to
    gdbus monitor and parses its output line by line.

    Composes a PollingWatcher as a slow safety net (see
    SAFETY_NET_POLL_SECONDS): a subscription that has silently stopped
    delivering is indistinguishable from an idle clipboard until something
    else actually looks at the content.

    `clipboard` is only used by that safety net -- available() never touches
    it -- but it is a required argument rather than a defaulted one, because a
    GPasteWatcher built without one would look healthy and silently have no
    safety net at all, which is the exact failure this class exists to catch.
    """

    def __init__(self, clipboard,
                 safety_net_interval_seconds=SAFETY_NET_POLL_SECONDS,
                 degraded_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None):
        self.clipboard = clipboard
        self._process = None
        self._thread = None
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
        self._safety_net = PollingWatcher(
            clipboard,
            degraded_interval_seconds if degraded else safety_net_interval_seconds,
            on_tick=self._observe_tick)

    def _observe_tick(self, previous, current):
        """Judge the event source from one safety-net tick.

        Runs on the safety net's own poll thread, after it has already reported
        any change through on_change -- see PollingWatcher.start for why that
        ordering is load-bearing.
        """
        signals = self._signals
        # A content difference ALONE does not prove the signal path missed it.
        # When GPaste is healthy the user copies something, the signal fires
        # and is handled -- and then this tick also sees content differing from
        # the poll's own baseline, because the poll keeps one. Concluding
        # "dead" from the difference alone would degrade every healthy
        # installation to polling on the user's first copy, which is worse than
        # the bug this safety net exists to fix. The count of accepted signals
        # is the discriminator: the source is dead only if the content moved
        # while no signal arrived to report it.
        #
        # Both reads must also have SUCCEEDED. read() returns None for a failed
        # wl-paste (WaylandClipboard.read keeps a dedicated one-shot log line
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
        missed = (previous is not None and current is not None
                  and previous != current
                  and signals == self._signals_at_last_tick)
        if missed and not self._degraded:
            self._degraded = True
            log("GPaste is not reporting clipboard changes (is the gnome-shell "
                "extension enabled?), polling every %.1fs for the rest of this "
                "connection" % self._degraded_interval)
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
            text=True, env=clipboard_env(),
        )

        def pump():
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    # Counted BEFORE dispatching, deliberately: on_change is
                    # Agent._local_change, whose wl-paste round trip can take
                    # up to SUBPROCESS_TIMEOUT=3s. Counting afterwards would
                    # let a safety-net tick landing inside that window see an
                    # unmoved counter and declare a perfectly healthy source
                    # dead.
                    self._signals += 1
                    on_change()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()
        # The SAME on_change object the signal path just got -- in production
        # Agent._local_change -- so a change only the safety net catches goes
        # through one observation, one one-shot echo suppression and one
        # _last_seen. A parallel path here would be a second copy of echo
        # logic this project has already fixed two races in.
        #
        # Its cost, deliberately accepted: _local_change stamps the clip at
        # the moment IT observes the change, so a clip caught only by the
        # safety net carries a timestamp up to one safety-net interval late
        # (30 seconds with the production default).
        self._safety_net.start(on_change)

    def stop(self):
        """Session teardown (Agent.clipboard_lost), NOT the degrade path.

        Terminating the gdbus child is correct here -- the Wayland session it
        was watching is gone -- and deliberately absent from _observe_tick's
        switch, where the subscription is left alive to recover on its own.
        """
        self._stop.set()
        self._safety_net.stop()
        if self._process:
            self._process.terminate()


class PollingWatcher:
    """Degraded mode: forks wl-paste and reads the whole clipboard each time,
    so it runs at a slower interval than the Mac's in-process poll.

    `interval` is re-read on every iteration, so a caller can change the poll
    rate mid-flight -- GPasteWatcher does exactly that when its safety net
    concludes the event source is dead -- without stopping and restarting the
    loop.

    `on_tick` is an optional pure observer, invoked with (previous, current)
    after EVERY tick, change or not. It exists so this stays the file's only
    content-comparison-and-notify loop, with a single owner of `previous`: the
    safety net judges the event source from these observations rather than
    running a second comparison of its own.
    """

    def __init__(self, clipboard, interval_seconds, on_tick=None):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._on_tick = on_tick
        self._stop = threading.Event()
        self._thread = None

    def available(self):
        return True

    def start(self, on_change):
        def pump():
            previous = self.clipboard.read()
            while not self._stop.wait(self.interval):
                current = self.clipboard.read()
                before = previous
                if current != previous:
                    previous = current
                    on_change()
                # AFTER on_change, and load-bearing rather than tidy:
                # on_change is Agent._local_change, whose own clipboard round
                # trip (up to SUBPROCESS_TIMEOUT=3s) and send are the grace
                # period the event source gets to deliver the signal for this
                # very change before an observer judges it missing. Hoisting
                # this above on_change looks like a harmless cleanup and
                # silently reopens that window.
                #
                # `before` is passed rather than a bare `changed` flag so the
                # observer can tell a real change from a failed read and its
                # recovery -- both are `!=` here, and neither involves a
                # selection change at all.
                if self._on_tick is not None:
                    self._on_tick(before, current)

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def make_watcher(clipboard, fallback_interval_seconds=DEGRADED_POLL_SECONDS,
                 degraded=False, on_degrade=None):
    """`fallback_interval_seconds` is the ONE knob for "how fast we poll when
    signals cannot be relied on", and there are two ways to arrive there:
    GPaste was never available, or its event source was diagnosed silent. It
    therefore reaches both branches below -- as the GPaste watcher's degraded
    rate and as the plain poller's interval -- so tuning it moves both and
    neither can quietly keep a hardcoded rate the log line then misquotes.

    `degraded`/`on_degrade` carry the dead-event-source verdict in and out, so
    it belongs to the connection rather than to whichever watcher reached it --
    see Agent._event_source_degraded."""
    watcher = GPasteWatcher(clipboard, degraded_interval_seconds=fallback_interval_seconds,
                            degraded=degraded, on_degrade=on_degrade)
    if watcher.available():
        if degraded:
            log("watching the clipboard through GPaste, already diagnosed as silent "
                "this connection, so polling every %.1fs" % fallback_interval_seconds)
        else:
            log("watching the clipboard through GPaste, with a safety-net poll every %.0fs"
                % SAFETY_NET_POLL_SECONDS)
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
