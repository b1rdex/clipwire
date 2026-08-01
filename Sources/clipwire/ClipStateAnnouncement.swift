// Sources/clipwire/ClipStateAnnouncement.swift
import CryptoKit
import Foundation

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
/// target that turns a raw `pasteboard.read()` into a (hash, kind) pair; every caller of
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

/// Saves, and logs rather than swallowing if it cannot. Every
/// `clipStateStore.save` call in the target goes through here -- four of them
/// now: `announceClipState`, `handleLocalChange`, and `handleFrame`'s `.clip` and
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
/// identical literals repeated across the target is exactly the drift this project
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
