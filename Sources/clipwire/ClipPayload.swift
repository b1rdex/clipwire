// Sources/clipwire/ClipPayload.swift
import Foundation

enum ClipPayloadConstants {
    static let timestampBytes = 8
}

enum ClipPayloadError: Error, Equatable {
    case tooShort(Int)
    case invalidUTF8
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
        let body = data.dropFirst(ClipPayloadConstants.timestampBytes)
        guard let text = String(data: Data(body), encoding: .utf8) else {
            throw ClipPayloadError.invalidUTF8
        }
        return ClipPayload(ts: Double(bitPattern: bits), text: text)
    }
}
