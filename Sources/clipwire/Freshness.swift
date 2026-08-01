// Sources/clipwire/Freshness.swift
import Foundation

/// Named after `agent/clipwire-agent.py`'s own `ClipStateError`, which this
/// mirrors: the two sides reject the same clip-state payloads for the same
/// reasons. Only the shape checks Foundation cannot express as a `Codable`
/// conformance live here -- a malformed ts already fails inside
/// `JSONDecoder` (see `decodePayload`), so `sha256` is the only field that
/// needs one -- until Task 6 added `kind`, which needs a second: an
/// unrecognized raw string (e.g. "video") already fails inside `JSONDecoder`
/// via `ClipKind`'s own synthesized `Decodable` conformance, but "a hash
/// with no kind, or a kind with no hash" is a cross-field rule `Codable`
/// cannot express any more than a lone malformed `sha256` could.
enum ClipStateError: Error, Equatable {
    case malformedSHA256
    case kindHashMismatch
}

/// The two content kinds clip-state can describe -- `nil` (no content held)
/// is the third, implicit state; see `ClipState.kind`. Mirrors Python's
/// `KIND_TEXT`/`KIND_IMAGE` string constants as `String` raw values, so the
/// wire representation (`"text"`/`"image"`) and this name can never drift
/// apart independently of one another.
enum ClipKind: String, Codable {
    case text
    case image
}

struct ClipState: Codable, Equatable {
    let sha256: String?
    let ts: Double
    /// `nil` exactly when `sha256` is `nil` -- enforced in `init(from:)`
    /// below, not merely assumed here. A hash alone cannot tell the two
    /// sides what they are agreeing about (Task 6); `kind` is for the send
    /// branch (Task 11) and the log (Task 14), never for `resolveFreshness`'s
    /// comparison below, which stays exactly the formula it was.
    let kind: ClipKind?

    /// `init(from:)` below is a custom implementation (see its own comment
    /// for why), and a struct that defines ANY initializer of its own loses
    /// the compiler-synthesized memberwise one -- so this has to be written
    /// out explicitly now, where before Task 6 it was free.
    init(sha256: String?, ts: Double, kind: ClipKind?) {
        self.sha256 = sha256
        self.ts = ts
        self.kind = kind
    }

    /// Custom rather than the synthesized decode, because `ClipStateStore.load()`
    /// calls `JSONDecoder().decode(ClipState.self, from:)` directly (see its
    /// own comment below for why it does not route through `decodePayload`)
    /// -- so this is the ONE place the kind/hash pairing rule can live to be
    /// enforced on BOTH the wire path and a direct store read. Putting it in
    /// `decodePayload` alone, the way the `sha256` hex-shape check stays
    /// there deliberately, would leave `load()` free to accept a v2-era
    /// store file (a real `sha256`, no `"kind"` key at all) as "a hash of
    /// unknown kind" -- exactly the silent mis-load Task 6 exists to close.
    /// See `ClipStateStoreTests.testAV2StoreFileIsRejectedNotLoadedAsKindless`.
    ///
    /// `decodeIfPresent` returns `nil` identically whether the `"kind"` key
    /// is ABSENT or present with a JSON `null`, and that conflation is
    /// exactly what is wanted: a v2 file's absent key must be rejected the
    /// same way a wire payload's explicit hash-without-kind pairing is,
    /// with no separate `container.contains(.kind)` check needed to tell
    /// the two apart -- they are meant to be indistinguishable here.
    ///
    /// An unknown kind value (e.g. `"video"`) needs no bespoke check in this
    /// initializer: `decodeIfPresent(ClipKind.self, forKey: .kind)` already
    /// throws when the raw string matches neither `.text` nor `.image`,
    /// via `ClipKind`'s own synthesized `Decodable` conformance -- the same
    /// "let `JSONDecoder`'s own error speak for itself" principle
    /// `decodePayload` below already applies to a non-finite `ts`.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let sha256 = try container.decodeIfPresent(String.self, forKey: .sha256)
        let ts = try container.decode(Double.self, forKey: .ts)
        let kind = try container.decodeIfPresent(ClipKind.self, forKey: .kind)
        guard (kind == nil) == (sha256 == nil) else {
            throw ClipStateError.kindHashMismatch
        }
        self.sha256 = sha256
        self.ts = ts
        self.kind = kind
    }

    /// Throws rather than substituting a fallback payload. `JSONEncoder`
    /// already rejects a non-finite `ts` (`.nan`, `.infinity`, `-.infinity`)
    /// with `EncodingError.invalidValue` -- the previous `(try? ...) ??
    /// Data("{}".utf8)` caught exactly that and sent the two bytes `{}`
    /// instead. `{}` is valid JSON, so it decoded fine on the Python side,
    /// where `decode_clip_state` reported "ts must be a number": one hop
    /// from the real cause (a non-finite ts produced here) and on the wrong
    /// side of the wire. Python's `encode_clip_state`/`decode_clip_state`
    /// already raise `ClipStateError` on a non-finite ts in both
    /// directions; letting `JSONEncoder`'s own error propagate makes Swift
    /// fail the same way on encode, at the point of the actual cause,
    /// before anything reaches the wire. No dedicated error type: the
    /// resulting `EncodingError.invalidValue` already names the offending
    /// value and explains why, same as `decodePayload` below already just
    /// lets `JSONDecoder`'s error speak for itself.
    func encodePayload() throws -> Data {
        try JSONEncoder().encode(self)
    }

    /// Symmetric with Python's decode-side non-finite check, though for a
    /// different reason: a bare `NaN`/`Infinity` token is not valid JSON
    /// syntax, so `JSONDecoder` rejects it as malformed input outright
    /// rather than parsing it (unlike Python's `json.loads`, which accepts
    /// it as an extension). Verified empirically, not assumed: an
    /// in-syntax numeral that overflows `Double` (e.g. `1e400`) is also
    /// rejected by Foundation's JSON parser as undecodable rather than
    /// silently rounding to `.infinity`. So no extra `isFinite` guard is
    /// needed here to match Python's explicit one.
    ///
    /// The `sha256` shape check is the one thing `Codable` cannot express,
    /// and it is not defensive typing: `resolveFreshness`'s tie-break orders
    /// hashes, and the two implementations do not order strings the same
    /// way. Swift's `String` compares by canonical Unicode equivalence,
    /// Python's `str` by code point, and those coincide over lowercase hex
    /// and nowhere else -- pinned by execution in
    /// `testSwiftAndPythonOnlyAgreeOnHashOrderOverHex`. A peer announcing
    /// U+00C5 against a local "A" + U+030A therefore makes this side resolve
    /// `.doNothing` while the PC resolves WAIT_FOR_PEER: both sides wait and
    /// the clip is lost with nothing logged anywhere.
    ///
    /// Enforced here, at the boundary, rather than inside `resolveFreshness`,
    /// so that function stays exactly what it is on both sides -- one formula
    /// over already-valid input.
    ///
    /// `ClipStateStore.load()` deliberately does NOT route through here: it
    /// decodes this process's own prior write, whose hash always came from
    /// `sha256Hex`, and a corrupt one would resolve to "content changed while
    /// apart" (ts = now) either way -- the same outcome as the `nil` that
    /// `load()` already returns for a torn file. The PC agent's own
    /// `load_clip_state` does share its decoder with the wire path, so that
    /// one validates its store file as a side effect; the asymmetry is
    /// harmless in both directions. (The kind/hash pairing rule is NOT part
    /// of this asymmetry -- see `init(from:)` above for why that one is
    /// shared with `load()` rather than confined here.)
    static func decodePayload(_ data: Data) throws -> ClipState {
        let state = try JSONDecoder().decode(ClipState.self, from: data)
        if let sha256 = state.sha256, !isSHA256Hex(sha256) {
            throw ClipStateError.malformedSHA256
        }
        return state
    }
}

/// Exactly 64 characters of `[0-9a-f]` -- the shape, and the only shape,
/// `sha256Hex` produces and the wire contract allows. The twin of
/// `agent/clipwire-agent.py`'s `_is_sha256_hex`, one condition at a time.
///
/// Counts UTF-8 BYTES rather than `String.count`, which counts grapheme
/// clusters: "A" + U+030A is a single cluster, so a `count == 64` check
/// would admit a 65-byte string built from composed characters -- the exact
/// input class this guard exists to reject. 64 UTF-8 bytes all drawn from
/// the ASCII hex alphabet can only be 64 ASCII hex characters.
func isSHA256Hex(_ value: String) -> Bool {
    guard value.utf8.count == 64 else { return false }
    return value.utf8.allSatisfy { byte in
        (byte >= UInt8(ascii: "0") && byte <= UInt8(ascii: "9"))
            || (byte >= UInt8(ascii: "a") && byte <= UInt8(ascii: "f"))
    }
}

enum FreshnessDecision: String, Equatable {
    case sendMine
    case waitForPeer
    case doNothing
}

/// Decides which side sends after both have announced what they hold.
///
/// Timestamps are never compared when either hash is nil — that comparison is
/// what would otherwise put a number next to a null and take the Python agent
/// down on every handshake with an empty clipboard.
///
/// The tie is broken by comparing hashes rather than privileging a machine, so
/// both implementations run one formula instead of a mirrored pair of
/// conditions. Mirrored conditions drifting apart has already bitten this
/// project twice.
func resolveFreshness(mine: ClipState, peer: ClipState) -> FreshnessDecision {
    switch (mine.sha256, peer.sha256) {
    case (nil, nil):
        return .doNothing
    case (nil, _):
        return .waitForPeer
    case (_, nil):
        return .sendMine
    case let (mineHash?, peerHash?):
        if mineHash == peerHash { return .doNothing }
        if mine.ts > peer.ts { return .sendMine }
        if mine.ts < peer.ts { return .waitForPeer }
        return mineHash > peerHash ? .sendMine : .waitForPeer
    }
}
