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
