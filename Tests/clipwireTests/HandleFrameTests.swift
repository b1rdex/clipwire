// Tests/clipwireTests/HandleFrameTests.swift
//
// Fix round 1, Finding 1: neither of this task's two central contracts
// (arm suppression before writing an incoming clip; always reply to a
// hello) was testable while frame handling lived as an inline closure
// inside `runAgent()`, writing straight to `NSPasteboard.general` and
// calling `channel.send`/`watcher.noteWrittenLocally` directly. Pulling
// it out as `handleFrame(_:send:noteWrittenLocally:pasteboard:status:log:)`
// makes both assertable with plain spies -- no real pasteboard, no real
// ssh, no function that blocks forever.
//
// Task 9 extends this suite with the freshness-reconciliation contracts:
// an incoming clip now carries and must store the PEER's timestamp, not
// `now`; a matched hello triggers exactly one clip-state announcement per
// connection; and an incoming clip-state frame is resolved against what we
// hold, producing a send only when we win.
import XCTest
@testable import clipwire

final class HandleFrameTests: XCTestCase {
    /// Records every `writeText` call and, via `onWrite`, lets a test
    /// observe exactly when it happens relative to other calls -- the
    /// ORDER is what these tests pin, not merely that a write occurred.
    /// A spy that only checked occurrence would pass against code that
    /// armed the suppression after writing instead of before.
    ///
    /// Also conforms to `PasteboardReading` (`changeCount`/`readText`),
    /// which `handleFrame`'s new clip-state paths need: announcing our own
    /// state and answering a peer's announcement both require reading
    /// whatever the pasteboard currently holds, not only writing to it.
    final class RecordingPasteboard: PasteboardReading, PasteboardWriting {
        private(set) var writtenTexts: [String] = []
        var onWrite: (() -> Void)?
        var changeCount = 0
        var textToRead: Data?

        func readText() -> Data? { textToRead }

        func writeText(_ text: String) {
            writtenTexts.append(text)
            onWrite?()
        }
    }

    private func tempStatusURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString).json")
    }

    private func tempLog() -> Log {
        Log(path: FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path)
    }

    /// A real, temp-path-backed store -- not a live production state
    /// directory -- mirroring `tempStatusURL()`/`tempLog()`'s existing
    /// pattern of injecting real-but-disposable dependencies rather than
    /// mocking file I/O.
    private func tempClipStateStore() -> ClipStateStore {
        ClipStateStore(path: FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString).json").path)
    }

    // MARK: - Contract 1: arm-before-write

    func testIncomingClipArmsSuppressionBeforeWritingToThePasteboard() {
        var order: [String] = []
        let pasteboard = RecordingPasteboard()
        pasteboard.onWrite = { order.append("write") }
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clip, payload: ClipPayload(ts: 555, text: "hello").encode()),
                    send: { _ in XCTFail("a clip frame must never trigger a reply") },
                    noteWrittenLocally: { _ in order.append("arm") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(order, ["arm", "write"],
                       "suppression must be armed strictly before the pasteboard write -- reversed " +
                       "order would let the watcher observe the write before the suppression exists " +
                       "and bounce our own clip back to the peer")
        XCTAssertEqual(pasteboard.writtenTexts, ["hello"], "exactly one write, with the decoded text")
    }

    /// Distinct from the ordering test above: this pins WHAT is armed, not
    /// merely WHEN. `PasteboardWatcher.poll()` hashes whatever
    /// `pasteboard.readText()` returns -- the plain text alone, never a
    /// timestamp prefix. Arming with `frame.payload` (the ts-prefixed clip
    /// payload) instead of the decoded text would make `EchoGuard`'s stored
    /// digest never match poll()'s later read, so `shouldSend` would always
    /// return true and EVERY applied remote clip would bounce straight back
    /// out to the peer it came from -- a total echo-suppression regression
    /// that an order-only assertion cannot see, since arm still happens
    /// before write either way.
    func testIncomingClipArmsSuppressionWithPlainTextBytesNotTheTimestampPrefixedFramePayload() {
        var armedWith: Data?
        let framePayload = ClipPayload(ts: 555, text: "hello").encode()

        handleFrame(Frame(type: .clip, payload: framePayload),
                    send: { _ in XCTFail("a clip frame must never trigger a reply") },
                    noteWrittenLocally: { armedWith = $0 },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(armedWith, Data("hello".utf8),
                       "must arm with the plain text PasteboardWatcher.poll() will read back " +
                       "from the pasteboard, not the ts-prefixed frame payload")
        XCTAssertNotEqual(armedWith, framePayload,
                          "arming with the ts-prefixed payload would make EchoGuard's digest never " +
                          "match poll()'s later read of plain text, silently disabling suppression")
    }

    func testEmptyClipTouchesNeitherSuppressionNorThePasteboard() {
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clip, payload: Data()),
                    send: { _ in },
                    noteWrittenLocally: { _ in XCTFail("must not arm for an undecodable clip") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
        XCTAssertNil(status.snapshot().lastReceivedAt)
    }

    /// Distinct failure mode from the empty-payload case above: this payload
    /// DECODES fine (a valid 8-byte ts prefix, no text) but carries empty
    /// text. `ClipPayload.decode` cannot reject this on its own -- an empty
    /// string is a valid `String` -- so `handleFrame` must still refuse to
    /// apply it, mirroring the pre-v2 `!frame.payload.isEmpty` guard and the
    /// watcher's own refusal to ever SEND an empty clip.
    func testDecodableButEmptyTextClipTouchesNeitherSuppressionNorThePasteboard() {
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let emptyTextPayload = ClipPayload(ts: 5, text: "").encode()

        handleFrame(Frame(type: .clip, payload: emptyTextPayload),
                    send: { _ in },
                    noteWrittenLocally: { _ in XCTFail("must not arm for empty text") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
    }

    func testNonUTF8ClipTouchesNeitherSuppressionNorThePasteboard() {
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        var invalidUTF8 = Data(repeating: 0, count: ClipPayloadConstants.timestampBytes) // a valid ts prefix
        invalidUTF8.append(contentsOf: [0xFF, 0xFE, 0xFD])                               // undecodable tail

        handleFrame(Frame(type: .clip, payload: invalidUTF8),
                    send: { _ in },
                    noteWrittenLocally: { _ in XCTFail("must not arm for undecodable text") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
    }

    // MARK: - Contract 2 (new in Task 9): the peer's timestamp is what gets stored

    /// The assertion the persistent store's whole design exists to make
    /// possible: stamping applied content with `now` instead of the peer's
    /// `ts` would make it look freshly copied here and win the next
    /// reconciliation against the machine it actually came from. `424242.0`
    /// is picked far from wall-clock time specifically so "came from the
    /// frame" and "came from `now`" cannot be confused by coincidence.
    func testIncomingClipStoresThePeersTimestampNotNow() throws {
        let store = tempClipStateStore()
        let peersTimestamp = 424242.0
        let framePayload = ClipPayload(ts: peersTimestamp, text: "peer's clip").encode()

        handleFrame(Frame(type: .clip, payload: framePayload),
                    send: { _ in }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        let stored = store.load()
        XCTAssertEqual(stored?.ts, peersTimestamp, "must store the PEER's ts, never now")
        XCTAssertEqual(stored?.sha256, sha256Hex(Data("peer's clip".utf8)))
    }

    // MARK: - Contract 3: a received hello always produces a reply

    func testMatchingHelloProducesExactlyOneReplyAndReportsUp() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) },
                    noteWrittenLocally: { _ in XCTFail("a hello must not touch the pasteboard") },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.filter { $0.type == .hello }.count, 1)
        XCTAssertEqual(sent.first?.type, .hello)
        XCTAssertEqual(status.snapshot().state, .up)
    }

    func testMismatchedHelloStillProducesExactlyOneReply() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let mismatched = Data(#"{"protocol":999,"agent":"9.9.9"}"#.utf8)

        handleFrame(Frame(type: .hello, payload: mismatched),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1,
                       "must reply even on a mismatch -- the peer noticing the same mismatch on its " +
                       "own side and exiting is the only mechanism that actually closes the channel, " +
                       "since Channel exposes no force-close from this file")
        XCTAssertEqual(sent.first?.type, .hello)
        XCTAssertEqual(status.snapshot().state, .down)
    }

    func testMalformedHelloStillProducesExactlyOneReply() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .hello, payload: Data("not json".utf8)),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
    }

    // A spy that only checks "a hello was sent" could still pass against
    // code that replies with the wrong payload. Decodes the actual reply
    // and checks it carries our own version, independent of whatever the
    // peer declared.
    func testTheReplyAlwaysCarriesOurOwnProtocolVersion() {
        var sent: [Frame] = []
        handleFrame(Frame(type: .hello, payload: Data(#"{"protocol":999}"#.utf8)),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        guard let reply = sent.first(where: { $0.type == .hello }), let decoded = decodeHello(reply.payload) else {
            return XCTFail("expected a decodable hello reply")
        }
        XCTAssertEqual(decoded.version, ProtocolConstants.version)
    }

    // MARK: - Contract 4 (new in Task 9): clip-state is announced once, only on a matched hello

    /// Per the design spec: sent exactly once per connection, immediately
    /// after hello.
    func testMatchedHelloSendsClipStateAnnouncementExactlyOnce() {
        var sent: [Frame] = []

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.filter { $0.type == .clipState }.count, 1,
                       "a matched hello must announce our clip-state exactly once")
    }

    /// The strongest form of "sent once, not again": the SAME gate instance
    /// (as `wireAgent` would use across the whole connection) is driven
    /// through two matched hellos -- the ordinary protocol never sends a
    /// second one, but the gate, not that assumption, is what must prevent
    /// a second announcement. A test that only exercises a single call
    /// cannot distinguish "the code checks a gate" from "the code just
    /// happens to run once in this test."
    func testASecondMatchedHelloInTheSameConnectionDoesNotAnnounceAgain() {
        var sent: [Frame] = []
        let store = tempClipStateStore()
        let announcement = ClipStateAnnouncement()
        let pasteboard = RecordingPasteboard()

        for _ in 0..<2 {
            handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                        send: { sent.append($0) }, noteWrittenLocally: { _ in },
                        pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                        log: tempLog(), clipStateStore: store, clipStateAnnouncement: announcement)
        }

        XCTAssertEqual(sent.filter { $0.type == .clipState }.count, 1,
                       "the same gate across two matched hellos in one connection must still " +
                       "announce only once")
    }

    func testMismatchedHelloDoesNotAnnounceClipState() {
        var sent: [Frame] = []
        let mismatched = Data(#"{"protocol":999,"agent":"9.9.9"}"#.utf8)

        handleFrame(Frame(type: .hello, payload: mismatched),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.allSatisfy { $0.type != .clipState },
                      "a connection doomed by a version mismatch must not announce")
    }

    /// Proves the announcement is specific to a matched hello, not a side
    /// effect of frame-handling in general: neither an incoming `.clip` nor
    /// an incoming `.clipState` may itself claim the gate or send a
    /// `.clipState` frame.
    func testOnlyAMatchedHelloTriggersClipStateAnnouncement() throws {
        let announcement = ClipStateAnnouncement()
        let store = tempClipStateStore()
        var sent: [Frame] = []

        handleFrame(Frame(type: .clip, payload: ClipPayload(ts: 1, text: "x").encode()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: announcement)

        handleFrame(Frame(type: .clipState, payload: try ClipState(sha256: nil, ts: 0).encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: announcement)

        XCTAssertTrue(sent.allSatisfy { $0.type != .clipState },
                      "neither a .clip nor a .clipState input frame may itself trigger the announcement")
        XCTAssertFalse(announcement.sent, "the gate must remain unclaimed -- only a matched hello claims it")
    }

    // MARK: - Contract 5 (new in Task 9): resolving a peer's clip-state announcement

    /// `resolveFreshness`'s `waitForPeer` outcome: the peer is fresher, so we
    /// wait. Conflating this with `doNothing` would be harmless here, but
    /// the point of a resend would be to CLOBBER a fresher peer -- exactly
    /// the defect this whole design exists to prevent.
    func testLosingClipStateWithPeerFresherProducesNoSend() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "aa", ts: 5))
        var sent: [Frame] = []
        let peerState = ClipState(sha256: "bb", ts: 9) // peer fresher -> waitForPeer

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.isEmpty, "the peer is fresher -- we wait, we do not resend")
    }

    /// `resolveFreshness`'s `doNothing` outcome via equal hashes: "hashes
    /// equal" must mean "we agree", not "resend" -- conflating it with
    /// `sendMine` would ping-pong the same content back and forth forever.
    func testAgreeingClipStateWithEqualHashesProducesNoSend() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "aa", ts: 5))
        var sent: [Frame] = []
        let peerState = ClipState(sha256: "aa", ts: 999) // same hash -> doNothing regardless of ts

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.isEmpty, "hashes equal means we agree, not resend")
    }

    /// `resolveFreshness`'s `sendMine` outcome: a peer with no clipboard at
    /// all (also the fix for v1's documented loss of Mac copies made while
    /// the PC was off). The resulting clip must carry OUR stored `ts`, not
    /// `now` -- resending with `now` would perpetually refresh its age and
    /// let it win every future reconciliation regardless of what actually
    /// happens next.
    func testWinningClipStateProducesExactlyOneClipFrameCarryingOurStoredTimestamp() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "aa", ts: 777))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("current clip text".utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0) // peer empty -> sendMine

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .clip)
        guard let first = sent.first else { return }
        let decoded = try ClipPayload.decode(first.payload)
        XCTAssertEqual(decoded.ts, 777, "must carry OUR stored ts, not now")
        XCTAssertEqual(decoded.text, "current clip text")
    }

    /// Unlike the watcher's own local-change path, this branch reads the
    /// live pasteboard independently and, before this fix, applied no size
    /// bound at all: winning a reconciliation over content at or beyond the
    /// cap would build a `ClipPayload` whose encoded frame exceeds
    /// `FrameConstants.maxPayloadBytes`, and the peer's `Frame.decode`
    /// would reject it as oversized and drop the whole channel -- the same
    /// boundary `PasteboardTests.testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt`
    /// pins for the watcher's send path.
    func testWinningClipStateWithContentAtExactlyTheCapProducesNoSend() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "aa", ts: 777))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data(repeating: 0x61, count: FrameConstants.maxPayloadBytes)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.isEmpty,
                      "content of exactly the cap would encode to a frame 8 bytes over it")
    }

    /// The other half of the boundary: content that leaves exact room for
    /// the timestamp prefix must still be sent -- an over-trimmed fix would
    /// silently refuse to reconcile a win over content the wire format
    /// actually supports.
    func testWinningClipStateWithContentLeavingExactRoomForTheTimestampPrefixStillSends() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "aa", ts: 777))
        let pasteboard = RecordingPasteboard()
        let text = String(repeating: "a",
                          count: FrameConstants.maxPayloadBytes - ClipPayloadConstants.timestampBytes)
        pasteboard.textToRead = Data(text.utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(try ClipPayload.decode(sent[0].payload).text, text)
    }

    /// If the store's own load somehow returns nothing (the fallback path
    /// for a disk failure on an earlier save, never expected in ordinary
    /// operation), `handleFrame` must still resolve a real state from the
    /// live pasteboard rather than a bare nil-hash placeholder. A bare nil
    /// there would make BOTH sides resolve `waitForPeer` against each
    /// other's (correctly announced) state and silently lose the clip --
    /// exactly v1's bug, reintroduced through the fallback path instead of
    /// the main one.
    func testClipStateFallbackWhenStoreIsEmptyStillResolvesFromTheLivePasteboard() throws {
        let store = tempClipStateStore() // never saved to -- load() returns nil
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("what we actually hold".utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0) // peer empty -> sendMine, if mine resolves non-nil

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1,
                       "an empty store must not silently resolve to waitForPeer against a peer " +
                       "that also holds nothing to compare against -- that is a silent loss")
        guard let first = sent.first else { return }
        XCTAssertEqual(try ClipPayload.decode(first.payload).text, "what we actually hold")
    }

    // MARK: - Cross-language hash contract

    /// Pins the exact format `resolveStartupState`'s `sha256` field must use:
    /// lowercase hex, no separators, byte-identical to Python's
    /// `hashlib.sha256(data).hexdigest()`. `resolveStartupState` compares
    /// these strings with plain `==`, so a case or separator difference here
    /// would send every startup down the "hashes differ" branch -- the
    /// systematic clobber the persistent store exists to prevent. Verified
    /// against Python directly: `python3 -c "import hashlib;
    /// print(hashlib.sha256(b'hi').hexdigest())"`.
    func testSha256HexMatchesPythonsHexdigestFormatForAKnownVector() {
        XCTAssertEqual(sha256Hex(Data("hi".utf8)),
                       "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4")
    }

    // MARK: - announceClipState: the outgoing announcement, tested directly

    /// The load-bearing case, tested directly against `announceClipState`
    /// rather than through `handleFrame` because `handleFrame`'s `now`
    /// parameter is defaulted to the real clock: unchanged content since
    /// the last recorded state must keep its true recorded age, never `now`.
    func testAnnounceClipStateKeepsStoredTimestampWhenContentIsUnchanged() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("same".utf8)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(Data("same".utf8)), ts: 555))
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard, clipStateStore: store, now: 999_999)

        XCTAssertEqual(sent.count, 1)
        let decoded = try ClipState.decodePayload(sent[0].payload)
        XCTAssertEqual(decoded.ts, 555, "content unchanged since last recorded must keep its real age")
    }

    func testAnnounceClipStateUsesNowWhenContentChangedWhileApart() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("new content".utf8)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: "some-other-hash-entirely", ts: 111))
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard, clipStateStore: store, now: 999_999)

        let decoded = try ClipState.decodePayload(sent[0].payload)
        XCTAssertEqual(decoded.ts, 999_999, "content changed while apart -- only now is honest")
    }

    /// Persists what it announces, so a later `.clipState` comparison (or a
    /// crash immediately afterward) sees the reconciled value, not whatever
    /// was on disk before this connection began.
    func testAnnounceClipStatePersistsTheResolvedValue() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("fresh content".utf8)
        let store = tempClipStateStore()

        announceClipState(send: { _ in }, pasteboard: pasteboard, clipStateStore: store, now: 42)

        XCTAssertEqual(store.load(), ClipState(sha256: sha256Hex(Data("fresh content".utf8)), ts: 42))
    }

    /// Self-review: a local disk failure is not the peer's fault, and must
    /// not silently disable reconciliation for the connection. Forces a real
    /// save failure (a plain file occupying the path where the store needs
    /// to create a directory) rather than asserting this from reading the
    /// implementation, and confirms the announcement still goes out.
    func testAnnounceClipStateStillSendsWhenTheStoreCannotBeSaved() throws {
        let blockingFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-blocker-\(UUID().uuidString)")
        try Data("occupying this name".utf8).write(to: blockingFile)
        // The store's directory-creation step must fail: `blockingFile` is a
        // plain file, not a directory, sitting where the store needs one.
        let unsaveableStore = ClipStateStore(path: blockingFile.appendingPathComponent("clip-state.json").path)
        XCTAssertThrowsError(try unsaveableStore.save(ClipState(sha256: "aa", ts: 1)),
                             "test setup must actually force a save failure, or this test proves nothing")

        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("still send this".utf8)
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard,
                          clipStateStore: unsaveableStore, now: 1)

        XCTAssertEqual(sent.count, 1, "a local disk failure must not prevent the announcement from going out")
    }
}
