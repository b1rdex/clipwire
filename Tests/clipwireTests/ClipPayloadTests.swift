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

    func testTimestampSurvivesSubsecondPrecision() throws {
        let ts = 1785400000.123456
        XCTAssertEqual(try ClipPayload.decode(ClipPayload(ts: ts, text: "x").encode()).ts, ts)
    }
}
