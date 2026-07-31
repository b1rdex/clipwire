// Tests/clipwireTests/FixtureTests.swift
import XCTest
@testable import clipwire

final class FixtureTests: XCTestCase {
    struct Case: Decodable {
        let name: String
        let type: UInt8
        let payload_hex: String
        let frame_hex: String
    }
    struct Fixtures: Decodable { let cases: [Case] }

    func loadFixtures() throws -> [Case] {
        // Tests/clipwireTests/ -> repo root -> fixtures/frames.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/frames.json"))
        let cases = try JSONDecoder().decode(Fixtures.self, from: data).cases
        XCTAssertFalse(cases.isEmpty, "fixture file must not be empty")
        return cases
    }

    func testEncodeMatchesGolden() throws {
        for c in try loadFixtures() {
            let frame = Frame(type: FrameType(rawValue: c.type)!, payload: hex(c.payload_hex))
            XCTAssertEqual(frame.encode(), hex(c.frame_hex), "encode mismatch for \(c.name)")
        }
    }

    func testDecodeMatchesGolden() throws {
        for c in try loadFixtures() {
            var buffer = hex(c.frame_hex)
            let frame = try Frame.decode(from: &buffer)
            XCTAssertEqual(frame?.type.rawValue, c.type, "type mismatch for \(c.name)")
            XCTAssertEqual(frame?.payload, hex(c.payload_hex), "payload mismatch for \(c.name)")
            XCTAssertTrue(buffer.isEmpty, "leftover bytes for \(c.name)")
        }
    }

    /// Pins the fixtures' type-1 payloads against the *clip payload* codec too,
    /// not just the frame envelope -- otherwise the fixture file pins the
    /// envelope while the new [ts][text] inner layout drifts freely.
    ///
    /// Text is compared as UTF-8 bytes rather than `String ==`: Swift's `String`
    /// equality is canonical-equivalence-based (`"e\u{301}" == "\u{e9}"`), so a
    /// literal-string comparison would not notice `nfd-clip`'s decomposed
    /// character being silently normalized -- the exact regression that fixture
    /// exists to catch.
    func testClipPayloadDecodesGolden() throws {
        let clipCases = try loadFixtures().filter { $0.type == FrameType.clip.rawValue }
        XCTAssertEqual(clipCases.count, 6, "expected exactly 6 type-1 fixture cases")
        for c in clipCases {
            let payload = hex(c.payload_hex)
            let decoded = try ClipPayload.decode(payload)
            XCTAssertEqual(decoded.ts, 1.0, "ts mismatch for \(c.name)")
            let expectedTextBytes = [UInt8](payload.dropFirst(ClipPayloadConstants.timestampBytes))
            XCTAssertEqual(Array(decoded.text.utf8), expectedTextBytes, "text bytes mismatch for \(c.name)")
        }

        // An independent, hardcoded pin -- not derived from payload_hex by
        // slicing, so it cannot pass merely by symmetry with the encoder.
        guard let ascii = clipCases.first(where: { $0.name == "ascii-clip" }) else {
            return XCTFail("ascii-clip fixture case missing")
        }
        let asciiDecoded = try ClipPayload.decode(hex(ascii.payload_hex))
        XCTAssertEqual(asciiDecoded.ts, 1.0)
        XCTAssertEqual(asciiDecoded.text, "hi")
    }

    private func hex(_ s: String) -> Data {
        var out = Data()
        var i = s.startIndex
        while i < s.endIndex {
            let j = s.index(i, offsetBy: 2)
            out.append(UInt8(s[i..<j], radix: 16)!)
            i = j
        }
        return out
    }
}
