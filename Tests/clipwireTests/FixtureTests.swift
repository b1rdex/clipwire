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
        XCTAssertEqual(clipCases.count, 7, "expected exactly 7 type-1 fixture cases")
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

    /// Pinned decode-only, same as `hello`: JSON key order and float
    /// formatting differ between Swift and Python, so a byte-exact encode
    /// vector would fail for reasons that have nothing to do with the
    /// protocol. This decodes the frame envelope (proving the type byte is
    /// really 2, not just that some payload was found) and then parses the
    /// payload's JSON directly -- there is no ClipStatePayload codec yet;
    /// that lands in a later task.
    func testClipStatePayloadDecodesGolden() throws {
        let cases = try loadFixtures().filter { $0.type == FrameType.clipState.rawValue }
        XCTAssertEqual(cases.count, 1, "expected exactly 1 type-2 fixture case")
        guard let clipState = cases.first else { return }

        var buffer = hex(clipState.frame_hex)
        let frame = try Frame.decode(from: &buffer)
        XCTAssertEqual(frame?.type, .clipState, "clip-state fixture must decode as type 2")
        XCTAssertTrue(buffer.isEmpty, "leftover bytes for \(clipState.name)")

        guard let payload = frame?.payload,
              let json = try JSONSerialization.jsonObject(with: payload) as? [String: Any] else {
            return XCTFail("clip-state payload must parse as a JSON object")
        }
        XCTAssertTrue(json["sha256"] is NSNull, "sha256 must parse to null")
        XCTAssertEqual(json["ts"] as? Double, 1.0)
    }

    /// Pins the fixtures' type-3 payload against the *image* payload codec
    /// too, not just the frame envelope. Filtered and asserted against the
    /// literal `3`, not `FrameType.imageClip.rawValue`: a vector that routed
    /// the type byte through the enum and back could not catch that enum
    /// being relabelled, which was a real defect in v1 (see
    /// task-5-brief.md's Step 5).
    func testImagePayloadDecodesGolden() throws {
        let cases = try loadFixtures().filter { $0.type == 3 }
        XCTAssertEqual(cases.count, 1, "expected exactly 1 type-3 fixture case")
        guard let c = cases.first else { return }

        var buffer = hex(c.frame_hex)
        let frame = try Frame.decode(from: &buffer)
        XCTAssertEqual(frame?.type.rawValue, 3, "image-clip fixture must decode as type 3")
        XCTAssertTrue(buffer.isEmpty, "leftover bytes for \(c.name)")

        guard let payload = frame?.payload else {
            return XCTFail("image-clip fixture must decode to a frame")
        }
        let decoded = try ImagePayload.decode(payload)
        XCTAssertEqual(decoded.ts, 1785400000.5, "ts mismatch for \(c.name)")
        // An independent, hardcoded pin -- not derived from payload_hex by
        // slicing, so it cannot pass merely by symmetry with the encoder.
        XCTAssertEqual(decoded.png, hex("89504e470d0a1a0a0000000d49484452"),
                       "png bytes mismatch for \(c.name)")
    }

    // MARK: - shared hash vectors (fixtures/hashes.json)

    struct HashCase: Decodable { let name: String; let input_hex: String; let sha256: String }
    struct HashFixtures: Decodable { let cases: [HashCase] }

    /// Shared with Python's test_fixtures.py::TestHashFixtures -- the same
    /// file, the same vectors, so neither implementation's idea of what
    /// sha256Hex/sha256_hex should produce can drift from the other's.
    ///
    /// This alone does not catch a call site that hashes the WRONG bytes (a
    /// timestamp-prefixed wire payload, or a re-encoded String) -- it only
    /// pins sha256Hex in isolation. testIncomingClipStoresTheLiteralKnownHashForAPinnedVector
    /// below (in HandleFrameTests.swift) closes that gap, by pinning a
    /// literal digest at the actual call site rather than re-deriving it
    /// from this same function.
    func testSha256HexMatchesSharedVectors() throws {
        // Tests/clipwireTests/ -> repo root -> fixtures/hashes.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/hashes.json"))
        let cases = try JSONDecoder().decode(HashFixtures.self, from: data).cases
        XCTAssertFalse(cases.isEmpty, "fixtures/hashes.json must not be empty")

        for c in cases {
            XCTAssertEqual(sha256Hex(hex(c.input_hex)), c.sha256, "sha256Hex mismatch for \(c.name)")
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
