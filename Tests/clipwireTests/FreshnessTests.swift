// Tests/clipwireTests/FreshnessTests.swift
import XCTest
@testable import clipwire

final class FreshnessTests: XCTestCase {
    struct Case: Decodable {
        let name: String
        let mine: ClipState
        let peer: ClipState
        let expect: String
    }
    struct Fixtures: Decodable { let cases: [Case] }

    func loadFixtures() throws -> [Case] {
        // Tests/clipwireTests/ -> repo root -> fixtures/freshness.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/freshness.json"))
        let cases = try JSONDecoder().decode(Fixtures.self, from: data).cases
        XCTAssertFalse(cases.isEmpty, "fixture file must not be empty")
        return cases
    }

    /// A dedicated, explicit non-vacuousness check, distinct from the assertion
    /// folded into `loadFixtures()` above (which every other test here also
    /// relies on implicitly). A misread or silently-truncated fixture file must
    /// never let `testResolveFreshnessMatchesFixtureTable` pass by iterating
    /// zero times, so this pins the exact row count too.
    func testFixtureFileIsNotEmpty() throws {
        let cases = try loadFixtures()
        XCTAssertFalse(cases.isEmpty, "fixtures/freshness.json must not be empty")
        XCTAssertEqual(cases.count, 8, "expected exactly 8 freshness fixture cases")
    }

    /// Drives every row of the shared decision table -- the same file Task 6's
    /// Python suite reads against the identical rule -- through
    /// `resolveFreshness`. The eight rows cover both-null, mine-null,
    /// peer-null, equal hashes, mine newer, peer newer, and both tie-break
    /// directions: every combination the decision can reach. Each row's
    /// mine/peer pair maps to exactly one decision, so flipping any single
    /// row's `expect` fails that row alone, not the suite in general.
    func testResolveFreshnessMatchesFixtureTable() throws {
        for c in try loadFixtures() {
            guard let expected = FreshnessDecision(rawValue: c.expect) else {
                XCTFail("unknown expected decision '\(c.expect)' for \(c.name)")
                continue
            }
            let actual = resolveFreshness(mine: c.mine, peer: c.peer)
            XCTAssertEqual(actual, expected, "resolveFreshness mismatch for \(c.name)")
        }
    }

    /// The property the shared formula exists to guarantee: running the same
    /// function on both machines with mine/peer swapped must yield exact
    /// opposites -- never both `waitForPeer` (the clip is lost forever, v1's
    /// bug) and never both `sendMine` (a ping-pong). A pair of hand-mirrored
    /// per-language conditions could still pass
    /// `testResolveFreshnessMatchesFixtureTable` in isolation while failing
    /// this -- exactly the class of defect this project has already hit
    /// twice, per the brief and the design doc.
    func testDecisionIsComplementaryWhenSidesSwap() throws {
        for c in try loadFixtures() {
            let mineView = resolveFreshness(mine: c.mine, peer: c.peer)
            let peerView = resolveFreshness(mine: c.peer, peer: c.mine)
            let expectedPeerView: FreshnessDecision
            switch mineView {
            case .sendMine: expectedPeerView = .waitForPeer
            case .waitForPeer: expectedPeerView = .sendMine
            case .doNothing: expectedPeerView = .doNothing
            }
            XCTAssertEqual(peerView, expectedPeerView, "not complementary for \(c.name)")
        }
    }

    func testClipStatePayloadRoundTrips() throws {
        // 64 lowercase hex: the only shape `decodePayload` accepts, and the
        // only shape `sha256Hex` -- hence the wire -- ever produces.
        let withHash = ClipState(sha256: String(repeating: "deadbeefcafe0123", count: 4),
                                 ts: 1785400000.5)
        XCTAssertEqual(try ClipState.decodePayload(withHash.encodePayload()), withHash)

        let empty = ClipState(sha256: nil, ts: 0)
        XCTAssertEqual(try ClipState.decodePayload(empty.encodePayload()), empty)
    }

    /// The wire contract says `sha256` is `hexdigest()` output or null, and
    /// the whole cross-language comparison rests on that domain: this side
    /// orders hashes by canonical Unicode equivalence, the PC agent orders
    /// them by code point, and the two coincide only over lowercase hex.
    /// `testSwiftAndPythonOnlyAgreeOnHashOrderOverHex` below pins the
    /// divergence this keeps out of reach.
    func testDecodePayloadRejectsASHA256ThatIsNot64LowercaseHex() throws {
        let bad = [
            "",                                          // empty
            "aa",                                        // too short
            String(repeating: "0", count: 63),           // one short of the boundary
            String(repeating: "0", count: 65),           // one past it
            String(repeating: "A", count: 64),           // uppercase: hexdigest() never emits it
            String(repeating: "g", count: 64),           // right length, outside the hex alphabet
            String(repeating: "0", count: 63) + " ",     // trailing space
            String(repeating: "\u{00C5}", count: 64),    // the composed character this exists for
            "A\u{030A}" + String(repeating: "0", count: 62),  // its canonical decomposition
        ]
        for value in bad {
            let payload = try ClipState(sha256: value, ts: 1).encodePayload()
            XCTAssertThrowsError(try ClipState.decodePayload(payload),
                                 "must reject a sha256 of \(value.debugDescription)") { error in
                guard case ClipStateError.malformedSHA256 = error else {
                    return XCTFail("expected .malformedSHA256, got \(error)")
                }
            }
        }
    }

    /// The other half: the guard must not reject what the protocol actually
    /// carries -- a real digest, and the null that means an empty or
    /// unreadable clipboard.
    func testDecodePayloadAcceptsARealDigestAndNull() throws {
        let real = sha256Hex(Data("anything at all".utf8))
        XCTAssertEqual(try ClipState.decodePayload(ClipState(sha256: real, ts: 1).encodePayload()).sha256,
                       real)
        XCTAssertNil(try ClipState.decodePayload(ClipState(sha256: nil, ts: 1).encodePayload()).sha256)
    }

    /// Why the guard above is not defensive typing. Pinned by execution
    /// rather than argued from documentation: Swift's `String` compares by
    /// canonical equivalence, so these two distinct byte sequences are EQUAL
    /// here, while Python's `str` compares by code point and puts U+00C5
    /// above "A" + U+030A. Feed both sides those two hashes with equal
    /// timestamps and this side resolves `.doNothing` while the PC resolves
    /// WAIT_FOR_PEER: both wait, and the clip is lost with nothing logged on
    /// either machine. `resolveFreshness` is deliberately left domain-
    /// agnostic -- one formula over valid input -- and the domain is enforced
    /// at the decode boundary instead.
    func testSwiftAndPythonOnlyAgreeOnHashOrderOverHex() {
        XCTAssertEqual("\u{00C5}", "A\u{030A}",
                       "Swift's String is canonically equivalent here; Python's str is not")
        XCTAssertEqual(resolveFreshness(mine: ClipState(sha256: "A\u{030A}", ts: 1),
                                        peer: ClipState(sha256: "\u{00C5}", ts: 1)),
                       .doNothing,
                       "this side sees agreement where the PC sees a conflict it expects US to lose")
    }

    /// `JSONEncoder` rejects a non-finite `Double` (`.nan`, `.infinity`,
    /// `-.infinity`) with `EncodingError.invalidValue` by default. The
    /// previous `encodePayload` caught exactly that error with `try?` and
    /// substituted `Data("{}".utf8)` -- valid-looking JSON that silently
    /// discarded the failure. That payload still decodes on the Python
    /// side, so `decode_clip_state` reported "ts must be a number": one hop
    /// from the real cause (a non-finite ts on the Swift side) and on the
    /// wrong side of the wire. Python's `encode_clip_state`/
    /// `decode_clip_state` already raise `ClipStateError` on a non-finite ts
    /// in both directions (test_freshness.py); this pins the matching
    /// behaviour on the Swift encode side, at the point of the actual
    /// cause, before anything reaches the wire.
    func testEncodePayloadThrowsOnNonFiniteTimestamp() {
        for badTs in [Double.nan, .infinity, -.infinity] {
            XCTAssertThrowsError(try ClipState(sha256: "aa", ts: badTs).encodePayload(),
                                  "ts=\(badTs) must not silently encode as {}")
        }
    }

    /// Pins `ClipState.decodePayload` against the *existing* `clip-state`
    /// vector in `fixtures/frames.json`, pinned decode-only back in Task 4
    /// specifically because this codec did not exist yet ("there is no
    /// ClipStatePayload codec yet; that lands in a later task" -- this task).
    /// That vector is the payload this codec must decode through -- checked
    /// directly against the fixture file, not assumed from reading its shape.
    func testClipStateDecodesExistingFrameFixture() throws {
        struct FrameCase: Decodable {
            let name: String
            let type: UInt8
            let payload_hex: String
        }
        struct FrameFixtures: Decodable { let cases: [FrameCase] }

        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/frames.json"))
        let allCases = try JSONDecoder().decode(FrameFixtures.self, from: data).cases
        let clipStateCases = allCases.filter { $0.type == FrameType.clipState.rawValue }
        XCTAssertEqual(clipStateCases.count, 1, "expected exactly 1 type-2 fixture case in frames.json")
        guard let clipState = clipStateCases.first else { return }

        let decoded = try ClipState.decodePayload(hex(clipState.payload_hex))
        XCTAssertNil(decoded.sha256, "clip-state fixture's sha256 must decode to nil")
        XCTAssertEqual(decoded.ts, 1.0)
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
