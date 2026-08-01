// Sources/clipwire/main.swift
import CryptoKit
import Foundation

// Namespaced rather than bare top-level `let`: main.swift is the file
// Swift treats as the literal entry point, so top-level statements here
// are correct and required (see the bottom of this file) -- but a bare
// top-level `let` is still a script-local binding, not a genuine shared
// constant, and every other constant in this target (FrameConstants,
// StatusConstants, ChannelConstants) is namespaced the same way for
// consistency and so nothing about its initialization depends on which
// file happens to be main.
enum AgentPaths {
    static let statusURL = URL(fileURLWithPath: expandTilde("~/.local/state/clipwire/status.json"))
    static let logPath = "~/.local/state/clipwire/clipwire.log"
}

/// Lowercase hex, no separators -- the exact shape `hashlib.sha256(data).hexdigest()`
/// produces on the Python side, and the shape every `ClipState.sha256` on the wire
/// must match byte-for-byte: `resolveStartupState`/`resolveFreshness` compare these
/// strings with plain `==`, so a case or separator difference here would make every
/// startup comparison see "hashes differ" and take the `now` branch meant only for
/// content that genuinely changed while nothing was watching -- the systematic
/// clobber the persistent store exists to prevent. `EchoGuard` already computes a
/// `SHA256.Digest` elsewhere in this target and never hexes it, so there is no
/// existing conversion to reuse here; this is the first, and is pinned against a
/// known vector in `Tests/clipwireTests/HandleFrameTests.swift`.
func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

/// Reconciles what the pasteboard holds right now against what was last persisted.
/// `nil` (an empty or unreadable pasteboard, or one holding content over its
/// kind's limit -- see below) is never hashed, matching the wire contract that
/// `sha256` is `null` for exactly those cases -- see `resolveStartupState` for
/// the rule this applies once a current hash is in hand.
///
/// `currentKind` -- the OTHER half of `resolveStartupState`'s signature -- comes from
/// this exact same `read()` call, never derived separately: `read()` is Task 8's one
/// canonical read, returning `(kind:, data:)` or `nil`, so the kind of what was just
/// hashed is sitting right there in the pair already. This is the one place in the
/// file that turns a raw `pasteboard.read()` into a (hash, kind) pair; every caller of
/// `resolveStartupState` goes through here rather than reading the pasteboard and
/// deciding a kind independently, which is what keeps a hash and a kind from ever
/// being paired up wrong. Mirrors `resolve_current_clip_state` on the PC side.
///
/// Content over its kind's limit resolves a NIL hash, with a log line -- the
/// third pasteboard state, alongside "empty" and "unreadable", that has no
/// announceable hash. Without it the announce path was the one path here with
/// no size guard at all, and the omission was not merely untidy:
/// `PasteboardWatcher.pollLocked` skips an oversized body and returns BEFORE
/// `persistClipState`, so the store keeps its older entry, this function
/// hashed the oversized body anyway, `resolveStartupState` saw a hash
/// differing from the store and stamped `now`, and `announceClipState`
/// persisted and announced it. That announcement beats anything the peer
/// copied earlier -- and then `.sendMine` refuses to send it, correctly, at
/// its own size guard. The peer has by then resolved `waitForPeer` and
/// suppressed its own push, so its perfectly sendable clip never arrives: the
/// wake flow protocol v2 exists to serve, broken by content that cannot
/// travel. A nil hash makes the peer win and deliver, which is the outcome
/// `resolveFreshness` already gives it for free.
///
/// The two predicates are the SENDERS' own, character for character
/// (`PasteboardWatcher.pollLocked`'s and `.sendMine`'s), so "announceable"
/// and "sendable" cannot drift apart. That includes their asymmetry:
/// `maxImageBytes` bounds the IMAGE, so an image at exactly the limit is
/// legal here and at every send site, while `maxTextBytes` bounds the encoded
/// text clip and so must leave room for its 8-byte timestamp.
///
/// The verdict clauses (`over the image limit` / `over the text limit`) are
/// the ones every other size-limit site already reports, byte for byte; the
/// sentences differ because the EVENT differs -- nothing is being skipped on
/// its way to the wire here, there is simply nothing to announce.
///
/// `log` is non-optional in the sense that matters: it has no default, so
/// every call site has to decide, the same discipline `resolveStartupState`
/// applies to `currentKind`. `nil` is legal and is what tests that assert
/// only on the resolved state pass; both production callers pass the real
/// one.
func resolveCurrentClipState(pasteboard: PasteboardReading, stored: ClipState?, now: Double,
                             log: Log?) -> ClipState {
    var currentHash: String? = nil
    var currentKind: ClipKind? = nil
    if let read = pasteboard.read(), !read.data.isEmpty {
        let size = read.data.count
        let oversized: Bool
        switch read.kind {
        case .text:
            oversized = size + ClipPayloadConstants.timestampBytes > FrameConstants.maxTextBytes
            if oversized {
                log?.line("not announcing a clip of \(size) bytes: over the text limit")
            }
        case .image:
            oversized = size > FrameConstants.maxImageBytes
            if oversized {
                log?.line("not announcing an image of \(size) bytes: over the image limit")
            }
        }
        if !oversized {
            currentHash = sha256Hex(read.data)
            currentKind = read.kind
        }
    }
    return resolveStartupState(currentHash: currentHash, currentKind: currentKind,
                               stored: stored, now: now)
}

/// Whether THIS connection's one-shot clip-state announcement has already gone out.
/// `handleFrame` checks and claims it via `markSent()`, on a matched hello only;
/// `wireAgent` calls `reset()` on `.clipboardPending`, the signal `Channel` raises
/// exactly once per established connection attempt (see `Channel.attempt()`'s
/// `established` guard) -- so the announcement re-arms on every reconnect, not only
/// the process's first connection ever. That re-arming is the entire point: Mac
/// sleep/wake cycles reconnect constantly, and each one needs its own reconciliation,
/// which is exactly what v1 never did.
///
/// Holds no lock, unlike `AgentStatus`: this is only ever touched from
/// `channel.onFrame`/`channel.onStateChange`, both invoked synchronously from
/// whichever single thread drives `Channel.run()`'s decode loop -- never from
/// `PasteboardWatcher`'s timer or the heartbeat timer, which is what forces a lock
/// onto `AgentStatus`.
final class ClipStateAnnouncement {
    private(set) var sent = false

    /// What THIS connection actually put on the wire. Kept because the store
    /// is not a reliable way to read it back: `announceClipState`'s save is
    /// best-effort and, like every save site, its failure is only logged.
    /// See the `.clipState` case for what depends on it.
    ///
    /// Cleared by `reset()` along with `sent`, so a value announced on one
    /// connection can never be resolved against on the next -- by then it
    /// describes an older reading of the pasteboard than the reconciliation
    /// that connection is about to perform for itself.
    private(set) var announced: ClipState?

    func reset() {
        sent = false
        announced = nil
    }

    /// Records what `announceClipState` resolved and sent. Separate from
    /// `markSent()` because that call has to happen BEFORE the announcement
    /// is built (it is what claims the one-shot), and the value only exists
    /// afterwards.
    func record(announced state: ClipState) {
        announced = state
    }

    /// Marks it sent and returns whether THIS call is the one that did so --
    /// `false` if an earlier call already claimed it this connection.
    @discardableResult
    func markSent() -> Bool {
        guard !sent else { return false }
        sent = true
        return true
    }
}

/// Saves, and logs rather than swallowing if it cannot. Every one of this
/// file's `clipStateStore.save` calls goes through here -- four of them now:
/// `announceClipState`, `handleLocalChange`, and `handleFrame`'s `.clip` and
/// `.imageClip` cases. Counting them here rather than naming a number alone,
/// since the number has already changed once.
///
/// The line matches the text every one of `agent/clipwire-agent.py`'s own
/// `save_clip_state` call sites already logs (`could not persist clip state:
/// %r`), the way the two "over the text limit" lines and the two skew lines
/// already match. The Swift sites were all bare `try?` before this was
/// introduced, which mattered specifically because the silent side is the one
/// whose disk failure is the PRECONDITION for a store-goes-stale clobber:
/// with nothing readable on disk, the next reconciliation re-derives an age
/// from `now` and wins a comparison it should have lost, which is the failure
/// the persistent store exists to prevent.
///
/// One function rather than a copy of the same `do/catch` per site:
/// identical literals repeated in one file is exactly the drift this project
/// has already been bitten by, and `ClipStateStore.save` deliberately throws
/// so that a CALLER can log -- it just should not be four callers writing the
/// string out independently.
func persistClipState(_ state: ClipState, to store: ClipStateStore, log: Log) {
    do {
        try store.save(state)
    } catch {
        log.line("could not persist clip state: \(error)")
    }
}

/// Builds and sends this side's one-shot clip-state announcement: reconciles
/// whatever the pasteboard currently holds against the persistent store (so
/// unchanged content keeps its true recorded age instead of looking freshly
/// copied -- see `resolveStartupState`), persists the reconciled value, and sends
/// it. The send is unconditional on the save's success -- a local disk failure is
/// not the peer's fault, and must not silently disable reconciliation for this
/// connection the way gating the send behind the save's result would.
///
/// Pulled out of `wireAgent` for the same reason `handleFrame` was pulled out of
/// `runAgent()`: a `send` spy can verify the exact frame this produces without a
/// live channel. See `Tests/clipwireTests/HandleFrameTests.swift`.
@discardableResult
func announceClipState(
    send: (Frame) -> Void,
    pasteboard: PasteboardReading,
    clipStateStore: ClipStateStore,
    log: Log,
    now: Double
) -> ClipState? {
    let stored = clipStateStore.load()
    let resolved = resolveCurrentClipState(pasteboard: pasteboard, stored: stored, now: now, log: log)
    // The line the design spec mandates by name for exactly this branch --
    // startup reconciliation finding that the content no longer matches what
    // was last recorded, so only `now` is honest about its age. Two things
    // rest on it: the acceptance checklist requires a divergence to be
    // visible in the log, and the design's one accepted trade-off (with both
    // clipboards changed while apart, the side whose agent was born more
    // recently wins) is justified on the grounds of being "visible in the log
    // rather than mysterious" -- which is only true if this line exists.
    //
    // Exactly the negation of `resolveStartupState`'s "the stored timestamp
    // is authoritative" condition, nothing-ever-stored included: content that
    // APPEARED while nothing was watching is the same judgement as content
    // that changed. A nil hash is deliberately silent -- a pasteboard that
    // is empty, unreadable, or holding content over its kind's limit (all
    // three resolve one, and the last says so in its own line) never reaches
    // a timestamp comparison at all, so there is no reconciliation
    // judgement to report.
    //
    // Logged here rather than inside `resolveCurrentClipState`, which the
    // `.clipState` case's own store-failure fallback also calls with
    // `stored: nil`: that call would then claim "changed while apart" on
    // every clip-state frame arriving while the store is unreadable, when
    // nothing changed at all. The spec ties this line to startup
    // reconciliation, which is this function. Byte-identical to
    // `announce_clip_state`'s own line on the PC side.
    if let hash = resolved.sha256, stored?.sha256 != hash {
        log.line("clipboard changed while apart")
    }
    persistClipState(resolved, to: clipStateStore, log: log)
    // Returns what actually reached the wire, and only that: a payload that
    // failed to encode was never announced, so there is nothing for a later
    // reconciliation to be consistent WITH.
    guard let payload = try? resolved.encodePayload() else { return nil }
    send(Frame(type: .clipState, payload: payload))
    return resolved
}

/// The one place a clip's kind becomes a frame: `.text` goes out as a
/// `.clip` frame carrying a `ClipPayload`, `.image` as an `.imageClip` frame
/// carrying an `ImagePayload`.
///
/// One function rather than the mapping written out at each of its two call
/// sites (`handleLocalChange` below, and `handleFrame`'s `.sendMine` branch).
/// Two inline copies is how the second one came to send text only: this file
/// carried the text codec in both places, so `.sendMine` had to decline an
/// image it had otherwise verified. Two copies of one rule in one file is the
/// same drift the two "over the text limit" lines and the three-word
/// reconciliation vocabulary already guard against across the two languages.
///
/// Throws only what `ImagePayload.encode` throws -- a non-finite `ts`, which
/// it rejects on encode as well as decode. No caller can currently supply
/// one: `handleLocalChange`'s comes from `Date().timeIntervalSince1970`, and
/// `.sendMine`'s from a `ClipState` that was JSON-decoded (a format with no
/// literal for NaN or infinity) or resolved from the same clock. Left
/// throwing rather than made unfailable anyway, so the finiteness rule keeps
/// living in one place -- the codec -- for both directions.
func outgoingClipFrame(kind: ClipKind, body: Data, ts: Double) throws -> Frame {
    switch kind {
    case .text:
        // `String(decoding:as:UTF8.self)` is lossy for bytes that are not
        // valid UTF-8, which is why `SystemPasteboard`'s text read goes
        // through `string(forType:)` rather than `data(forType:)` -- see
        // `PasteboardBackend`'s doc comment. By the time a body reaches this
        // function under `.text` it has already come back from that read, so
        // there is nothing here for the substitution to damage.
        return Frame(type: .clip,
                     payload: ClipPayload(ts: ts, text: String(decoding: body, as: UTF8.self)).encode())
    case .image:
        return Frame(type: .imageClip, payload: try ImagePayload.encode(ts: ts, png: body))
    }
}

/// Everything a clipboard change this agent OBSERVED locally does: record
/// what we now hold and how old it is, then put it on the wire.
///
/// Pulled out of `wireAgent`'s `watcher.onChange` closure for the same
/// reason `handleFrame` and `announceClipState` were pulled out of
/// `runAgent()`, and it matters more here than it looks: `channel.send` has
/// no test-observable hook whatsoever -- `Channel` is `final`, its
/// `stdinPipe` is private and assigned only inside `attempt()`, so a send
/// with no live ssh process reports `onSent(false)` and leaves nothing
/// behind. While this logic lived inside that closure, the entire outbound
/// path could have been wired to the text codec for both kinds with no test
/// in either suite able to see it. A `send` spy can now verify the exact
/// frame; see `Tests/clipwireTests/HandleFrameTests.swift`.
///
/// `send` is the one-argument shape rather than `Channel.send(_:onSent:)`:
/// the `onSent` callback exists to keep `clipwire status` from reporting a
/// clip that never left the machine, which is `wireAgent`'s business and not
/// this function's. `wireAgent` closes over it.
///
/// No size guard here, deliberately: `PasteboardWatcher.pollLocked` applies
/// the per-kind limit at the moment of observation, where the skip can be
/// logged next to the read that produced it. The `.sendMine` branch needs
/// its own copy because it reads the pasteboard independently; this path
/// does not.
func handleLocalChange(
    kind: ClipKind,
    body: Data,
    observedAt: Double,
    send: (Frame) -> Void,
    clipStateStore: ClipStateStore,
    log: Log
) {
    // `observedAt` -- the moment `PasteboardWatcher` actually read it, not
    // whenever this runs -- is the timestamp both for what we persist and for
    // what we send. The save happens regardless of whether the send below
    // ever reaches the peer (no channel yet, or the write fails): the store's
    // job is "what do we hold and how old is it", independent of delivery.
    //
    // `kind` is the one the watcher observed, threaded through from the same
    // `pasteboard.read()` pair the body came from. A hardcoded `.text` here
    // would announce a PNG's digest as text on the next reconnect, and the
    // peer believes it -- `decode_clip_state` accepts both kinds, so nothing
    // rejects it on arrival.
    persistClipState(ClipState(sha256: sha256Hex(body), ts: observedAt, kind: kind),
                     to: clipStateStore, log: log)
    let frame: Frame
    do {
        frame = try outgoingClipFrame(kind: kind, body: body, ts: observedAt)
    } catch {
        // Logged rather than swallowed by `try?`. A `try?` here is the exact
        // shape of the v2 defect this branch's whole design reacts to: a
        // clip that cannot be encoded is not sent, which is right, but a
        // silent drop is how a user concludes the tool is broken with
        // nothing anywhere to look at. The line is shared byte-for-byte with
        // the `.sendMine` branch's own catch -- one sentence per condition,
        // two sites.
        log.line("could not encode a clip for the peer: \(error)")
        return
    }
    send(frame)
}

/// Handles one decoded frame. Pulled out of `runAgent()`'s inline closure
/// so the two contracts this task exists for are directly assertable: the
/// echo suppression must be armed strictly before the incoming clip is
/// written to the pasteboard, and a received hello -- matched or not --
/// must always produce a reply. Neither was testable when this lived as a
/// closure inside a function that blocks forever and wrote straight to
/// `NSPasteboard.general`; a later refactor could have swapped the
/// arm/write order, or dropped the reply on a mismatch, and all existing
/// tests would still have passed.
///
/// `send`/`noteWrittenLocally` are the two bound methods (`channel.send`,
/// `watcher.noteWrittenLocally`) rather than the concrete `Channel`/
/// `PasteboardWatcher` types themselves: both types are `final` classes
/// with no test-observable hook into these specific calls (`Channel.send`
/// only does anything once a real ssh pipe exists; nothing on either type
/// records that it was invoked), so a test cannot substitute a spy for
/// them directly. Passing the one function each call site actually needs
/// makes both substitutable with a plain closure in a test, with no new
/// protocol required for either. `pasteboard: PasteboardWriting` is that
/// one new protocol, for the write side, per Pasteboard.swift.
///
/// See `Tests/clipwireTests/HandleFrameTests.swift` for the ordering
/// assertions this exists to make possible.
func handleFrame(
    _ frame: Frame,
    send: (Frame) -> Void,
    noteWrittenLocally: (ClipKind, Data) -> Void,
    pasteboard: PasteboardReading & PasteboardWriting,
    status: AgentStatus,
    log: Log,
    clipStateStore: ClipStateStore,
    clipStateAnnouncement: ClipStateAnnouncement,
    now: Double = Date().timeIntervalSince1970
) {
    switch frame.type {
    case .hello:
        // Reply unconditionally, matched or not. `Channel` exposes no
        // way to force-close the ssh process from here, so the peer
        // noticing the SAME mismatch on its own side (it validates
        // whatever hello it receives from us, exactly as we validate
        // whatever it sends) and exiting is the only mechanism that
        // actually drops the channel; withholding our reply on a
        // mismatch would just leave it open with nothing to trigger
        // the "channel closed" a user would expect. This also replaces
        // the brief's one-shot `channel.send(hello)` called before
        // `channel.run()` even starts: `Channel.send` looks up
        // `stdinPipe` only when its queued write actually runs, and
        // `stdinPipe` is set only inside `Channel.attempt`, which does
        // not exist yet at that point in the brief's control flow --
        // so that send is either silently dropped or wins a race that
        // depends on GCD scheduling, and either way never fires again
        // on a later reconnect. Sending here, in reaction to every
        // received hello, fires exactly once per connection attempt,
        // on every attempt, because `onFrame` is only ever invoked
        // from inside `attempt()`'s read loop, which always runs after
        // `stdinPipe` has already been assigned.
        send(Frame(type: .hello, payload: ProtocolConstants.helloPayload))
        guard let peer = decodeHello(frame.payload) else {
            let reason = "malformed hello from peer — run `clipwire install`"
            status.recordProtocolMismatch(reason)
            log.line(reason)
            return
        }
        guard peer.version == ProtocolConstants.version else {
            let reason = "protocol mismatch: peer speaks \(peer.version), we speak "
                + "\(ProtocolConstants.version) — run `clipwire install`"
            status.recordProtocolMismatch(reason)
            log.line(reason)
            return
        }
        status.recordHelloMatched()
        log.line("peer said hello (agent \(peer.agent ?? "unknown"))")
        // Matched peers only: a clock reading from one we cannot talk to is
        // noise next to the mismatch itself.
        if let skew = skewLogLine(peerSentAt: peer.sentAt, now: now) {
            log.line(skew)
        }
        // Sent exactly once per connection, immediately after a matched
        // hello -- per the design spec. `clipStateAnnouncement` is what
        // makes "once" true, not the assumption that hello itself only
        // ever arrives once: `wireAgent` resets it on the next
        // `.clipboardPending`, so it re-arms on every reconnect.
        if clipStateAnnouncement.markSent() {
            if let announced = announceClipState(send: send, pasteboard: pasteboard,
                                                 clipStateStore: clipStateStore, log: log, now: now) {
                clipStateAnnouncement.record(announced: announced)
            }
        }
    case .clipState:
        // Logged rather than dropped in silence, exactly as the `.clip`
        // case below now does. The DROP itself stays, and so does the
        // divergence it embodies: `Channel` exposes no way to force-close
        // the ssh process from here, which is why the PC agent's
        // `_on_clip_state` deliberately raises instead and lets the
        // connection go (its own docstring in `agent/clipwire-agent.py`
        // spells that out). What must not stay is the silence. A frame
        // whose `sha256` is well-formed JSON but not 64 lowercase hex is
        // rejected here, which also skips `recordPeerClipboardReady()`
        // below -- so on a real codec desync the PC tears the channel down
        // WITH a line while this side sits at `clipboard-pending` for the
        // rest of the connection having written nothing anywhere. Same
        // "failures are visible" principle the `.clip` line rests on; the
        // wording follows `could not decode a clip from the peer` and
        // `could not persist clip state`, its two nearest siblings.
        let peerState: ClipState
        do {
            peerState = try ClipState.decodePayload(frame.payload)
        } catch {
            log.line("could not decode a clip state from the peer: \(error)")
            return
        }
        // After the decode guard, not before it: a frame we cannot read
        // proves nothing about the peer's clipboard. This is the only frame
        // that proves the channel can actually sync -- see
        // `recordPeerClipboardReady` -- and it is reported regardless of
        // which way the reconciliation below then goes, since who wins says
        // nothing about whether the channel is healthy.
        status.recordPeerClipboardReady()
        // `clipStateStore.load()` should already reflect our own current
        // state -- either from this connection's own announcement above,
        // or from an ordinary local-change/applied-clip save since -- so
        // the fallback below only matters if an earlier save failed. It
        // must still resolve a REAL state from the live pasteboard rather
        // than a bare nil-hash placeholder: a wrong nil here would make
        // both sides resolve waitForPeer against each other's (correctly
        // announced) state and silently lose the clip, reintroducing v1's
        // bug through the fallback path instead of the main one.
        // `load()` first, then what THIS connection announced, and only
        // then a fresh re-derivation.
        //
        // The store should already be current -- from this connection's own
        // announcement, or an ordinary local-change/applied-clip save since --
        // so the rest only matters once a save has failed, which every save
        // site merely logs. The old code went straight to the
        // re-derivation there, and that is a clobber, not a fallback: it
        // stamps `now` on content whose age this connection already ANNOUNCED
        // to this same peer, so a peer that is genuinely fresher than what we
        // put on the wire still loses to a number nobody was told about. The
        // announced pair is the only value consistent with the announcement
        // the peer is answering.
        //
        // `load()` still outranks it, unlike the PC agent, which prefers its
        // just-computed pair outright -- and the asymmetry is deliberate, not
        // drift. There, `_resolve_clip_state` is called from
        // `clipboard_became_ready`'s own call frame, one line after computing
        // the pair, so nothing can have happened in between. Here the
        // `.clipState` frame arrives arbitrarily later than the `.hello` that
        // announced, and a local change may legitimately have moved the store
        // on since; the freshest readable value wins.
        //
        // The re-derivation survives as the last resort for the case neither
        // covers: a clip-state arriving before we ever announced (this side
        // does not stash, unlike the PC). A nil-hash placeholder there would
        // make both sides resolve waitForPeer against each other's correctly
        // announced state and silently lose the clip -- v1's bug through the
        // fallback path.
        //
        // Accepted trade-off, stated rather than left to be discovered: if a
        // local change ALSO failed to save after the announcement, the
        // announced pair now describes older content than the pasteboard
        // holds, and a `.sendMine` below would send the current text under the
        // announced `ts` -- underselling its age. That needs two independent
        // save failures plus a concurrent announcement, where the previous
        // behaviour needed only one, and the local change in question was
        // already sent to the peer by the watcher's own path.
        let mine = clipStateStore.load()
            ?? clipStateAnnouncement.announced
            ?? resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: now, log: log)
        let decision = resolveFreshness(mine: mine, peer: peerState)
        // Every reconciliation outcome is reported, not only the interesting
        // ones. Acceptance item 2 requires the conflict to appear in the log,
        // and the design's accepted trade-off -- with both clipboards changed
        // while apart, the more recently born agent wins -- is only tolerable
        // because it is visible here rather than mysterious.
        //
        // The decision word is the shared vocabulary: `FreshnessDecision`'s
        // raw values are the same three strings the PC agent's SEND_MINE /
        // WAIT_FOR_PEER / DO_NOTHING constants hold, so the two sides' lines
        // are byte-identical with no formatting bridge -- the convention the
        // frame-cap and skew lines already follow, which has caught drift
        // twice. In production both sides' lines land in the SAME file:
        // `Channel.attempt` pipes the agent's stderr into this log with a
        // `remote: ` prefix, so one file shows the conflict and its winner.
        //
        // Task 14: the decision word alone is not enough. "reconciled with
        // the peer: sendMine" with the two sides holding different kinds is
        // undiagnosable after the fact -- "why did a picture overwrite my
        // text" has no answer in the line above this comment. `?? "none"`
        // only ever fires on a genuine `nil` kind: `ClipKind` has exactly
        // two cases and neither raw-values to an empty string, so there is
        // no real kind this could be mistaken for. Byte-identical to the PC
        // agent's own suffix, the same convention the decision word itself
        // already follows.
        log.line("reconciled with the peer: \(decision.rawValue) "
            + "(mine=\(mine.kind?.rawValue ?? "none") peer=\(peerState.kind?.rawValue ?? "none"))")
        switch decision {
        case .sendMine:
            // Verify before sending: read the pasteboard, hash what came
            // back, and require it to match `mine` on BOTH halves -- the
            // kind it records and the hash it records -- before a single
            // byte of it goes out. `mine` is an ANNOUNCEMENT, made at some
            // earlier moment; the pasteboard is free to have moved on since,
            // and this branch is the one place that sends content it did not
            // itself observe changing. Without the check it sends whatever
            // it happens to find under the announced timestamp: the wrong
            // kind, or the right kind at a stale age. Either is a clobber the
            // receiver cannot detect, because everything it can see about the
            // frame is well-formed and consistent -- the PC agent applies any
            // incoming clip unconditionally, exactly as this side does.
            //
            // A mismatch sends NOTHING, and that is the whole remedy: the
            // pasteboard changed, so `PasteboardWatcher` has either already
            // carried the new content or is about to, and this frame's job --
            // telling a peer about content it lacks -- is being done
            // correctly by someone else. A `nil` read counts as a mismatch
            // rather than as a special case: an emptied pasteboard genuinely
            // no longer holds what we announced.
            //
            // That promise is unqualified HERE and deliberately qualified in
            // the PC agent's twin, which is not drift. There, a read can race
            // `wl-copy`'s detached, asynchronous handoff and disagree with a
            // pasteboard that did not actually change -- and the watcher then
            // carries nothing, because the eventual GPaste `Update` for our
            // own write is (correctly) suppressed as an echo. `NSPasteboard`
            // writes are synchronous, so no such window exists on this side:
            // a mismatch here means the pasteboard really did change, and a
            // real change is exactly what `PasteboardWatcher` reports.
            //
            // The line is byte-identical to the PC agent's own in
            // `_resolve_clip_state`, the convention the frame-cap and skew
            // lines already follow: no interpolated values, so the two cannot
            // drift apart in formatting. In production both land in the same
            // file -- `Channel.attempt` pipes the agent's stderr into this
            // log with a `remote: ` prefix.
            guard let read = pasteboard.read(),
                  read.kind == mine.kind,
                  sha256Hex(read.data) == mine.sha256 else {
                log.line("clipboard changed before the send")
                return
            }
            // Either kind, since Task 13. Task 8 had already made `sendMine`
            // REACHABLE for an image -- an image-only pasteboard used to read
            // back as nothing, so it resolved a `nil` hash and could never
            // win a reconciliation; it now resolves a real
            // `(hash, ts, .image)` state -- but the only frame this branch
            // could build was a `ClipPayload`, the TEXT codec, so a verified
            // image had to fall out silently rather than reach the wire as a
            // mojibake transliteration of a PNG. `outgoingClipFrame` picks
            // the codec from the kind now, and the verification above is
            // exactly what licenses trusting that kind: it proved the live
            // pasteboard agrees with `mine.kind` as well as with
            // `mine.sha256`. The PC agent's `_resolve_clip_state` is the
            // worked example, from Task 12.
            //
            // The empty guard is defensive rather than reachable:
            // `resolveCurrentClipState` records a nil hash for an empty
            // pasteboard, and `SystemPasteboard.read()` reports nothing for
            // an empty body, so `mine` could only carry the empty string's
            // digest if something else wrote the store. Refused anyway --
            // neither codec has anything to say about zero bytes. The PC's
            // twin carries the identical note.
            guard !read.data.isEmpty else { return }
            let body = read.data
            // Both bounds match `PasteboardWatcher.pollLocked`'s own
            // send-side guards (Pasteboard.swift): this branch reads the live
            // pasteboard independently, so the limits have to be applied
            // again here rather than inherited from an observation that never
            // happened. Logged (unlike a merely-empty pasteboard, which is
            // not a skip at all) so a user whose large content never syncs
            // has something to look at.
            switch read.kind {
            case .text:
                // Content at or beyond the TEXT limit would build a
                // `ClipPayload` whose encoded frame exceeds
                // `FrameConstants.maxTextBytes`. That is the text-content
                // limit, not the (larger) `FrameConstants.maxPayloadBytes`
                // wire cap `Frame.decode` enforces -- since Task 4 the two
                // are separate, and a send this size would still fit inside
                // the frame cap; it is refused here purely as a matter of the
                // policy text clips are held to.
                guard body.count + ClipPayloadConstants.timestampBytes <= FrameConstants.maxTextBytes else {
                    log.line("skipping a clip of \(body.count) bytes: over the text limit")
                    return
                }
            case .image:
                // The bare body, unlike the text branch above, and the
                // difference is load-bearing: `maxImageBytes` bounds the
                // image, so an image at exactly the limit is legal and
                // encodes to a payload eight bytes over it -- 4,194,312,
                // which still fits `maxPayloadBytes` (8,388,608) with 4 MiB
                // to spare. Written in the text guard's shape it would refuse
                // exactly the maximum-size screenshot the three separated
                // caps exist to permit. Same verdict clause as
                // `PasteboardWatcher.pollLocked`'s, which enumerates all five
                // sites that report this limit and why the shared thing is
                // the clause rather than the whole sentence.
                guard body.count <= FrameConstants.maxImageBytes else {
                    log.line("skipping an image of \(body.count) bytes: over the image limit")
                    return
                }
            }
            do {
                // `mine.ts`, not `now`: the content has not changed, only
                // been re-announced, so its recorded age must be preserved.
                // Sending with `now` would perpetually refresh it and let it
                // win every future reconciliation regardless of what happens
                // next. Carrying the announced timestamp rather than
                // re-deriving one is the property Task 11's verification
                // above exists to make safe.
                //
                // No `EchoGuard` arm to go with this send, unlike the PC
                // agent's twin, which updates `_last_seen` here. Not drift:
                // that field exists because the GPaste watcher fires on
                // non-changes (a history deletion emits Update too), so a
                // later spurious signal could see the clipboard still holding
                // `body` and resend it. `PasteboardWatcher` is
                // `changeCount`-driven and cannot fire without an actual
                // change, and `EchoGuard` is a ONE-SHOT consumed by the next
                // observation -- arming it here would spend it on whatever
                // the user copies next, swallowing a genuine change.
                send(try outgoingClipFrame(kind: read.kind, body: body, ts: mine.ts))
            } catch {
                // Logged rather than swallowed by `try?`, and byte-identical
                // to `handleLocalChange`'s own catch -- one sentence per
                // condition, two sites.
                log.line("could not encode a clip for the peer: \(error)")
            }
        case .waitForPeer, .doNothing:
            // Hashes equal means we agree -- not a signal to resend. A
            // peer that is fresher means we wait. Conflating either with
            // sendMine reintroduces a clobber or a ping-pong.
            break
        }
    case .clip:
        // Logged rather than swallowed by `try?`. The drop itself is right --
        // there is nothing valid to apply -- but doing it invisibly is what
        // makes it permanent: `wl-paste` hands the PC agent RAW BYTES, which
        // `_local_change` hashes and sends unchanged, so a clip whose bytes
        // are not valid UTF-8 fails here, is not applied, and is not stored.
        // The two persistent stores then disagree forever, and on every
        // subsequent reconnect the PC resolves SEND_MINE (its ts is the newer
        // one), re-sends the same bytes, and this side discards them again --
        // with nothing logged on either machine, ever. One line is what turns
        // a permanent silent failure into something a user can find.
        //
        // Empty text stays silent by contrast: it decoded fine, and applying
        // nothing is the correct uneventful outcome, matching the PC agent's
        // own `_write_clip`, which returns quietly for exactly that input.
        let decoded: ClipPayload
        do {
            decoded = try ClipPayload.decode(frame.payload)
        } catch {
            log.line("could not decode a clip from the peer: \(error)")
            return
        }
        guard !decoded.text.isEmpty else { return }
        let textData = Data(decoded.text.utf8)
        // Arm suppression BEFORE writing to the pasteboard, with the
        // PLAIN TEXT bytes -- not `frame.payload`, which carries the
        // 8-byte timestamp prefix. PasteboardWatcher's own poll() hashes
        // the body `pasteboard.read()` returns, which is this text
        // alone; arming with the ts-prefixed payload would make
        // EchoGuard's digest never match, `shouldSend` would always
        // return true, and every applied remote clip would bounce
        // straight back out to the peer it came from. PasteboardWatcher's
        // lock keeps its own bookkeeping consistent, but it does not own
        // this write, so only this ordering keeps the watcher from
        // observing our write before the suppression exists.
        //
        // `.text`, and the same bytes that were armed: this case decoded a
        // `.clip` (type 0x01) payload, the text-clip codec, so there is
        // nothing else it could be. An image applied from the peer arrives
        // as `.imageClip` and does not reach this branch.
        noteWrittenLocally(.text, textData)
        pasteboard.write(kind: .text, data: textData)
        // The peer's timestamp, never `now`: this is the entire reason it
        // travels in the frame. Stamping it with `now` would make applied
        // content look freshly copied here and win the next
        // reconciliation against the machine it actually came from.
        // `.text` unconditionally: this case decoded the payload via
        // `ClipPayload.decode` two lines up -- the text-clip codec, `.clip`
        // (type 0x01) exclusively. An image applied from the peer arrives
        // as `.imageClip` instead, which does not reach this branch.
        persistClipState(ClipState(sha256: sha256Hex(textData), ts: decoded.ts, kind: .text),
                         to: clipStateStore, log: log)
        status.recordReceived()
    case .imageClip:
        // The `.clip` case above, one codec over. Task 4 added this case as
        // the smallest legal body the compiler would accept (log the receipt,
        // do nothing); it applies the image now.
        //
        // Logged rather than swallowed by `try?`, for the reason spelled out
        // at `.clip`: a drop nobody can see is what makes a mutual desync
        // permanent and invisible on both machines at once. A deliberate
        // divergence from the PC agent, whose `_write_clip` returns silently
        // on a `ClipPayloadError` -- the same divergence, with the same
        // justification, that the text path already carries.
        //
        // No separate empty-body guard, unlike `.clip`'s `!decoded.text.isEmpty`:
        // `ImagePayload.decode` refuses a body of zero bytes itself
        // (`ClipPayloadError.emptyBody`), so that case arrives here as a
        // throw and is logged. The asymmetry is the codecs': an empty clip
        // TEXT is legal and applying it is a correct, uneventful no-op, while
        // an image clip carrying no image has no representable meaning.
        let decoded: (ts: Double, png: Data)
        do {
            decoded = try ImagePayload.decode(frame.payload)
        } catch {
            log.line("could not decode an image clip from the peer: \(error)")
            return
        }
        // Arm suppression BEFORE writing, with the PNG bytes alone -- not
        // `frame.payload`, which carries the 8-byte timestamp prefix.
        // `PasteboardWatcher` hashes the body `pasteboard.read()` returns,
        // which is this PNG; arming with the prefixed payload would make
        // `EchoGuard`'s digest never match and every applied image bounce
        // straight back to the peer it came from. That ordering became
        // load-bearing for images only with this task, which is what makes
        // the watcher emit them at all.
        //
        // `.image`, and the same bytes that were armed: this case decoded an
        // `.imageClip` (type 0x03) payload, so there is nothing else it could
        // be, and `EchoGuard` compares the kind alongside the digest.
        noteWrittenLocally(.image, decoded.png)
        pasteboard.write(kind: .image, data: decoded.png)
        // The peer's timestamp, never `now` -- this is the entire reason it
        // travels in the frame. Stamping `now` would make applied content
        // look freshly copied here and win the next reconciliation against
        // the machine it actually came from.
        //
        // The hash is of what we WROTE, and on this side that is also what
        // the pasteboard will read back: `NSPasteboard` returns the bytes it
        // was given and nothing here re-encodes them. The PC agent has to
        // correct its own store afterwards (`_consume_image_reoffer`) because
        // GPaste takes over the selection and re-encodes the image; see
        // `SystemPasteboard.write`'s doc comment for why no read-back belongs
        // here.
        persistClipState(ClipState(sha256: sha256Hex(decoded.png), ts: decoded.ts, kind: .image),
                         to: clipStateStore, log: log)
        status.recordReceived()
    }
}

/// Wires the channel, the pasteboard watcher, and status reporting together.
/// Pulled out of `runAgent()` so the last unpinned link in the echo chain --
/// that the closures actually bind `watcher.noteWrittenLocally` into the
/// frame handler, not something forgotten or a no-op -- is directly
/// exercisable in a test, with no live ssh or real pasteboard needed. See
/// `Tests/clipwireTests/AgentWiringTests.swift`.
///
/// `watcher.onChange` only calls `status.recordSent()` once `channel.send`
/// reports the frame actually reached the pipe (`onSent(true)`) -- a send
/// attempted before any channel is established (`stdinPipe` still nil) must
/// not make `clipwire status` claim a clip that never left the machine.
func wireAgent(
    channel: Channel, watcher: PasteboardWatcher, pasteboard: PasteboardReading & PasteboardWriting,
    status: AgentStatus, log: Log, clipStateStore: ClipStateStore, clipStateAnnouncement: ClipStateAnnouncement
) {
    // The kind the watcher observed is passed straight through, never
    // re-derived: it comes from the same `pasteboard.read()` pair the body
    // did (see `PasteboardWatcher.onChange`). Dropping it here would compile
    // clean and pass both the watcher's and `handleLocalChange`'s own suites,
    // and the damage would surface only on the wire, where `channel.send` is
    // not observable at all -- so `AgentWiringTests` catches it through the
    // store instead.
    //
    // `status.recordSent()` stays here rather than moving into
    // `handleLocalChange`: it is only called once `channel.send` reports the
    // frame actually reached the pipe (`onSent(true)`), because a send
    // attempted before any channel is established (`stdinPipe` still nil)
    // must not make `clipwire status` claim a clip that never left the
    // machine. That is this wiring's business, not the handler's.
    watcher.onChange = { kind, payload, observedAt in
        handleLocalChange(
            kind: kind, body: payload, observedAt: observedAt,
            send: { frame in
                channel.send(frame, onSent: { sent in
                    if sent { status.recordSent() }
                })
            },
            clipStateStore: clipStateStore, log: log)
    }

    channel.onFrame = { frame in
        handleFrame(frame, send: channel.send, noteWrittenLocally: watcher.noteWrittenLocally,
                    pasteboard: pasteboard, status: status, log: log,
                    clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)
    }

    channel.onStateChange = { state, reason in
        status.applyChannelState(state, reason)
        // Channel raises `.clipboardPending` exactly once per established
        // connection attempt (see `Channel.attempt()`'s `established`
        // guard) -- the reset point that lets the one-shot announcement
        // re-arm on every reconnect rather than firing only on the
        // process's first connection ever.
        if state == .clipboardPending {
            clipStateAnnouncement.reset()
        }
    }
}

func runAgent() -> Int32 {
    let log = Log(path: AgentPaths.logPath)
    let config: Config
    do {
        config = try Config.load()
    } catch {
        log.line("\(error)")
        // `line(_:)` only enqueues the write; without this it races the
        // process actually exiting and reliably loses (confirmed
        // empirically -- see Log.flush()'s doc comment), leaving nothing
        // in the log to explain a dead agent.
        log.flush()
        // Exit 0 on a config error: the plist uses KeepAlive={SuccessfulExit: false},
        // so a non-zero exit here would give an eternal restart loop.
        return 0
    }

    let status = AgentStatus(pid: ProcessInfo.processInfo.processIdentifier, url: AgentPaths.statusURL)
    status.writeInitial()

    let channel = Channel(config: config, log: log)
    // The log is what makes a dropped image visible: a TIFF that fails to
    // convert reads back as nothing, which is indistinguishable at every
    // call site from an empty pasteboard. This is the only construction site
    // that passes one; every test constructs a `SystemPasteboard` without.
    let systemPasteboard = SystemPasteboard(log: log)
    let watcher = PasteboardWatcher(
        pasteboard: systemPasteboard,
        pollInterval: Double(config.macPollIntervalMs) / 1000.0,
        log: log)
    let clipStateStore = ClipStateStore(path: ClipStateStoreConstants.defaultPath)
    let clipStateAnnouncement = ClipStateAnnouncement()

    wireAgent(channel: channel, watcher: watcher, pasteboard: systemPasteboard, status: status, log: log,
              clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)

    watcher.start()

    let heartbeat = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    heartbeat.schedule(deadline: .now(), repeating: 5)
    heartbeat.setEventHandler {
        status.tickHeartbeat(reconnects: channel.reconnects)
    }
    heartbeat.resume()

    channel.run()   // never returns
    return 0
}

func printStatus() -> Int32 {
    switch Status.read(from: AgentPaths.statusURL) {
    case .healthy(let status):
        print("up — \(status.reconnects) reconnects")
        if let sent = status.lastSentAt { print("last sent:     \(sent)") }
        if let received = status.lastReceivedAt { print("last received: \(received)") }
        return 0
    case .unhealthy(let status, let reason):
        // `Status.read` falls back to the state's own name when the agent
        // recorded no specific reason, which would print it twice. That was
        // unreachable until `clipboard-pending` became a state the agent
        // actually reports -- it is now the normal state for the whole window
        // between a PC reboot and someone logging in to GNOME.
        if reason == status.state.rawValue {
            print(status.state.rawValue)
        } else {
            print("\(status.state.rawValue) — \(reason)")
        }
        return 1
    case .agentDead(let why):
        print("agent dead — \(why)")
        return 1
    }
}

func initConfig() -> Int32 {
    let url = Config.defaultURL
    guard !FileManager.default.fileExists(atPath: url.path) else {
        print("config already exists at \(url.path) — not overwriting")
        return 1
    }
    let example = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("config.example.json")
    do {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(contentsOf: example).write(to: url)
        print("wrote \(url.path) — edit it, then run `clipwire install`")
        return 0
    } catch {
        print("could not write config: \(error)")
        return 1
    }
}

func install() -> Int32 {
    let config: Config
    do {
        config = try Config.load()
    } catch {
        // Prints the real ConfigError (missing vs invalid, and why) rather
        // than swallowing it behind one generic line -- consistent with
        // the rest of this file's "failures are visible" intent.
        print("no usable config: \(error)")
        return 1
    }
    let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("agent/clipwire-agent.py")
    let target = config.remoteAgentPath
    let remote = "\(config.user)@\(config.host)"

    func ssh(_ command: String) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-i", expandTilde(config.identityFile),
                          "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                          "-o", "ConnectTimeout=5", remote, command]
        try? task.run()
        task.waitUntilExit()
        return task.terminationStatus
    }

    guard ssh("mkdir -p $(dirname \(target))") == 0 else {
        print("could not create the remote directory")
        return 1
    }

    let scp = Process()
    scp.executableURL = URL(fileURLWithPath: "/usr/bin/scp")
    scp.arguments = ["-i", expandTilde(config.identityFile),
                     "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     "-o", "ConnectTimeout=5",
                     source.path, "\(remote):\(target)"]
    try? scp.run()
    scp.waitUntilExit()
    guard scp.terminationStatus == 0 else {
        print("copy failed")
        return 1
    }

    guard ssh("chmod +x \(target)") == 0 else {
        print("could not set the executable bit on \(target)")
        return 1
    }
    let selftest = ssh("\(target) --selftest")
    print(selftest == 0 ? "installed and verified" : "installed, but --selftest failed")
    return selftest
}

switch Array(CommandLine.arguments.dropFirst()).first {
case "run", nil: exit(runAgent())
case "status":   exit(printStatus())
case "init":     exit(initConfig())
case "install":  exit(install())
default:
    print("usage: clipwire [run|status|init|install]")
    exit(2)
}
