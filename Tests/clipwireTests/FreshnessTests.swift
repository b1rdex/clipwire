// Tests/clipwireTests/FreshnessTests.swift
import XCTest
@testable import clipwire

final class FreshnessTests: XCTestCase {
    /// fixtures/freshness.json's `mine`/`peer` rows are (sha256, ts) only --
    /// deliberately, and permanently: Task 6 confirmed `resolveFreshness`'s
    /// FORMULA does not read `kind` at all (see its own doc comment), and
    /// the brief is explicit that no edit to this fixture is ever the right
    /// fix. Decoded into this lightweight type rather than `ClipState`
    /// itself, so this fixture stays exempt from `ClipState.init(from:)`'s
    /// kind/hash equivalence rule -- a rule that exists to catch a REAL
    /// clip-state payload missing its kind (a v2 store file, or a malformed
    /// wire frame), not this deliberately kind-less micro-fixture. `asClipState`
    /// builds the value `resolveFreshness` actually takes, via `ClipState`'s
    /// plain memberwise init (no validation) rather than its decoder.
    struct RawState: Decodable {
        let sha256: String?
        let ts: Double

        var asClipState: ClipState { ClipState(sha256: sha256, ts: ts, kind: nil) }
    }

    struct Case: Decodable {
        let name: String
        let mine: RawState
        let peer: RawState
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
            let actual = resolveFreshness(mine: c.mine.asClipState, peer: c.peer.asClipState)
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
            let mineView = resolveFreshness(mine: c.mine.asClipState, peer: c.peer.asClipState)
            let peerView = resolveFreshness(mine: c.peer.asClipState, peer: c.mine.asClipState)
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
                                 ts: 1785400000.5, kind: .text)
        XCTAssertEqual(try ClipState.decodePayload(withHash.encodePayload()), withHash)

        let empty = ClipState(sha256: nil, ts: 0, kind: nil)
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
            let payload = try ClipState(sha256: value, ts: 1, kind: .text).encodePayload()
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
        XCTAssertEqual(try ClipState.decodePayload(ClipState(sha256: real, ts: 1, kind: .text).encodePayload()).sha256,
                       real)
        XCTAssertNil(try ClipState.decodePayload(ClipState(sha256: nil, ts: 1, kind: nil).encodePayload()).sha256)
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
        XCTAssertEqual(resolveFreshness(mine: ClipState(sha256: "A\u{030A}", ts: 1, kind: .text),
                                        peer: ClipState(sha256: "\u{00C5}", ts: 1, kind: .text)),
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
            XCTAssertThrowsError(try ClipState(sha256: "aa", ts: badTs, kind: .text).encodePayload(),
                                  "ts=\(badTs) must not silently encode as {}")
        }
    }

    /// Pins `ClipState.decodePayload` against the type-2 vectors in
    /// `fixtures/frames.json`: the original null-hash/null-kind vector
    /// (pinned decode-only back in Task 4, before this codec existed) and
    /// the real-hash/text-kind vector Task 6 adds alongside it. Checked
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
        XCTAssertEqual(clipStateCases.count, 2, "expected exactly 2 type-2 fixture cases in frames.json")
        let byName = Dictionary(uniqueKeysWithValues: clipStateCases.map { ($0.name, $0) })

        guard let empty = byName["clip-state"] else { return XCTFail("clip-state fixture case missing") }
        let decodedEmpty = try ClipState.decodePayload(hex(empty.payload_hex))
        XCTAssertNil(decodedEmpty.sha256, "clip-state fixture's sha256 must decode to nil")
        XCTAssertEqual(decodedEmpty.ts, 1.0)
        XCTAssertNil(decodedEmpty.kind, "a nil hash must carry a nil kind")

        guard let texted = byName["clip-state-text"] else { return XCTFail("clip-state-text fixture case missing") }
        let decodedText = try ClipState.decodePayload(hex(texted.payload_hex))
        XCTAssertEqual(decodedText.sha256, String(repeating: "ab", count: 32))
        XCTAssertEqual(decodedText.ts, 1.0)
        XCTAssertEqual(decodedText.kind, .text)
    }

    // MARK: - Task 6: clip-state carries a kind

    /// A hash alone cannot tell the two sides what they are agreeing about,
    /// so clip-state now carries a `kind`. This does NOT touch
    /// `resolveFreshness` itself -- the resolution formula is unchanged and
    /// still compares only `sha256`/`ts` (see the fixture-driven tests
    /// above, run unmodified against fixtures/freshness.json); `kind` is for
    /// the send branch (Task 11) and the log (Task 14).
    ///
    /// Two validation rules, enforced at decode -- the wire is peer-
    /// controlled input: `kind` must be nil exactly when `sha256` is nil,
    /// and otherwise must be one of the two known values, so an unknown
    /// kind can never reach the send branch that switches on it.

    func testRoundTripCarriesTheKind() throws {
        for kind in [ClipKind.text, .image] {
            let state = ClipState(sha256: String(repeating: "ab", count: 32), ts: 1.5, kind: kind)
            XCTAssertEqual(try ClipState.decodePayload(state.encodePayload()), state)
        }
    }

    func testANullHashCarriesANullKind() throws {
        let state = ClipState(sha256: nil, ts: 1.5, kind: nil)
        XCTAssertEqual(try ClipState.decodePayload(state.encodePayload()), state)
    }

    /// An unknown kind must not reach the send branch, which switches on
    /// it. Built as raw JSON, not through `ClipState`/`ClipKind`, since
    /// `ClipKind` cannot represent an unknown value by construction --
    /// exactly the point: this is peer-controlled input, not something this
    /// side's own encoder could ever produce.
    func testAnUnknownKindIsRejected() throws {
        let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": "video"}"#.utf8)
        XCTAssertThrowsError(try ClipState.decodePayload(payload))
    }

    func testAHashWithoutAKindIsRejected() throws {
        let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": null}"#.utf8)
        XCTAssertThrowsError(try ClipState.decodePayload(payload))
    }

    /// The other direction of the same rule: a v2 store file (real hash, no
    /// kind at all) is the practical case that matters
    /// (ClipStateStoreTests.swift's testAV2StoreFileIsRejectedNotLoadedAsKindless),
    /// but the rule itself is symmetric, so both directions are pinned here.
    func testAKindWithoutAHashIsRejected() throws {
        let payload = Data(#"{"sha256": null, "ts": 1.5, "kind": "text"}"#.utf8)
        XCTAssertThrowsError(try ClipState.decodePayload(payload))
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
