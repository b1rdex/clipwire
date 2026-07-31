// Sources/clipwire/Freshness.swift
import Foundation

struct ClipState: Codable, Equatable {
    let sha256: String?
    let ts: Double

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
    static func decodePayload(_ data: Data) throws -> ClipState {
        try JSONDecoder().decode(ClipState.self, from: data)
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
