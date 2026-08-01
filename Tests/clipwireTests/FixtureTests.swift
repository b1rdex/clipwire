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
    /// really 2, not just that some payload was found) and then parses each
    /// payload's JSON directly, asserting the full (sha256, ts, kind) triple
    /// for both the null-hash/null-kind row and the real-hash/text-kind row
    /// Task 6 added alongside it -- and, since v3.2, the origin each row
    /// does or does not carry.
    func testClipStatePayloadDecodesGolden() throws {
        let cases = try loadFixtures().filter { $0.type == FrameType.clipState.rawValue }
        XCTAssertEqual(cases.count, 3, "expected exactly 3 type-2 fixture cases")
        let byName = Dictionary(uniqueKeysWithValues: cases.map { ($0.name, $0) })

        guard let empty = byName["clip-state"] else { return XCTFail("clip-state fixture case missing") }
        var emptyBuffer = hex(empty.frame_hex)
        let emptyFrame = try Frame.decode(from: &emptyBuffer)
        XCTAssertEqual(emptyFrame?.type, .clipState, "clip-state fixture must decode as type 2")
        XCTAssertTrue(emptyBuffer.isEmpty, "leftover bytes for \(empty.name)")
        guard let emptyPayload = emptyFrame?.payload,
              let emptyJSON = try JSONSerialization.jsonObject(with: emptyPayload) as? [String: Any] else {
            return XCTFail("clip-state payload must parse as a JSON object")
        }
        XCTAssertTrue(emptyJSON["sha256"] is NSNull, "sha256 must parse to null")
        XCTAssertEqual(emptyJSON["ts"] as? Double, 1.0)
        XCTAssertTrue(emptyJSON["kind"] is NSNull, "a null hash must carry a null kind")
        XCTAssertNil(emptyJSON["origin"], "a pre-v3.2 vector carries no origin key at all")

        guard let texted = byName["clip-state-text"] else { return XCTFail("clip-state-text fixture case missing") }
        var textedBuffer = hex(texted.frame_hex)
        let textedFrame = try Frame.decode(from: &textedBuffer)
        XCTAssertEqual(textedFrame?.type, .clipState, "clip-state-text fixture must decode as type 2")
        XCTAssertTrue(textedBuffer.isEmpty, "leftover bytes for \(texted.name)")
        guard let textedPayload = textedFrame?.payload,
              let textedJSON = try JSONSerialization.jsonObject(with: textedPayload) as? [String: Any] else {
            return XCTFail("clip-state-text payload must parse as a JSON object")
        }
        XCTAssertEqual(textedJSON["sha256"] as? String, String(repeating: "ab", count: 32))
        XCTAssertEqual(textedJSON["ts"] as? Double, 1.0)
        XCTAssertEqual(textedJSON["kind"] as? String, "text")
        XCTAssertNil(textedJSON["origin"], "a pre-v3.2 vector carries no origin key at all")

        // v3.2's row. The two above it are the "without" half of the pair:
        // their bytes did not move when the field was added, which is what
        // an OPTIONAL field means at the wire level rather than only in
        // prose.
        guard let originated = byName["clip-state-origin"] else {
            return XCTFail("clip-state-origin fixture case missing")
        }
        var originatedBuffer = hex(originated.frame_hex)
        let originatedFrame = try Frame.decode(from: &originatedBuffer)
        XCTAssertEqual(originatedFrame?.type, .clipState, "clip-state-origin fixture must decode as type 2")
        XCTAssertTrue(originatedBuffer.isEmpty, "leftover bytes for \(originated.name)")
        guard let originatedPayload = originatedFrame?.payload,
              let originatedJSON = try JSONSerialization.jsonObject(with: originatedPayload) as? [String: Any] else {
            return XCTFail("clip-state-origin payload must parse as a JSON object")
        }
        XCTAssertEqual(originatedJSON["sha256"] as? String, String(repeating: "cd", count: 32))
        XCTAssertEqual(originatedJSON["ts"] as? Double, 1.0)
        XCTAssertEqual(originatedJSON["kind"] as? String, "image")
        XCTAssertEqual(originatedJSON["origin"] as? String, String(repeating: "ef", count: 32))
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
