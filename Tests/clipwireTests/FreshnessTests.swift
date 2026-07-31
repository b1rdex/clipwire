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
        let withHash = ClipState(sha256: "deadbeefcafe", ts: 1785400000.5)
        XCTAssertEqual(try ClipState.decodePayload(withHash.encodePayload()), withHash)

        let empty = ClipState(sha256: nil, ts: 0)
        XCTAssertEqual(try ClipState.decodePayload(empty.encodePayload()), empty)
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
