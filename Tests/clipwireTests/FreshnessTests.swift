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
        XCTAssertEqual(clipStateCases.count, 3, "expected exactly 3 type-2 fixture cases in frames.json")
        let byName = Dictionary(uniqueKeysWithValues: clipStateCases.map { ($0.name, $0) })

        guard let empty = byName["clip-state"] else { return XCTFail("clip-state fixture case missing") }
        let decodedEmpty = try ClipState.decodePayload(hex(empty.payload_hex))
        XCTAssertNil(decodedEmpty.sha256, "clip-state fixture's sha256 must decode to nil")
        XCTAssertEqual(decodedEmpty.ts, 1.0)
        XCTAssertNil(decodedEmpty.kind, "a nil hash must carry a nil kind")
        XCTAssertNil(decodedEmpty.origin, "a vector written before v3.2 has no origin key, and must decode to nil")

        guard let texted = byName["clip-state-text"] else { return XCTFail("clip-state-text fixture case missing") }
        let decodedText = try ClipState.decodePayload(hex(texted.payload_hex))
        XCTAssertEqual(decodedText.sha256, String(repeating: "ab", count: 32))
        XCTAssertEqual(decodedText.ts, 1.0)
        XCTAssertEqual(decodedText.kind, .text)
        XCTAssertNil(decodedText.origin, "a vector written before v3.2 has no origin key, and must decode to nil")

        // The other half of "absent means today's behaviour": the vector
        // that carries one. Pinned as a golden frame rather than only as a
        // round trip, so both languages are held to the same bytes for the
        // one field they could still disagree about.
        guard let originated = byName["clip-state-origin"] else {
            return XCTFail("clip-state-origin fixture case missing")
        }
        let decodedOrigin = try ClipState.decodePayload(hex(originated.payload_hex))
        XCTAssertEqual(decodedOrigin.sha256, String(repeating: "cd", count: 32))
        XCTAssertEqual(decodedOrigin.ts, 1.0)
        XCTAssertEqual(decodedOrigin.kind, .image)
        XCTAssertEqual(decodedOrigin.origin, String(repeating: "ef", count: 32))
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

    // MARK: - v3.2: clip-state carries an origin

    /// One field, one rule at decode, and absence meaning exactly what it
    /// meant before -- so a v3.2 side and a v3.1 peer degrade to today's
    /// behaviour in both directions instead of failing. Read by
    /// `resolveProvenance`, and only BEFORE `resolveFreshness`, which v3.2
    /// leaves exactly as it was (the fixture-driven tests at the top of this
    /// file run unmodified against an unmodified fixtures/freshness.json).

    func testRoundTripCarriesTheOrigin() throws {
        let state = ClipState(sha256: String(repeating: "ab", count: 32), ts: 1.5, kind: .image,
                              origin: String(repeating: "cd", count: 32))
        XCTAssertEqual(try ClipState.decodePayload(state.encodePayload()), state)
    }

    /// Absence has to be absence at the BYTE level, not merely in meaning.
    /// `Codable` synthesis uses `encodeIfPresent` for an `Optional`
    /// property, so a nil origin's key is omitted rather than written as an
    /// explicit null -- asserted here by execution rather than assumed from
    /// the documentation, because it is what keeps every clip-state without
    /// an origin byte-for-byte what it was before v3.2, including the golden
    /// vectors in fixtures/frames.json, and it is the shape the PC agent's
    /// `encode_clip_state` was told to match.
    func testAStateWithoutAnOriginOmitsTheKeyEntirely() throws {
        let payload = try ClipState(sha256: String(repeating: "ab", count: 32), ts: 1.5, kind: .text)
            .encodePayload()
        guard let json = String(data: payload, encoding: .utf8) else {
            return XCTFail("payload must be UTF-8")
        }
        XCTAssertFalse(json.contains("origin"), "a nil origin must not reach the wire at all, got \(json)")
    }

    /// Absent and null must be indistinguishable on the way in, even though
    /// neither side ever writes the null: a peer is free to spell it either
    /// way and the two must not come to mean different things.
    func testAnExplicitNullOriginDecodesAsNoOrigin() throws {
        let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": "text", "origin": null}"#.utf8)
        XCTAssertNil(try ClipState.decodePayload(payload).origin)
    }

    /// The rule is NOT the nil-iff-nil pairing `kind` gets. Copying that
    /// shape would reject every ordinary announcement this protocol sends,
    /// since a hash with no origin is the normal case and an origin is the
    /// rare one. Pinned in its own test because a one-directional rule that
    /// has quietly become symmetric still passes every rejection test.
    func testAHashWithoutAnOriginIsAccepted() throws {
        let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": "text"}"#.utf8)
        let decoded = try ClipState.decodePayload(payload)
        XCTAssertEqual(decoded.sha256, String(repeating: "ab", count: 32))
        XCTAssertNil(decoded.origin)
    }

    /// The one direction that IS an error, and peer-controlled input. An
    /// origin says "what I hold was born from this hash", so it needs
    /// content of its own to describe; beside a null `sha256` it claims an
    /// ancestor for a clipboard holding nothing, which `resolveProvenance`
    /// could only ever compare against nothing. Built as raw JSON, since
    /// this side's own encoder is free to produce the pairing -- the
    /// memberwise init deliberately validates nothing -- and the boundary is
    /// where it must be caught.
    func testAnOriginWithoutAHashIsRejected() throws {
        let payload = Data(#"{"sha256": null, "ts": 1.5, "kind": null, "origin": "\#(String(repeating: "cd", count: 32))"}"#.utf8)
        XCTAssertThrowsError(try ClipState.decodePayload(payload)) { error in
            guard case ClipStateError.originWithoutHash = error else {
                return XCTFail("expected .originWithoutHash, got \(error)")
            }
        }
    }

    /// The same guard on the path that bypasses `decodePayload` entirely.
    /// `ClipStateStore.load()` calls `JSONDecoder().decode(ClipState.self,
    /// from:)` directly, which is exactly why this rule lives in
    /// `init(from:)` beside `kind`'s rather than in `decodePayload` beside
    /// the sha256 shape check.
    func testAnOriginWithoutAHashIsRejectedByTheBareDecoderToo() throws {
        let payload = Data(#"{"sha256": null, "ts": 1.5, "kind": null, "origin": "\#(String(repeating: "cd", count: 32))"}"#.utf8)
        XCTAssertThrowsError(try JSONDecoder().decode(ClipState.self, from: payload))
    }

    /// Same class as the sha256 type check: the wire is peer-controlled, and
    /// a number reaching `resolveProvenance` would compare unequal to every
    /// hash forever rather than failing where the fault is. `JSONDecoder`'s
    /// own `typeMismatch` is left to speak for itself, the way it already is
    /// for an unknown kind.
    func testANonStringOriginIsRejected() throws {
        for literal in ["5", "1.5", "true", "[]", "{}"] {
            let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": "text", "origin": \#(literal)}"#.utf8)
            XCTAssertThrowsError(try ClipState.decodePayload(payload), "must reject an origin of \(literal)")
        }
    }

    /// The upgrade path, in both of its shapes: a v3.1 peer's frame and a
    /// v3.1 store file are the same bytes -- a well-formed clip-state with
    /// no "origin" key at all -- and both must load, not fail. That is the
    /// whole meaning of "optional" here.
    func testAStateWrittenBeforeV32DecodesWithNoOrigin() throws {
        let payload = Data(#"{"sha256": "\#(String(repeating: "ab", count: 32))", "ts": 1.5, "kind": "image"}"#.utf8)
        XCTAssertEqual(try ClipState.decodePayload(payload),
                       ClipState(sha256: String(repeating: "ab", count: 32), ts: 1.5, kind: .image))
    }

    // MARK: - v3.2: provenance, the rule and its shared table

    /// fixtures/provenance.json's rows carry only the two fields the rule
    /// compares -- `sha256` and `origin` -- exactly as freshness.json's
    /// carry only its own two. `ts` and `kind` are filled in here with
    /// values `resolveProvenance` never reads, through `ClipState`'s plain
    /// memberwise init (no validation) rather than its decoder, the same way
    /// `RawState` above builds a deliberately kind-less state: this
    /// micro-fixture is exempt from the wire's pairing rules by design, and
    /// the PC agent's `_record` helper fills the same two the same way.
    struct RawProvenanceState: Decodable {
        let sha256: String?
        let origin: String?

        var asClipState: ClipState { ClipState(sha256: sha256, ts: 0, kind: nil, origin: origin) }
    }

    struct ProvenanceCase: Decodable {
        let name: String
        let mine: RawProvenanceState
        let peer: RawProvenanceState
        let expect: Bool
    }
    struct ProvenanceFixtures: Decodable { let cases: [ProvenanceCase] }

    func loadProvenanceFixtures() throws -> [ProvenanceCase] {
        // Tests/clipwireTests/ -> repo root -> fixtures/provenance.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/provenance.json"))
        let cases = try JSONDecoder().decode(ProvenanceFixtures.self, from: data).cases
        XCTAssertFalse(cases.isEmpty, "fixtures/provenance.json must not be empty")
        return cases
    }

    /// A dedicated, explicit non-vacuousness check, distinct from the
    /// assertion folded into `loadProvenanceFixtures()` above. Pins the
    /// exact row count too, so a truncated file -- or one that quietly loses
    /// its nil rows, the only ones that discriminate against the bug below
    /// -- still fails.
    func testProvenanceFixtureFileIsNotEmpty() throws {
        let cases = try loadProvenanceFixtures()
        XCTAssertFalse(cases.isEmpty, "fixtures/provenance.json must not be empty")
        XCTAssertEqual(cases.count, 12, "expected exactly 12 provenance fixture cases")
    }

    /// Drives every row of the table shared with the PC agent's
    /// `TestProvenanceFixture` through `resolveProvenance` -- the same file,
    /// the same rule. Each row maps to exactly one verdict, so flipping any
    /// single row's `expect` fails that row alone, not the suite in general.
    func testResolveProvenanceMatchesFixtureTable() throws {
        for c in try loadProvenanceFixtures() {
            XCTAssertEqual(resolveProvenance(mine: c.mine.asClipState, peer: c.peer.asClipState),
                           c.expect,
                           "resolveProvenance mismatch for \(c.name)")
        }
    }

    /// The fixture's real job, checked rather than assumed. `nil == nil` is
    /// `true` for a Swift `Optional` exactly as `None == None` is `True` in
    /// Python, so the naive spelling of this rule -- equality with no
    /// present-value guard -- fires on an empty clipboard against a peer
    /// with no origin, stands both sides down, and kills
    /// `resolveFreshness`'s `(_, nil) -> .sendMine` recovery for every kind
    /// of content. A table built only from present values would pass with
    /// exactly that bug in it.
    ///
    /// So the naive rule is written out here and run over the same table,
    /// and the table is required to catch it. This is the break-and-observe
    /// check made permanent, and it has to exist in BOTH languages: a
    /// Python-only version proves nothing about the `Optional` comparison
    /// that would ship here.
    func testTheTableWouldCatchPlainEquality() throws {
        func naive(mine: ClipState, peer: ClipState) -> Bool {
            peer.origin == mine.sha256 || mine.origin == peer.sha256
        }
        let caught = try loadProvenanceFixtures().filter {
            naive(mine: $0.mine.asClipState, peer: $0.peer.asClipState) != $0.expect
        }
        XCTAssertFalse(caught.isEmpty,
                       "no row in fixtures/provenance.json distinguishes the real rule from plain "
                        + "equality -- the table cannot catch the one bug it exists to catch")
    }

    /// The property that makes one function correct for both machines:
    /// provenance is symmetric, so the Mac and the PC reach the SAME verdict
    /// from opposite viewpoints and stand down together. An asymmetric
    /// implementation -- "the Mac's rule" and "the PC's rule", which is what
    /// a per-side split grows into -- would leave one machine waiting for a
    /// clip the other has already decided not to send, which is v1's silent
    /// loss reintroduced one layer up.
    func testProvenanceVerdictIsTheSameWhenSidesSwap() throws {
        for c in try loadProvenanceFixtures() {
            let mineView = resolveProvenance(mine: c.mine.asClipState, peer: c.peer.asClipState)
            let peerView = resolveProvenance(mine: c.peer.asClipState, peer: c.mine.asClipState)
            XCTAssertEqual(peerView, mineView, "not symmetric for \(c.name)")
        }
    }

    /// The daily case, pinned literally rather than only as a fixture row,
    /// because it is the one this rule must never break: a Mac holding a
    /// clip against a locked PC whose clipboard reads as nothing. Provenance
    /// must stay out of the way so `resolveFreshness` can hand the peer its
    /// clipboard back.
    func testAnEmptyPeerWithNoOriginStillRecovers() {
        let mine = ClipState(sha256: String(repeating: "aa", count: 32), ts: 5, kind: .text)
        let peer = ClipState(sha256: nil, ts: 1, kind: nil)
        XCTAssertFalse(resolveProvenance(mine: mine, peer: peer),
                       "two absent origins must not compare equal")
        XCTAssertEqual(resolveFreshness(mine: mine, peer: peer), .sendMine,
                       "and the recovery this protects must still fire")
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
