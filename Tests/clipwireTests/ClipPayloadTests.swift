// Tests/clipwireTests/ClipPayloadTests.swift
import XCTest
@testable import clipwire

final class ClipPayloadTests: XCTestCase {
    func testRoundTrip() throws {
        let original = ClipPayload(ts: 1785400000.5, text: "привет 🔥")
        let decoded = try ClipPayload.decode(original.encode())
        XCTAssertEqual(decoded.ts, original.ts)
        XCTAssertEqual(decoded.text, original.text)
    }

    func testLayoutIsTimestampThenUTF8() {
        let encoded = ClipPayload(ts: 1.0, text: "hi").encode()
        XCTAssertEqual(encoded.count, 8 + 2)
        // 1.0 as IEEE-754 big-endian is 3F F0 00 00 00 00 00 00
        XCTAssertEqual([UInt8](encoded.prefix(8)),
                       [0x3F, 0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        XCTAssertEqual([UInt8](encoded.suffix(2)), [0x68, 0x69])
    }

    func testEmptyTextIsRepresentable() throws {
        let decoded = try ClipPayload.decode(ClipPayload(ts: 5, text: "").encode())
        XCTAssertEqual(decoded.text, "")
        XCTAssertEqual(decoded.ts, 5)
    }

    func testTooShortThrows() {
        XCTAssertThrowsError(try ClipPayload.decode(Data([0x00, 0x01, 0x02]))) { error in
            guard case ClipPayloadError.tooShort(3) = error else {
                return XCTFail("expected .tooShort(3), got \(error)")
            }
        }
    }

    func testInvalidUTF8Throws() {
        var data = Data(repeating: 0, count: 8)
        data.append(contentsOf: [0xFF, 0xFE])
        XCTAssertThrowsError(try ClipPayload.decode(data)) { error in
            guard case ClipPayloadError.invalidUTF8 = error else {
                return XCTFail("expected .invalidUTF8, got \(error)")
            }
        }
    }

    /// The Mac twin of `decode_clip_payload`'s own finiteness guard
    /// (agent/clipwire-agent.py:88). `Double(bitPattern:)` accepts every
    /// one of the 2^64 patterns, including the non-finite ones -- there is
    /// no bit pattern it rejects -- so without this check a peer sending
    /// `ts = NaN` is accepted here and only fails four steps later, inside
    /// `JSONEncoder` (pinned by FreshnessTests), where `handleFrame`'s
    /// `try?` swallows it. The pasteboard then holds the peer's content
    /// while the store still describes the PREVIOUS clip, so the next
    /// `announceClipState` sees a changed hash, stamps `now`, and announces
    /// peer-supplied content as freshly copied here -- winning the next
    /// reconciliation and bouncing it back to the machine it came from.
    /// That is exactly the clobber the persistent store exists to prevent,
    /// so it is rejected here, at the shared decode point, the way Python
    /// already does.
    func testNonFiniteTimestampThrows() {
        for ts in [Double.nan, .infinity, -.infinity] {
            XCTAssertThrowsError(try ClipPayload.decode(ClipPayload(ts: ts, text: "x").encode()),
                                 "a non-finite ts must not decode: \(ts)") { error in
                guard case ClipPayloadError.nonFiniteTimestamp = error else {
                    return XCTFail("expected .nonFiniteTimestamp, got \(error)")
                }
            }
        }
    }

    func testTimestampSurvivesSubsecondPrecision() throws {
        let ts = 1785400000.123456
        XCTAssertEqual(try ClipPayload.decode(ClipPayload(ts: ts, text: "x").encode()).ts, ts)
    }
}
