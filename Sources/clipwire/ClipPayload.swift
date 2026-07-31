// Sources/clipwire/ClipPayload.swift
import Foundation

enum ClipPayloadConstants {
    static let timestampBytes = 8
}

enum ClipPayloadError: Error, Equatable {
    case tooShort(Int)
    case invalidUTF8
    /// Deliberately carries no associated value, unlike `tooShort(Int)`.
    /// The obvious `nonFiniteTimestamp(Double)` would make this enum's
    /// synthesized `Equatable` unusable for exactly the case that matters:
    /// `.nonFiniteTimestamp(.nan) == .nonFiniteTimestamp(.nan)` is FALSE,
    /// since IEEE-754 says no NaN equals anything, so a test comparing the
    /// thrown error against an expected one would fail on the commonest of
    /// the three inputs while passing on the two infinities.
    case nonFiniteTimestamp
}

/// The payload of a clip frame: when the text was copied, then the text.
///
/// The timestamp travels with the clip because the receiving side must record
/// the *peer's* timestamp for content it applies. Without it, applied content
/// would look freshly copied here and bounce straight back on the next
/// reconciliation.
struct ClipPayload: Equatable {
    let ts: Double
    let text: String

    func encode() -> Data {
        var out = Data(capacity: ClipPayloadConstants.timestampBytes + text.utf8.count)
        var bits = ts.bitPattern.bigEndian
        withUnsafeBytes(of: &bits) { out.append(contentsOf: $0) }
        out.append(contentsOf: Array(text.utf8))
        return out
    }

    static func decode(_ data: Data) throws -> ClipPayload {
        guard data.count >= ClipPayloadConstants.timestampBytes else {
            throw ClipPayloadError.tooShort(data.count)
        }
        let head = data.prefix(ClipPayloadConstants.timestampBytes)
        var bits: UInt64 = 0
        for byte in head { bits = (bits << 8) | UInt64(byte) }
        // Checked here, immediately after the bit pattern becomes a Double
        // and before the text is even looked at, mirroring the position of
        // `decode_clip_payload`'s own guard (right after its
        // `struct.unpack`) in agent/clipwire-agent.py. `Double(bitPattern:)`
        // has no rejected input: every one of the 2^64 patterns is a valid
        // IEEE-754 double, including NaN and both infinities.
        //
        // Guarding at this shared decode point, rather than at the one
        // caller, is what makes the failure loud instead of self-
        // perpetuating. `handleFrame`'s `.clip` case applies the text to
        // the pasteboard first and only then persists `ClipState(ts:)`,
        // where `JSONEncoder` rejects a non-finite ts (pinned by
        // FreshnessTests) -- so the pasteboard would hold the peer's
        // content while the store still described the previous clip, and
        // the next `announceClipState` would see a changed hash, stamp
        // `now`, and announce peer-supplied content as freshly copied here.
        let ts = Double(bitPattern: bits)
        guard ts.isFinite else {
            throw ClipPayloadError.nonFiniteTimestamp
        }
        let body = data.dropFirst(ClipPayloadConstants.timestampBytes)
        guard let text = String(data: Data(body), encoding: .utf8) else {
            throw ClipPayloadError.invalidUTF8
        }
        return ClipPayload(ts: ts, text: text)
    }
}
