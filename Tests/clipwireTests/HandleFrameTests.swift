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
    /// Records every `write` call and, via `onWrite`, lets a test
    /// observe exactly when it happens relative to other calls -- the
    /// ORDER is what these tests pin, not merely that a write occurred.
    /// A spy that only checked occurrence would pass against code that
    /// armed the suppression after writing instead of before.
    ///
    /// Also conforms to `PasteboardReading` (`changeCount`/`read`),
    /// which `handleFrame`'s clip-state paths need: announcing our own
    /// state and answering a peer's announcement both require reading
    /// whatever the pasteboard currently holds, not only writing to it.
    final class RecordingPasteboard: PasteboardReading, PasteboardWriting {
        private(set) var writes: [(kind: ClipKind, data: Data)] = []
        var onWrite: (() -> Void)?
        var changeCount = 0
        var textToRead: Data?
        /// Only read when `textToRead` is nil, so the double applies the
        /// same text-wins rule the real board does -- and so a test can put
        /// an image on the pasteboard and drive the paths that now build an
        /// `.imageClip` frame from one (`handleFrame`'s `.sendMine` branch,
        /// and `announceClipState`).
        var imageToRead: Data?

        /// What was written as TEXT, decoded -- the shape most of this
        /// suite asserts on. Kept as a derived view rather than a second
        /// stored property so it cannot disagree with `writes`.
        var writtenTexts: [String] {
            writes.filter { $0.kind == .text }.map { String(decoding: $0.data, as: UTF8.self) }
        }

        /// How many times `read()` was called. A cost, not bookkeeping: on
        /// `SystemPasteboard` an image read pulls the board's TIFF
        /// representation and converts it to PNG, so asking twice about the
        /// same bytes converts a multi-megabyte screenshot twice.
        private(set) var reads = 0

        func read() -> (kind: ClipKind, data: Data)? {
            reads += 1
            if let textToRead, !textToRead.isEmpty { return (.text, textToRead) }
            if let imageToRead, !imageToRead.isEmpty { return (.image, imageToRead) }
            return nil
        }

        func write(kind: ClipKind, data: Data) {
            writes.append((kind, data))
            onWrite?()
        }
    }

    func tempStatusURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString).json")
    }

    func tempLogPath() -> String {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
    }

    func tempLog() -> Log {
        Log(path: tempLogPath())
    }

    /// Everything `Log` wrote to `path`, with its own ISO8601 stamp prefix
    /// (one token, then a single space) stripped, so a test can assert the
    /// exact message `handleFrame` asked for. Call `log.flush()` first --
    /// `line(_:)` only enqueues the write; `LogTests` pins that contract.
    func loggedMessages(at path: String) -> [String] {
        let contents = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        return contents.split(separator: "\n").map {
            String($0.drop(while: { $0 != " " }).dropFirst())
        }
    }

    /// 64 lowercase hex characters: the only shape `ClipState.decodePayload`
    /// accepts, and the only shape `sha256Hex` -- hence the wire -- ever
    /// produces. Obviously fake, but well-formed, so these tests exercise the
    /// same path a real digest does instead of one the protocol forbids. A
    /// peer hash that is NOT protocol-shaped now fails at the decode point,
    /// which would make every "no send expected" assertion below pass without
    /// resolveFreshness ever running. `hashA` sorts below `hashB`, which the
    /// hash tie-break depends on.
    static let hashA = String(repeating: "aa", count: 32)
    static let hashB = String(repeating: "bb", count: 32)

    /// A real, temp-path-backed store -- not a live production state
    /// directory -- mirroring `tempStatusURL()`/`tempLog()`'s existing
    /// pattern of injecting real-but-disposable dependencies rather than
    /// mocking file I/O.
    func tempClipStateStore() -> ClipStateStore {
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
                    noteWrittenLocally: { _, _ in order.append("arm") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(order, ["arm", "write"],
                       "suppression must be armed strictly before the pasteboard write -- reversed " +
                       "order would let the watcher observe the write before the suppression exists " +
                       "and bounce our own clip back to the peer")
        XCTAssertEqual(pasteboard.writtenTexts, ["hello"], "exactly one write, with the decoded text")
    }

    /// Distinct from the ordering test above: this pins WHAT is armed, not
    /// merely WHEN. `PasteboardWatcher.poll()` hashes the body
    /// `pasteboard.read()` returns -- the plain text alone, never a
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
                    noteWrittenLocally: { armedWith = $1 },
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
                    noteWrittenLocally: { _, _ in XCTFail("must not arm for an undecodable clip") },
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
                    noteWrittenLocally: { _, _ in XCTFail("must not arm for empty text") },
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
                    noteWrittenLocally: { _, _ in XCTFail("must not arm for undecodable text") },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
    }

    // MARK: - Contract 1b (final wave): an undecodable clip is logged, never dropped in silence

    /// `wl-paste` hands the PC agent raw bytes, `_local_change` hashes and
    /// sends them unchanged, and this side then fails to decode them. The
    /// drop itself is correct -- there is nothing valid to apply -- but
    /// with `try?` swallowing the error, nothing was written anywhere: not
    /// the pasteboard, not the store, not the log. So the two persistent
    /// stores disagree permanently, and on EVERY subsequent reconnect the
    /// PC resolves SEND_MINE (its ts is the newer one), re-sends the same
    /// bytes, and this side discards them again -- forever, with nothing
    /// logged on either machine. The repeat-forever property is what makes
    /// the invisibility, rather than the drop, the actual defect.
    func testAnUndecodableClipIsLogged() {
        let path = tempLogPath()
        let log = Log(path: path)
        var invalidUTF8 = Data(repeating: 0, count: ClipPayloadConstants.timestampBytes)
        invalidUTF8.append(contentsOf: [0xFF, 0xFE, 0xFD])

        handleFrame(Frame(type: .clip, payload: invalidUTF8),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(loggedMessages(at: path),
                       ["could not decode a clip from the peer: invalidUTF8"],
                       "the drop is correct; doing it invisibly is what makes it permanent")
    }

    /// The other way `ClipPayload.decode` now throws. Pinned separately so
    /// the log line is known to carry the REASON rather than a fixed string
    /// that happens to match one input -- a test with only the UTF-8 case
    /// would pass against an implementation that hardcoded "invalidUTF8".
    func testAClipWithANonFiniteTimestampIsLogged() {
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clip, payload: ClipPayload(ts: .nan, text: "x").encode()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(loggedMessages(at: path),
                       ["could not decode a clip from the peer: nonFiniteTimestamp"])
    }

    /// Empty text is deliberately NOT logged: it decoded fine and applying
    /// nothing is the correct, uneventful outcome, exactly as the PC
    /// agent's own `_write_clip` returns quietly for the same input. Only
    /// a decode FAILURE is a defect worth a line.
    func testAnEmptyClipIsDroppedInSilence() {
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clip, payload: ClipPayload(ts: 5, text: "").encode()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(loggedMessages(at: path), [])
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
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        let stored = store.load()
        XCTAssertEqual(stored?.ts, peersTimestamp, "must store the PEER's ts, never now")
        XCTAssertEqual(stored?.sha256, sha256Hex(Data("peer's clip".utf8)))
    }

    // MARK: - Contract 3: a received hello always produces a reply

    /// The `.up` half of this became `.clipboardPending` in the final wave
    /// -- see `testAMatchedHelloAloneDoesNotReportUp` for why. The reply
    /// count is what this test is actually for.
    func testMatchingHelloProducesExactlyOneReplyAndReportsClipboardPending() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) },
                    noteWrittenLocally: { _, _ in XCTFail("a hello must not touch the pasteboard") },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.filter { $0.type == .hello }.count, 1)
        XCTAssertEqual(sent.first?.type, .hello)
        XCTAssertEqual(status.snapshot().state, .clipboardPending)
    }

    func testMismatchedHelloStillProducesExactlyOneReply() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let mismatched = Data(#"{"protocol":999,"agent":"9.9.9"}"#.utf8)

        handleFrame(Frame(type: .hello, payload: mismatched),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        guard let reply = sent.first(where: { $0.type == .hello }), let decoded = decodeHello(reply.payload) else {
            return XCTFail("expected a decodable hello reply")
        }
        XCTAssertEqual(decoded.version, ProtocolConstants.version)
    }

    // MARK: - Contract 5 (new in Task 12): skew is measured from the hello, and only there

    /// `now` is injected rather than read from the wall clock: skew is a
    /// comparison against this side's clock, so a test using the real one
    /// could only assert vaguely, and a sleep would make it slower without
    /// making it deterministic.
    func testAMatchedHelloLogsTheSkewAgainstTheInjectedNow() {
        let path = tempLogPath()
        let log = Log(path: path)
        // Built from ProtocolConstants.version rather than a hardcoded
        // literal -- matches test_mainloop.py's own comment on the Python
        // side's equivalent hello builder. A literal "2" here was exactly
        // what this task's version bump to 3 broke: this hello must MATCH
        // to reach the skew-logging code this test is actually pinning, and
        // a stale literal silently turns it into a mismatch instead.
        let hello = Data(#"{"protocol":\#(ProtocolConstants.version),"agent":"0.1.0","sent_at":1000.0}"#.utf8)

        handleFrame(Frame(type: .hello, payload: hello),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 1000.5)

        log.flush()
        XCTAssertEqual(loggedMessages(at: path).filter { $0.contains("skew") },
                       ["peer clock skew 0.5s"])
    }

    func testABadlySkewedPeerWarns() {
        let path = tempLogPath()
        let log = Log(path: path)
        // See testAMatchedHelloLogsTheSkewAgainstTheInjectedNow: must match
        // ProtocolConstants.version, not repeat a hardcoded literal.
        let hello = Data(#"{"protocol":\#(ProtocolConstants.version),"agent":"0.1.0","sent_at":1000.0}"#.utf8)

        handleFrame(Frame(type: .hello, payload: hello),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 1060.0)

        log.flush()
        XCTAssertEqual(loggedMessages(at: path).filter { $0.contains("skew") },
                       ["peer clock skew 60.0s — over 5s, check the clock on both machines"])
    }

    /// A v1 peer, or any hand-built payload. Unmeasurable is not a protocol
    /// violation: nothing about skew is logged, and the hello is otherwise
    /// processed exactly as any other matched one.
    func testAHelloWithoutSentAtIsAcceptedInSilence() {
        let path = tempLogPath()
        let log = Log(path: path)
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        // Must match ProtocolConstants.version -- see
        // testAMatchedHelloLogsTheSkewAgainstTheInjectedNow.
        handleFrame(Frame(type: .hello,
                          payload: Data(#"{"protocol":\#(ProtocolConstants.version),"agent":"0.1.0"}"#.utf8)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: status,
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 1000.0)

        log.flush()
        XCTAssertEqual(loggedMessages(at: path).filter { $0.contains("skew") }, [])
        // Not `.down`, rather than specifically `.up`: what this pins is
        // that an unmeasurable peer clock is not a protocol violation. The
        // promotion to `.up` now waits for the peer's clip-state frame.
        XCTAssertEqual(status.snapshot().state, .clipboardPending,
                       "a missing sent_at must not be treated as a mismatch")
    }

    func testAMismatchedHelloDoesNotReportSkew() {
        let path = tempLogPath()
        let log = Log(path: path)
        let hello = Data(#"{"protocol":999,"agent":"9.9.9","sent_at":1000.0}"#.utf8)

        handleFrame(Frame(type: .hello, payload: hello),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 1060.0)

        log.flush()
        XCTAssertEqual(loggedMessages(at: path).filter { $0.contains("skew") }, [],
                       "a clock reading from a peer we cannot talk to is noise next to the mismatch")
    }

    /// The anti-requirement, and the whole reason skew is measured from the
    /// hello's `sent_at` rather than the clip's own timestamp: a clip
    /// legitimately copied yesterday is a day old, and warning on THAT would
    /// fire on nearly every handshake and teach everyone to ignore the log.
    func testAnOldClipAfterACurrentHelloDoesNotWarn() {
        let path = tempLogPath()
        let log = Log(path: path)
        let now: Double = 1_000_000
        // Must match ProtocolConstants.version -- see
        // testAMatchedHelloLogsTheSkewAgainstTheInjectedNow.
        let hello = Data(#"{"protocol":\#(ProtocolConstants.version),"agent":"0.1.0","sent_at":1000000.0}"#.utf8)
        let store = tempClipStateStore()

        handleFrame(Frame(type: .hello, payload: hello),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement(), now: now)
        handleFrame(Frame(type: .clip,
                          payload: ClipPayload(ts: now - 86400, text: "copied yesterday").encode()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement(), now: now)

        log.flush()
        XCTAssertEqual(loggedMessages(at: path).filter { $0.contains("skew") },
                       ["peer clock skew 0.0s"],
                       "the clip's age must not be measured as clock skew")
    }

    // MARK: - Contract 4 (new in Task 9): clip-state is announced once, only on a matched hello

    /// Per the design spec: sent exactly once per connection, immediately
    /// after hello.
    func testMatchedHelloSendsClipStateAnnouncementExactlyOnce() {
        var sent: [Frame] = []

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
                        send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: announcement)

        handleFrame(Frame(type: .clipState, payload: try ClipState(sha256: nil, ts: 0, kind: nil).encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
        let held = Data("something to wrongly send".utf8)
        let store = tempClipStateStore()
        // Real content on the pasteboard, and a stored hash that actually
        // MATCHES it: a wrongly-resolved sendMine would otherwise stop at one
        // of that branch's own guards -- since Task 11 the first of them is
        // the verification, which a placeholder hash fails -- and this
        // assertion would hold for the wrong reason.
        try store.save(ClipState(sha256: sha256Hex(held), ts: 5, kind: .text))
        var sent: [Frame] = []
        let peerState = ClipState(sha256: Self.hashB, ts: 9, kind: .text) // peer fresher -> waitForPeer
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = held

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.isEmpty, "the peer is fresher -- we wait, we do not resend")
    }

    /// `resolveFreshness`'s `doNothing` outcome via equal hashes: "hashes
    /// equal" must mean "we agree", not "resend" -- conflating it with
    /// `sendMine` would ping-pong the same content back and forth forever.
    func testAgreeingClipStateWithEqualHashesProducesNoSend() throws {
        // See the test above: without real content the pasteboard actually
        // holds -- and a stored hash that matches it -- a wrongly-resolved
        // sendMine stops at one of that branch's own guards and this would
        // pass regardless. The hash goes on BOTH sides here, since equal
        // hashes are what the doNothing outcome under test turns on.
        let held = Data("something to wrongly send".utf8)
        let agreed = sha256Hex(held)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: agreed, ts: 5, kind: .text))
        var sent: [Frame] = []
        let peerState = ClipState(sha256: agreed, ts: 999, kind: .text) // same hash -> doNothing regardless of ts
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = held

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertTrue(sent.isEmpty, "hashes equal means we agree, not resend")
    }

    /// `resolveFreshness`'s `sendMine` outcome: a peer with no clipboard at
    /// all (also the fix for v1's documented loss of Mac copies made while
    /// the PC was off). The resulting clip must carry OUR stored `ts`, not
    /// `now` -- resending with `now` would perpetually refresh its age and
    /// let it win every future reconciliation regardless of what actually
    /// happens next.
    ///
    /// The stored hash is the real digest of what the pasteboard double
    /// returns: since Task 11 the branch verifies the two against each other
    /// before sending, so a placeholder hash here would make this test prove
    /// only that the verification works.
    func testWinningClipStateProducesExactlyOneClipFrameCarryingOurStoredTimestamp() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(Data("current clip text".utf8)), ts: 777, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("current clip text".utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil) // peer empty -> sendMine

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .clip)
        guard let first = sent.first else { return }
        let decoded = try ClipPayload.decode(first.payload)
        XCTAssertEqual(decoded.ts, 777, "must carry OUR stored ts, not now")
        XCTAssertEqual(decoded.text, "current clip text")
    }

    /// Replaces `testWinningClipStateWithAnImageOnThePasteboardProducesNoSend`,
    /// which asserted the opposite: until this task the branch built only a
    /// `ClipPayload` -- the TEXT codec -- so a verified image had to fall out
    /// silently rather than reach the wire as mojibake. It now has the image
    /// codec, and the PC agent's `_resolve_clip_state` is the worked example
    /// this mirrors.
    ///
    /// This branch is REACHABLE for an image only because of Task 8: an
    /// image-only pasteboard used to read back as nothing, so it resolved a
    /// nil hash and could never win a reconciliation; it now resolves a real
    /// `(hash, ts, .image)` state.
    ///
    /// `mine.ts`, not `now` -- and this is the property Task 11's
    /// verification exists to make safe: the content has not changed, only
    /// been re-announced, so re-stamping it would let it win every future
    /// reconciliation regardless of what happens next. The stored hash is
    /// the image's REAL digest, so the send is the branch's own doing rather
    /// than the verification's -- asserted directly by the absence of the
    /// verification's log line.
    func testWinningClipStateWithAnImageSendsAnImageClipCarryingOurStoredTimestamp() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(png), ts: 777, kind: .image))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = png
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil) // peer empty -> sendMine
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .imageClip,
                       "an image must go out under the image codec, never as a text clip")
        guard let first = sent.first else { return }
        let decoded = try ImagePayload.decode(first.payload)
        XCTAssertEqual(decoded.ts, 777, "must carry OUR stored ts, not now")
        XCTAssertEqual(decoded.png, png)
        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed before the send"),
                       "the pasteboard holds exactly what we announced")
    }

    /// The image half of the size boundary, and it is deliberately NOT shaped
    /// like the text half above: an image at exactly `maxImageBytes` is SENT,
    /// where text at exactly `maxTextBytes` is skipped. That asymmetry is the
    /// whole reason the three caps were separated. `maxImageBytes` bounds the
    /// IMAGE, so an image at exactly the limit encodes to a payload eight
    /// bytes over it -- 4,194,312 -- which still fits `maxPayloadBytes`
    /// (8,388,608) with 4 MiB to spare. Adding the timestamp to this guard,
    /// as the text guard correctly does, would refuse the maximum-size
    /// screenshot the protocol explicitly makes room for.
    func testWinningClipStateWithAnImageAtExactlyTheImageLimitStillSends() throws {
        let png = Data(repeating: 0x89, count: FrameConstants.maxImageBytes)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(png), ts: 777, kind: .image))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = png
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.payload.count,
                       FrameConstants.maxImageBytes + ClipPayloadConstants.timestampBytes,
                       "sanity check on the boundary itself -- over maxImageBytes, well under the frame cap")
    }

    /// The image twin of `testWinningClipStateWithOversizedContentIsLoggedWithItsSize`.
    /// The verdict clause is byte-identical to the PC agent's own three image
    /// skips: one sentence per limit, so the sites cannot drift into several
    /// names for it -- the convention the frame-cap and skew lines already
    /// follow, which has caught drift twice on this project.
    func testWinningClipStateWithAnOversizedImageIsLoggedWithItsSize() throws {
        let oversized = FrameConstants.maxImageBytes + 1
        let png = Data(repeating: 0x89, count: oversized)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(png), ts: 777, kind: .image))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = png
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil)
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty)
        XCTAssertTrue(loggedMessages(at: path)
                        .contains("skipping an image of \(oversized) bytes: over the image limit"),
                      "got: \(loggedMessages(at: path))")
    }

    // MARK: - Task 11: the send branch verifies before it sends

    /// `mine` says text with one hash; by the time we send, the pasteboard
    /// holds an image. Sending it under the announced timestamp is a clobber
    /// the peer cannot detect -- and the watcher will carry the real change a
    /// moment later on its own path, so nothing is lost by staying quiet.
    ///
    /// Both halves of the verification are wrong here at once (the kind and
    /// the hash), which is the honest shape of the race: whatever replaced
    /// the announced content is not required to be of the same kind.
    func testTheSendBranchStaysSilentWhenThePasteboardMovedOn() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashA, ts: 5000, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        var sent: [Frame] = []
        // Older than ours, so we resolve sendMine.
        let peerState = ClipState(sha256: Self.hashB, ts: 1000, kind: .text)
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty, "the pasteboard no longer holds what we announced")
        XCTAssertTrue(loggedMessages(at: path).contains("clipboard changed before the send"),
                      "got: \(loggedMessages(at: path))")
    }

    /// The kind still matches and only the content changed -- the ordinary
    /// shape of the race, a second copy landing between the announcement and
    /// this frame. A verification that compared only the kind would pass this
    /// and send the wrong text under the announced timestamp.
    func testTheSendBranchStaysSilentWhenOnlyTheHashMovedOn() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashA, ts: 5000, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("whatever the user copied since".utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: Self.hashB, ts: 1000, kind: .text)
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty, "the announced hash is not what the pasteboard offers")
        XCTAssertTrue(loggedMessages(at: path).contains("clipboard changed before the send"),
                      "got: \(loggedMessages(at: path))")
    }

    /// A pasteboard that now reads back as nothing at all is the same class
    /// of failure as one holding different content: it does not hold what we
    /// announced.
    func testTheSendBranchStaysSilentWhenThePasteboardEmptied() throws {
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashA, ts: 5000, kind: .text))
        let pasteboard = RecordingPasteboard() // nothing to read
        var sent: [Frame] = []
        let peerState = ClipState(sha256: Self.hashB, ts: 1000, kind: .text)
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty)
        XCTAssertTrue(loggedMessages(at: path).contains("clipboard changed before the send"),
                      "got: \(loggedMessages(at: path))")
    }

    /// The positive half: without it the three tests above pass against a
    /// branch that never sends anything at all.
    func testTheSendBranchSendsWhenThePasteboardStillMatches() throws {
        let body = Data("still here".utf8)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(body), ts: 5000, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = body
        var sent: [Frame] = []
        let peerState = ClipState(sha256: Self.hashB, ts: 1000, kind: .text)
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .clip)
        guard let first = sent.first else { return }
        let decoded = try ClipPayload.decode(first.payload)
        XCTAssertEqual(decoded.ts, 5000)
        XCTAssertEqual(decoded.text, "still here")
        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed before the send"))
    }

    /// Unlike the watcher's own local-change path, this branch reads the
    /// live pasteboard independently and, before this fix, applied no size
    /// bound at all: winning a reconciliation over content at or beyond the
    /// TEXT limit would build a `ClipPayload` whose encoded frame exceeds
    /// `FrameConstants.maxTextBytes` -- the same
    /// boundary `PasteboardTests.testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt`
    /// pins for the watcher's send path. Since Task 4, `maxTextBytes` (this
    /// guard) and `maxPayloadBytes` (the wire's frame cap, enforced only by
    /// `Frame.decode`) are separate constants that happen to still share
    /// this number.
    ///
    /// The stored hash is the real digest of the oversized content, not a
    /// placeholder: since Task 11 the branch verifies before it sends, and a
    /// placeholder would make this test pass on the verification's silence
    /// while the cap it exists for went untested.
    func testWinningClipStateWithContentAtExactlyTheCapProducesNoSend() throws {
        let oversized = Data(repeating: 0x61, count: FrameConstants.maxTextBytes)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(oversized), ts: 777, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = oversized
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
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
        let text = String(repeating: "a",
                          count: FrameConstants.maxTextBytes - ClipPayloadConstants.timestampBytes)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(Data(text.utf8)), ts: 777, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data(text.utf8)
        var sent: [Frame] = []
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(try ClipPayload.decode(sent[0].payload).text, text)
    }

    /// A user whose large paste wins a reconciliation but can't actually be
    /// sent has nothing to look at otherwise -- matches the Python agent's
    /// existing "skipping a clip of N bytes: over the text limit" line for
    /// the same cap, and `PasteboardTests.testOversizedClipIsLoggedWithItsSize`
    /// for the watcher's own send-side guard.
    func testWinningClipStateWithOversizedContentIsLoggedWithItsSize() throws {
        let oversized = FrameConstants.maxTextBytes
        let content = Data(repeating: 0x61, count: oversized)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(content), ts: 777, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = content
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil)
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        let contents = try? String(contentsOfFile: logPath, encoding: .utf8)
        XCTAssertEqual(contents?.contains("skipping a clip of \(oversized) bytes"), true,
                       "expected the skip to be logged with its size; got: \(contents ?? "<unreadable>")")
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
        let peerState = ClipState(sha256: nil, ts: 0, kind: nil) // peer empty -> sendMine, if mine resolves non-nil

        handleFrame(Frame(type: .clipState, payload: try peerState.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.count, 1,
                       "an empty store must not silently resolve to waitForPeer against a peer " +
                       "that also holds nothing to compare against -- that is a silent loss")
        guard let first = sent.first else { return }
        XCTAssertEqual(try ClipPayload.decode(first.payload).text, "what we actually hold")
    }

    // MARK: - Task 13: one function turns a kind into a frame

    /// The kind-to-codec mapping, pinned where it lives rather than at each
    /// of its two call sites. Both of them -- `handleLocalChange` (a local
    /// observation) and `handleFrame`'s `.sendMine` branch (a reconciliation
    /// win) -- previously built a `ClipPayload` inline, which is how the
    /// second one came to send only text: two inline copies of "which codec
    /// goes with which kind" is exactly the drift this project has already
    /// been bitten by twice across the two languages, and here it would be
    /// within one file.
    func testOutgoingFrameUsesTheClipCodecForTextAndTheImageCodecForAnImage() throws {
        let text = try outgoingClipFrame(kind: .text, body: Data("hello".utf8), ts: 1000)
        XCTAssertEqual(text.type, .clip)
        XCTAssertEqual(try ClipPayload.decode(text.payload), ClipPayload(ts: 1000, text: "hello"))

        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let image = try outgoingClipFrame(kind: .image, body: png, ts: 2000)
        XCTAssertEqual(image.type, .imageClip)
        let decoded = try ImagePayload.decode(image.payload)
        XCTAssertEqual(decoded.ts, 2000)
        XCTAssertEqual(decoded.png, png)
    }

    /// PNG bytes are not valid UTF-8, so the text codec would not merely
    /// mislabel them -- `String(decoding:as:UTF8.self)` substitutes U+FFFD
    /// for every byte it cannot read, and the peer would apply that
    /// substituted text to its clipboard as a text clip. Pinned as bytes
    /// rather than as a frame type so a future change that kept the
    /// `.imageClip` type but routed the body through the wrong codec still
    /// fails here.
    func testAnImageBodySurvivesTheOutgoingFrameByteForByte() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0xFF, 0xFE, 0x00, 0x80, 0x1A, 0x0A])
        let frame = try outgoingClipFrame(kind: .image, body: png, ts: 1)
        XCTAssertEqual(try ImagePayload.decode(frame.payload).png, png)
    }

    // MARK: - Task 13: the local-change path, with a send spy

    /// `wireAgent`'s `watcher.onChange` used to build its frame inline and
    /// hand it straight to `channel.send`, which has no test-observable hook
    /// at all -- `Channel` is `final`, its `stdinPipe` is private and set
    /// only inside `attempt()`, so a send with no live ssh process reports
    /// `onSent(false)` and leaves nothing behind. The entire outbound half
    /// could have been wired to the text codec for both kinds and every
    /// other test in this suite would still pass. Pulled out for the same
    /// reason `handleFrame` and `announceClipState` were, and documented
    /// there: a `send` spy can verify the exact frame.
    func testALocalImageChangeSendsAnImageClipCarryingTheObservationTimestamp() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        var sent: [Frame] = []

        handleLocalChange(kind: .image, body: png, observedAt: 4242,
                          send: { sent.append($0) },
                          clipStateStore: tempClipStateStore(), log: tempLog())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .imageClip)
        guard let first = sent.first else { return }
        let decoded = try ImagePayload.decode(first.payload)
        XCTAssertEqual(decoded.ts, 4242, "the moment of OBSERVATION, not of the send")
        XCTAssertEqual(decoded.png, png)
    }

    /// The text half, unchanged in behaviour by this task and asserted so it
    /// stays that way: generalising this path to both kinds is exactly the
    /// edit that could quietly change the frame type of every text clip.
    func testALocalTextChangeStillSendsAPlainClipFrame() throws {
        var sent: [Frame] = []

        handleLocalChange(kind: .text, body: Data("typed by the user".utf8), observedAt: 4242,
                          send: { sent.append($0) },
                          clipStateStore: tempClipStateStore(), log: tempLog())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .clip)
        guard let first = sent.first else { return }
        XCTAssertEqual(try ClipPayload.decode(first.payload),
                       ClipPayload(ts: 4242, text: "typed by the user"))
    }

    /// The store must record what we hold and how old it is, under the kind
    /// it actually is -- independent of whether the send below it ever
    /// reaches the peer. A hardcoded `.text` here would announce a PNG's
    /// digest as text on the next reconnect, and the peer believes it:
    /// `decode_clip_state` accepts both kinds, so nothing rejects it on
    /// arrival.
    func testALocalImageChangePersistsTheImageKindAndItsObservationTimestamp() {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let store = tempClipStateStore()

        handleLocalChange(kind: .image, body: png, observedAt: 4242,
                          send: { _ in }, clipStateStore: store, log: tempLog())

        XCTAssertEqual(store.load(), ClipState(sha256: sha256Hex(png), ts: 4242, kind: .image))
    }

    /// The save is best-effort and the send must not depend on it: a local
    /// disk failure is not the peer's fault, and gating the send behind it
    /// would silently disable Mac-to-PC sync for as long as the state
    /// directory is unwritable. Same rule `announceClipState` follows.
    func testALocalChangeStillSendsWhenTheStoreCannotBeSaved() throws {
        var sent: [Frame] = []
        let path = tempLogPath()
        let log = Log(path: path)

        handleLocalChange(kind: .image, body: Data([0x89, 0x50]), observedAt: 1,
                          send: { sent.append($0) },
                          clipStateStore: try unsaveableStore(), log: log)
        log.flush()

        XCTAssertEqual(sent.count, 1, "a disk failure must not stop the frame")
        XCTAssertEqual(persistFailures(at: path).count, 1, "and must not be silent either")
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

    /// Closes a gap the test above cannot: comparing the stored hash against
    /// `sha256Hex(Data("hi".utf8))` itself would still pass even if
    /// `handleFrame` hashed the WRONG bytes (`frame.payload`, the
    /// timestamp-prefixed wire payload, instead of `textData`) -- as long as
    /// it did so consistently with `sha256Hex`'s own behavior on whatever it
    /// was given. This pins the LITERAL, independently-verified digest at
    /// the actual call site instead. See fixtures/hashes.json, read by both
    /// suites (FixtureTests.testSha256HexMatchesSharedVectors and Python's
    /// test_fixtures.py::TestHashFixtures), so this exact vector cannot
    /// drift between them.
    func testIncomingClipStoresTheLiteralKnownHashForAPinnedVector() throws {
        let store = tempClipStateStore()
        let framePayload = ClipPayload(ts: 1, text: "hi").encode()

        handleFrame(Frame(type: .clip, payload: framePayload),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(store.load()?.sha256,
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
        try store.save(ClipState(sha256: sha256Hex(Data("same".utf8)), ts: 555, kind: .text))
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard, clipStateStore: store, log: tempLog(), now: 999_999)

        XCTAssertEqual(sent.count, 1)
        let decoded = try ClipState.decodePayload(sent[0].payload)
        XCTAssertEqual(decoded.ts, 555, "content unchanged since last recorded must keep its real age")
    }

    func testAnnounceClipStateUsesNowWhenContentChangedWhileApart() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("new content".utf8)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashB, ts: 111, kind: .text))
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard, clipStateStore: store, log: tempLog(), now: 999_999)

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

        announceClipState(send: { _ in }, pasteboard: pasteboard, clipStateStore: store, log: tempLog(), now: 42)

        XCTAssertEqual(store.load(), ClipState(sha256: sha256Hex(Data("fresh content".utf8)), ts: 42, kind: .text))
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
        XCTAssertThrowsError(try unsaveableStore.save(ClipState(sha256: "aa", ts: 1, kind: .text)),
                             "test setup must actually force a save failure, or this test proves nothing")

        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("still send this".utf8)
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard,
                          clipStateStore: unsaveableStore, log: tempLog(), now: 1)

        XCTAssertEqual(sent.count, 1, "a local disk failure must not prevent the announcement from going out")
    }

    // MARK: - Final wave: reconcile against what we announced, not a re-derivation

    /// The `.clipState` case re-derived `mine` from the live pasteboard
    /// whenever `clipStateStore.load()` came back nil, stamping `now` on it.
    /// Since every save site swallows its failure, an unwritable state
    /// directory reaches that path silently -- and the re-derived value is
    /// not the one this connection ANNOUNCED to this same peer moments
    /// earlier. It is strictly newer, because `now` has moved on, so a peer
    /// that is genuinely fresher than what we announced still loses, and we
    /// clobber it with older content under an invented age.
    ///
    /// Driven end to end through `handleFrame` with one shared
    /// `ClipStateAnnouncement`, exactly as a real connection does it: a
    /// matched hello announces, then the peer's own clip-state arrives. The
    /// peer's `ts` sits deliberately BETWEEN the announced `ts` and the `now`
    /// the re-derivation would use, so the two implementations disagree about
    /// who wins rather than merely about a timestamp's value.
    func testAPeersClipStateResolvesAgainstWhatWeAnnouncedWhenTheStoreIsUnreadable() throws {
        let store = try unsaveableStore()
        let announcement = ClipStateAnnouncement()
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("what we hold".utf8)
        var sent: [Frame] = []

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 1000)

        let announced = sent.filter { $0.type == .clipState }
        XCTAssertEqual(announced.count, 1, "the hello must have produced an announcement")
        XCTAssertEqual(try ClipState.decodePayload(announced[0].payload).ts, 1000)
        XCTAssertNil(store.load(), "the store must really be unreadable, or this proves nothing")
        sent.removeAll()

        // The peer is fresher than what we announced (2000 > 1000) but older
        // than the clock a re-derivation would stamp (2000 < 3000).
        let peer = ClipState(sha256: Self.hashB, ts: 2000, kind: .text)
        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 3000)

        XCTAssertEqual(sent.filter { $0.type == .clip }, [],
                       "the peer is fresher than the state we put on the wire, so it wins -- " +
                       "re-deriving our own from `now` invents an age nobody was told about " +
                       "and clobbers a peer that should have won")
    }

    // MARK: - The other half of that precedence: a readable store outranks what we announced

    /// The Mac keeps `clipStateStore.load()` AHEAD of
    /// `clipStateAnnouncement.announced`, while the PC agent passes its
    /// just-computed pair into `_resolve_clip_state(peer, mine=announced)`
    /// outright. That asymmetry is deliberate, and nothing pinned this half
    /// of it -- so a refactor "restoring consistency" between the two sides
    /// could reorder these two and silently reintroduce a clobber.
    ///
    /// What makes it right HERE and not there is a window that exists on
    /// exactly one of the two machines. On this side `watcher.onChange`
    /// calls `persistClipState` from `PasteboardWatcher`'s timer thread,
    /// while the `.clipState` case runs on the channel thread arbitrarily
    /// later than the `.hello` that announced -- so a genuine local copy can
    /// land in between. Resolving against `announced` in that window is the
    /// clobber: a peer that beats a stale `announced` wins, and its clip
    /// overwrites a NEWER local change that the peer is itself about to
    /// receive and apply. `load()` first turns that into a harmless
    /// duplicate instead. On the PC the window does not exist by
    /// construction: `_resolve_clip_state` runs inside
    /// `clipboard_became_ready`'s own call frame, a few lines after
    /// `announce_clip_state`, and `self._watcher` is still None until later
    /// in that same method -- there is no observer thread yet to race.
    ///
    /// Driven as two `handleFrame` calls with a store write in between,
    /// which reproduces that ordering deterministically -- no thread, no
    /// timer, no sleep. The peer's `ts` sits deliberately BETWEEN the two:
    /// fresher than what we announced (2000 > 1000), staler than what we
    /// now hold (2000 < 3000), so the two precedence orders disagree about
    /// WHO WINS rather than merely about a timestamp's value. The asserted
    /// `ts` on the sent clip pins the third order too: going straight to a
    /// re-derivation would send under `now` (4000) instead.
    func testALocalChangeAfterOurAnnouncementOutranksWhatWeAnnounced() throws {
        let store = tempClipStateStore()
        let announcement = ClipStateAnnouncement()
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("what we held at announce time".utf8)
        var sent: [Frame] = []

        // The hello, and this connection's one-shot announcement: ts 1000.
        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 1000)

        let announced = sent.filter { $0.type == .clipState }
        XCTAssertEqual(announced.count, 1, "the hello must have produced an announcement")
        XCTAssertEqual(try ClipState.decodePayload(announced[0].payload).ts, 1000,
                       "the announced pair is what a wrong precedence order would resolve against")
        sent.removeAll()

        // The watcher thread observing a local copy, and persisting it --
        // `wireAgent`'s `watcher.onChange` does exactly this save, off the
        // channel thread, so it can land at any point before the frame below.
        let copied = "copied while the peer's frame was still in flight"
        pasteboard.textToRead = Data(copied.utf8)
        try store.save(ClipState(sha256: sha256Hex(Data(copied.utf8)), ts: 3000, kind: .text))
        XCTAssertEqual(store.load()?.ts, 3000,
                       "the local change must really have replaced the announced value on disk, " +
                       "or this test proves nothing")

        let peer = ClipState(sha256: Self.hashB, ts: 2000, kind: .text)
        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 4000)

        let clips = sent.filter { $0.type == .clip }
        XCTAssertEqual(clips.count, 1,
                       "we hold something newer than the peer announced, so we send -- resolving " +
                       "against the stale announced pair would make us wait while the peer " +
                       "clobbers a local change it has never seen")
        guard let clip = clips.first else { return }
        let decoded = try ClipPayload.decode(clip.payload)
        XCTAssertEqual(decoded.ts, 3000,
                       "the local change's own recorded age, not the announced 1000 and not `now`")
        XCTAssertEqual(decoded.text, copied)
    }

    // MARK: - Final wave: status reports `up` only once the peer can actually sync

    /// The window this closes is the ordinary post-reboot one: the PC agent
    /// sends its hello the instant sshd spawns it, minutes before GNOME
    /// login, so `Channel.attempt` marks `.clipboardPending` and then that
    /// same frame's handler promoted straight back to `.up` microseconds
    /// later. `clipwire status` said `up` for the entire pre-login window,
    /// while nothing could sync at all. The peer's clip-state frame is the
    /// first that actually proves otherwise -- the agent sends it from
    /// inside `clipboard_became_ready`.
    func testAMatchedHelloAloneDoesNotReportUp() {
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(status.snapshot().state, .clipboardPending,
                       "a live peer whose clipboard is not readable yet cannot sync anything")
    }

    func testAPeersClipStateReportsUp() throws {
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let peer = ClipState(sha256: Self.hashB, ts: 9, kind: .text)

        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(status.snapshot().state, .up,
                       "the peer announced what it holds, so its clipboard is readable and sync works")
    }

    /// A null hash means the peer's clipboard is EMPTY, not unavailable --
    /// the agent only announces from inside `clipboard_became_ready`, so the
    /// frame's existence is the proof, not its contents. Syncing works fine
    /// in that state.
    func testAPeersEmptyClipStateStillReportsUp() throws {
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clipState, payload: try ClipState(sha256: nil, ts: 0, kind: nil).encodePayload()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(status.snapshot().state, .up)
    }

    /// A frame we cannot decode proves nothing about the peer's clipboard,
    /// so it must not promote. Placed after the decode guard, not before it.
    func testAMalformedClipStateDoesNotReportUp() {
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clipState, payload: Data("not json".utf8)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(), clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertNotEqual(status.snapshot().state, .up)
    }

    // MARK: - An undecodable clip-state is logged too, never dropped in silence

    /// The twin of `testAnUndecodableClipIsLogged` for the other frame type,
    /// and the last silent decode failure left in `handleFrame`. The drop is
    /// correct -- there is nothing valid to reconcile against -- but it also
    /// skips `recordPeerClipboardReady()`, so this side sits at
    /// `clipboard-pending` for the whole rest of the connection. In the
    /// mirror-image case the PC agent raises `ClipStateError` (a
    /// `FrameError`) and tears the channel down WITH a line -- its
    /// `_on_clip_state` calls `decode_clip_state` bare on purpose, and that
    /// divergence stays. Without this line a real codec desync reads as
    /// "the PC hangs up loudly, the Mac says nothing at all".
    ///
    /// Driven with well-formed JSON whose `sha256` is simply not 64
    /// lowercase hex, rather than the `Data("not json".utf8)` that
    /// `testAMalformedClipStateDoesNotReportUp` above already drives: this
    /// reaches `ClipState.decodePayload`'s own shape check instead of
    /// stopping inside `JSONDecoder`, so the line is known to carry the
    /// REASON rather than a fixed string that happens to match one input --
    /// the same distinction `testAClipWithANonFiniteTimestampIsLogged`
    /// draws for the `.clip` line.
    func testAnUndecodableClipStateIsLogged() {
        let path = tempLogPath()
        let log = Log(path: path)
        // "kind":"text" pairs validly with the non-null (if malformed) hash,
        // so the kind/hash equivalence check in `ClipState.init(from:)`
        // passes and this payload reaches `decodePayload`'s own sha256
        // shape check instead -- the thing this test actually pins.
        let wellFormedJSONWithABadHash = Data(#"{"sha256":"not-a-sha256","ts":1,"kind":"text"}"#.utf8)

        handleFrame(Frame(type: .clipState, payload: wellFormedJSONWithABadHash),
                    send: { _ in XCTFail("a rejected clip-state must not produce any frame") },
                    noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(loggedMessages(at: path),
                       ["could not decode a clip state from the peer: malformedSHA256"],
                       "the drop is right; doing it invisibly is what leaves this side stuck at " +
                       "clipboard-pending with nothing anywhere to explain it")
    }

    // MARK: - Final wave: the reconciliation decisions are logged, on both sides

    /// The design doc mandates this line by name for the branch where
    /// startup reconciliation finds the content no longer matches what was
    /// last recorded. It existed in neither implementation. Two things rest
    /// on it: acceptance item 2 requires a divergence to APPEAR IN THE LOG,
    /// and the design's one accepted trade-off -- with both clipboards
    /// changed while apart, the side whose agent was born more recently wins
    /// -- is justified on the grounds of being "visible in the log rather
    /// than mysterious", which is only true once this line exists.
    func testAnnounceClipStateLogsWhenTheClipboardChangedWhileApart() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashB, ts: 111, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("new content".utf8)

        announceClipState(send: { _ in }, pasteboard: pasteboard, clipStateStore: store,
                          log: log, now: 999_999)

        log.flush()
        XCTAssertTrue(loggedMessages(at: path).contains("clipboard changed while apart"),
                      "got: \(loggedMessages(at: path))")
    }

    /// End to end for the kind-threading fix, at the point where a wrong
    /// kind would actually do damage: what `announceClipState` puts ON THE
    /// WIRE and INTO THE STORE for an image-only pasteboard.
    ///
    /// The two unit tests on `resolveStartupState`/`resolveCurrentClipState`
    /// pin the rule; this one pins that nothing between them and the frame
    /// re-derives, defaults or drops the kind on the way out. `.image`
    /// against a stored `.text` again, so a hardcoded `.text` anywhere in
    /// that chain fails here rather than matching by coincidence. The peer's
    /// `decode_clip_state` accepts "image" (it is in `_KNOWN_KINDS`), so a
    /// wrong value here is not rejected on arrival -- it is believed.
    func testAnnounceClipStateOfAnImageOnlyPasteboardPutsTheImageKindOnTheWireAndOnDisk() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x02])
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: Self.hashB, ts: 111, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = png
        var sent: [Frame] = []

        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard,
                          clipStateStore: store, log: tempLog(), now: 999_999)

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .clipState)
        guard let payload = sent.first?.payload else { return }
        let announced = try ClipState.decodePayload(payload)
        XCTAssertEqual(announced, ClipState(sha256: sha256Hex(png), ts: 999_999, kind: .image),
                       "an image on the pasteboard must be announced as an image")
        XCTAssertEqual(store.load(), announced,
                       "and the store must record exactly what was announced, and no local hash")
    }

    /// "Nothing on disk" is the same branch: the content appeared while
    /// nothing was watching, and only `now` is honest about its age.
    func testAnnounceClipStateLogsWhenNothingWasEverStored() {
        let path = tempLogPath()
        let log = Log(path: path)
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("first ever content".utf8)

        announceClipState(send: { _ in }, pasteboard: pasteboard,
                          clipStateStore: tempClipStateStore(), log: log, now: 42)

        log.flush()
        XCTAssertTrue(loggedMessages(at: path).contains("clipboard changed while apart"))
    }

    /// The complement, and what keeps the line worth reading: the stored
    /// hash still matches, so nothing changed while apart. A line on every
    /// reconnect would teach everyone to ignore it.
    func testAnnounceClipStateIsSilentWhenTheStoredStateIsStillAuthoritative() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let text = Data("unchanged".utf8)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(text), ts: 555, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = text

        announceClipState(send: { _ in }, pasteboard: pasteboard, clipStateStore: store,
                          log: log, now: 999_999)

        log.flush()
        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed while apart"))
    }

    /// A nil hash never reaches a timestamp comparison at all --
    /// `resolveFreshness` refuses to compare timestamps when either side's
    /// hash is nil -- so there is no reconciliation judgement to report.
    func testAnnounceClipStateIsSilentForAnEmptyPasteboard() {
        let path = tempLogPath()
        let log = Log(path: path)

        announceClipState(send: { _ in }, pasteboard: RecordingPasteboard(),
                          clipStateStore: tempClipStateStore(), log: log, now: 42)

        log.flush()
        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed while apart"))
    }

    /// No reconciliation outcome was logged at all before this. The decision
    /// word is the shared vocabulary -- `FreshnessDecision`'s raw values are
    /// the same three strings the PC agent's SEND_MINE / WAIT_FOR_PEER /
    /// DO_NOTHING constants hold -- so both sides' lines are byte-identical
    /// for free, the convention the frame-cap and skew lines already follow.
    /// In production they even land in the same file: `Channel.attempt`
    /// pipes the agent's stderr into this log with a `remote: ` prefix, so
    /// one file shows the conflict and which side won it.
    ///
    /// The line also carries a `(mine=... peer=...)` kind suffix since
    /// Task 14 -- matched with `contains` below rather than exact equality
    /// for exactly that reason, so this test does not have to know its
    /// shape. See `testTheReconciliationLineNamesBothKinds` for the suffix
    /// itself.
    func testEveryReconciliationOutcomeIsLogged() throws {
        // The stored hash is the real digest of what the pasteboard double
        // returns, as in every other `sendMine` fixture in this file: the
        // decision line under test is logged BEFORE Task 11's verification,
        // so a placeholder would not break this test -- it would merely make
        // the `sendMine` case emit a stray "clipboard changed before the
        // send" and diverge from its siblings for no reason.
        let held = Data("whatever we hold".utf8)
        let heldHash = sha256Hex(held)
        let cases: [(storedTs: Double, peer: ClipState, expected: String)] = [
            (5, ClipState(sha256: Self.hashB, ts: 9, kind: .text), "waitForPeer"),
            (5, ClipState(sha256: heldHash, ts: 999, kind: .text), "doNothing"),
            (777, ClipState(sha256: nil, ts: 0, kind: nil), "sendMine"),
        ]
        for c in cases {
            let path = tempLogPath()
            let log = Log(path: path)
            let store = tempClipStateStore()
            try store.save(ClipState(sha256: heldHash, ts: c.storedTs, kind: .text))
            let pasteboard = RecordingPasteboard()
            pasteboard.textToRead = held

            handleFrame(Frame(type: .clipState, payload: try c.peer.encodePayload()),
                        send: { _ in }, noteWrittenLocally: { _, _ in },
                        pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                        log: log, clipStateStore: store,
                        clipStateAnnouncement: ClipStateAnnouncement())

            log.flush()
            XCTAssertTrue(loggedMessages(at: path).contains(where: { $0.contains("reconciled with the peer: \(c.expected)") }),
                          "expected \(c.expected); got: \(loggedMessages(at: path))")
        }
    }

    /// "why did a picture overwrite my text" must have an answer in the log.
    /// The decision word alone cannot say it -- see every case above, which
    /// never once asks what kind either side held. Mirrors test_watcher.py's
    /// test_the_reconciliation_line_names_both_kinds.
    func testTheReconciliationLineNamesBothKinds() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        let png = Data([0x89, 0x50])
        try store.save(ClipState(sha256: sha256Hex(png), ts: 5000, kind: .image))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = png
        let peer = ClipState(sha256: Self.hashB, ts: 1000, kind: .text)

        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        guard let line = loggedMessages(at: path).first(where: { $0.contains("reconciled with the peer") }) else {
            return XCTFail("no reconciliation line logged; got: \(loggedMessages(at: path))")
        }
        // One literal, not two independent substrings: pins the separator and
        // the spacing too, the same shape as the "over the image limit" lines
        // elsewhere in this suite, so a change that reordered the pair or
        // dropped the space still goes red here. The leading space matters
        // specifically: production builds this line from two concatenated
        // string literals (HandleFrame.swift), with the space on the FIRST one, so
        // a literal starting at "(" would miss that one being dropped.
        XCTAssertTrue(line.contains(" (mine=image peer=text)"), "got: \(line)")
    }

    /// The complement: neither side's hash implies neither side's kind, and
    /// the line must say so rather than omit it or print "nil". Mirrors
    /// test_watcher.py's test_the_line_says_none_when_a_side_holds_nothing.
    func testTheLineSaysNoneWhenASideHoldsNothing() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let peer = ClipState(sha256: nil, ts: 1000, kind: nil)

        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        guard let line = loggedMessages(at: path).first(where: { $0.contains("reconciled with the peer") }) else {
            return XCTFail("no reconciliation line logged; got: \(loggedMessages(at: path))")
        }
        XCTAssertTrue(line.contains(" (mine=none peer=none)"), "got: \(line)")
    }

    // MARK: - Final wave: a failed save is logged, at every Swift save site

    /// A plain file occupying the name where the store needs a directory,
    /// so `save()`'s very first step (`createDirectory`) throws for real
    /// rather than being mocked. Asserts the setup itself before returning,
    /// so a future change to `save()` that stopped failing here could not
    /// leave these tests silently proving nothing.
    private func unsaveableStore() throws -> ClipStateStore {
        let blockingFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-handleframe-test-blocker-\(UUID().uuidString)")
        try Data("occupying this name".utf8).write(to: blockingFile)
        let store = ClipStateStore(path: blockingFile.appendingPathComponent("clip-state.json").path)
        XCTAssertThrowsError(try store.save(ClipState(sha256: "aa", ts: 1, kind: .text)),
                             "test setup must actually force a save failure, or this test proves nothing")
        return store
    }

    private func persistFailures(at path: String) -> [String] {
        loggedMessages(at: path).filter { $0.hasPrefix("could not persist clip state: ") }
    }

    /// Every one of `agent/clipwire-agent.py`'s own `save_clip_state` call
    /// sites logs `could not persist clip state: %r`; the Swift ones were all
    /// bare `try?`. The asymmetry matters because the silent side is
    /// the one whose disk failure is the PRECONDITION for a store-goes-stale
    /// clobber: with nothing on disk, the next reconciliation re-derives an
    /// age from `now` and wins a comparison it should have lost.
    ///
    /// One test per Swift site, and the sites are enumerated on
    /// `persistClipState` in ClipStateAnnouncement.swift -- four now, since Task 13 gave
    /// `.imageClip` an apply path of its own. Both counts have moved once
    /// already, which is why neither is written as a number here.
    func testAnnounceClipStateLogsAFailedSave() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("still send this".utf8)

        announceClipState(send: { _ in }, pasteboard: pasteboard,
                          clipStateStore: try unsaveableStore(), log: log, now: 1)

        log.flush()
        XCTAssertEqual(persistFailures(at: path).count, 1,
                       "the announce path's failed save must not be silent")
    }

    func testAnAppliedClipLogsAFailedSave() throws {
        let path = tempLogPath()
        let log = Log(path: path)

        handleFrame(Frame(type: .clip, payload: ClipPayload(ts: 424242, text: "peer's clip").encode()),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: try unsaveableStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(persistFailures(at: path).count, 1,
                       "an applied clip whose state cannot be persisted must not be silent")
    }

    /// The fourth site, added with the `.imageClip` apply path. Its own
    /// failure is the more consequential of the two apply sites: with nothing
    /// on disk, the next reconnect's `announceClipState` resolves "clipboard
    /// changed while apart" for an image this side applied correctly, stamps
    /// `now` on it, and wins a comparison against the peer that actually sent
    /// it. The PC agent's `_write_clip` logs the same line for the same save.
    func testAnAppliedImageClipLogsAFailedSave() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let pasteboard = RecordingPasteboard()

        handleFrame(Frame(type: .imageClip, payload: try ImagePayload.encode(ts: 424242, png: png)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: try unsaveableStore(),
                    clipStateAnnouncement: ClipStateAnnouncement())

        log.flush()
        XCTAssertEqual(persistFailures(at: path).count, 1,
                       "an applied image whose state cannot be persisted must not be silent")
        XCTAssertEqual(pasteboard.writes.map(\.kind), [.image],
                       "and the write must have happened anyway -- a local disk failure is not " +
                       "the peer's fault and must not undo the clip it sent")
    }

    // MARK: - Task 13: an image clip from the peer is applied

    /// Replaces `testImageClipFrameIsReceivedAndLoggedButNotYetHandled`,
    /// which pinned Task 4's placeholder body (log the receipt, do nothing
    /// else) and would now pass vacuously against the real handler for the
    /// wrong reason -- its payload, `Data("not yet a real image".utf8)`, is
    /// 20 bytes with a finite leading ts and a non-empty body, so it decodes
    /// and is APPLIED rather than ignored.
    ///
    /// The kind is where a mistake would do the damage: written as `.text`,
    /// the PNG would land under `public.utf8-plain-text`, where
    /// `SystemPasteboard.read()` refuses it as invalid UTF-8 (see
    /// `PasteboardBackend`'s doc comment) -- the peer's image would vanish on
    /// arrival with nothing logged anywhere.
    func testAnImageClipIsAppliedToThePasteboardAsAnImage() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x07])
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .imageClip, payload: try ImagePayload.encode(ts: 1000, png: png)),
                    send: { _ in XCTFail("an image clip must never trigger a reply") },
                    noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: status, log: tempLog(),
                    clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 2000)

        XCTAssertEqual(pasteboard.writes.map(\.kind), [.image])
        XCTAssertEqual(pasteboard.writes.first?.data, png)
        XCTAssertNotNil(status.snapshot().lastReceivedAt,
                        "an applied image is a received clip, exactly as an applied text one is")
    }

    /// The image twin of `testIncomingClipArmsSuppressionBeforeWritingToThePasteboard`.
    /// `PasteboardWatcher` now EMITS images, so this ordering is load-bearing
    /// for images for the first time: a write observed before its suppression
    /// exists is echoed straight back to the peer it came from.
    ///
    /// Also pins WHICH bytes are armed -- the PNG alone, not `frame.payload`,
    /// which carries the 8-byte timestamp prefix. The watcher hashes what
    /// `pasteboard.read()` returns, which is the PNG; arming with the prefixed
    /// payload would make `EchoGuard`'s digest never match and every applied
    /// image bounce back.
    func testAnIncomingImageClipArmsSuppressionBeforeWritingToThePasteboard() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x07])
        var order: [String] = []
        var armed: [(kind: ClipKind, data: Data)] = []
        let pasteboard = RecordingPasteboard()
        pasteboard.onWrite = { order.append("write") }

        handleFrame(Frame(type: .imageClip, payload: try ImagePayload.encode(ts: 1000, png: png)),
                    send: { _ in },
                    noteWrittenLocally: { kind, data in
                        order.append("arm")
                        armed.append((kind, data))
                    },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 2000)

        XCTAssertEqual(order, ["arm", "write"],
                       "suppression must be armed BEFORE the write becomes observable")
        XCTAssertEqual(armed.map(\.kind), [.image],
                       "armed under the kind the watcher will observe, or the digests never meet")
        XCTAssertEqual(armed.first?.data, png,
                       "the PNG alone -- the timestamp-prefixed payload would never match a read")
    }

    /// The peer's timestamp, never `now`: the entire reason it travels in the
    /// frame. Stamping `now` would make an applied image look freshly copied
    /// here and win the next reconciliation against the machine it came from.
    /// The kind is `.image` for the same reason `.clip` stores `.text` --
    /// this case decoded the image codec, so there is nothing else it could
    /// be, and a wrong kind here is believed by the peer rather than rejected
    /// (`decode_clip_state` accepts both).
    func testAnIncomingImageClipStoresThePeersTimestampAndTheImageKind() throws {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x07])
        let store = tempClipStateStore()

        handleFrame(Frame(type: .imageClip, payload: try ImagePayload.encode(ts: 424242, png: png)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: RecordingPasteboard(), status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 999_999)

        XCTAssertEqual(store.load(), ClipState(sha256: sha256Hex(png), ts: 424242, kind: .image))
    }

    /// The image twin of `testAnUndecodableClipIsLogged`, and a deliberate
    /// divergence from the PC agent, whose `_write_clip` returns silently on
    /// a `ClipPayloadError` -- the same divergence the text path already
    /// carries, for the same reason: a drop nobody can see is how a
    /// permanent, mutual desync becomes invisible on both machines at once.
    ///
    /// Both cases the image codec rejects on top of a well-formed frame are
    /// covered, since they are reached by different guards: a payload too
    /// short to hold a timestamp, and one that is a timestamp and nothing
    /// else. The second is why `ClipPayloadError.emptyBody` exists at all --
    /// an empty clip TEXT is legal and applies in silence, an image clip
    /// carrying no image is not representable.
    func testAnUndecodableImageClipIsLogged() {
        for payload in [Data(), Data([0x00]), Data(repeating: 0, count: 8)] {
            let pasteboard = RecordingPasteboard()
            let path = tempLogPath()
            let log = Log(path: path)

            handleFrame(Frame(type: .imageClip, payload: payload),
                        send: { _ in },
                        noteWrittenLocally: { _, _ in
                            XCTFail("nothing was applied, so nothing may be suppressed")
                        },
                        pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                        log: log, clipStateStore: tempClipStateStore(),
                        clipStateAnnouncement: ClipStateAnnouncement())
            log.flush()

            XCTAssertTrue(pasteboard.writes.isEmpty, "nothing decoded, so nothing may be written")
            XCTAssertTrue(loggedMessages(at: path).contains {
                $0.hasPrefix("could not decode an image clip from the peer: ")
            }, "got: \(loggedMessages(at: path))")
        }
    }

    // MARK: - v3.2: an incoming image is applied, whatever the board holds

    /// v3.1 read the pasteboard here and kept the local bytes whenever the
    /// incoming image decoded to the same pixels, to stop a retina screenshot
    /// coming back from the PC at double size. It never fired on real
    /// hardware -- GPaste re-encodes through the embedded ICC profile, so the
    /// samples move -- and v3.2 deleted it in favour of the PC announcing the
    /// `origin` of the bytes it holds. These fixtures are what is left of that
    /// section: two distinct blobs, since nothing in this path decodes them
    /// any more. Real PNGs built by `ImageIO` went with the comparison that
    /// needed them.
    private static let heldImage = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x01])
    private static let returnedImage = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x02])

    /// *** The removal, as an assertion. *** An image arrives while this board
    /// already holds a DIFFERENT image, and it is applied: armed, written, and
    /// stored under the peer's hash and timestamp. Under v3.1 what the board
    /// held decided whether any of that happened; now nothing about it is even
    /// read.
    func testAnImageIsAppliedEvenWhenTheBoardAlreadyHoldsAnImage() throws {
        let store = tempClipStateStore()
        var order: [String] = []
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.heldImage
        pasteboard.onWrite = { order.append("write") }

        handleFrame(Frame(type: .imageClip,
                          payload: try ImagePayload.encode(ts: 1000, png: Self.returnedImage)),
                    send: { _ in }, noteWrittenLocally: { _, _ in order.append("arm") },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 2000)

        XCTAssertEqual(order, ["arm", "write"])
        XCTAssertEqual(pasteboard.writes.first?.data, Self.returnedImage)
        XCTAssertEqual(store.load(),
                       ClipState(sha256: sha256Hex(Self.returnedImage), ts: 1000, kind: .image),
                       "the bytes it wrote ARE what the clipboard will return, so the store "
                       + "records their hash under the peer's timestamp")
    }

    /// The same, over text. Kept as the other half of "whatever the board
    /// holds": a kind guard is exactly what v3.1 needed and v3.2 does not.
    func testAnImageArrivingOverTextIsApplied() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = Data("something the user copied".utf8)

        handleFrame(Frame(type: .imageClip,
                          payload: try ImagePayload.encode(ts: 1000, png: Self.returnedImage)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 2000)

        XCTAssertEqual(pasteboard.writes.map(\.kind), [.image])
    }

    /// *** The cost the removal takes back. *** This branch asked the
    /// pasteboard what it was holding so it could compare pixels, and on
    /// `SystemPasteboard` that is not a cheap accessor: an image read pulls
    /// the board's TIFF representation and converts it to PNG, on the
    /// channel's decode thread, for a multi-megabyte screenshot. With the
    /// comparison gone the count is not one but ZERO, and pinning that is what
    /// keeps a future "just check what's there first" from quietly putting the
    /// conversion back.
    func testTheImageBranchNeverReadsThePasteboard() throws {
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.heldImage

        handleFrame(Frame(type: .imageClip,
                          payload: try ImagePayload.encode(ts: 1000, png: Self.returnedImage)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: tempClipStateStore(),
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 2000)

        XCTAssertEqual(pasteboard.reads, 0,
                       "applying an image needs to know nothing about what the board held")
    }

    /// *** Acceptance items 2 and 3, end to end. *** Apply the peer's image,
    /// then let the next connection announce: the seed must find the clipboard
    /// UNCHANGED, stay silent, and announce the hash both sides now hold --
    /// which is what makes the peer resolve `doNothing` and the loop settle
    /// after one frame instead of pulling the same picture across on every
    /// reconnect forever.
    ///
    /// The board is set to return what was written, because `RecordingPasteboard`
    /// does not do that for itself and `NSPasteboard` does: it hands back the
    /// bytes it was given, which is the premise the applying branch's stored
    /// hash rests on. v3.1's version of this test staged the divergent record
    /// the density fix produced; that state no longer exists, and the property
    /// it guarded -- no false `clipboard changed while apart` after applying a
    /// clip -- does.
    func testTheNextAnnouncementAfterAnAppliedImageFindsNothingChanged() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.heldImage
        var sent: [Frame] = []

        handleFrame(Frame(type: .imageClip,
                          payload: try ImagePayload.encode(ts: 424242, png: Self.returnedImage)),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement(), now: 999_999)
        pasteboard.imageToRead = Self.returnedImage
        // A later connection, on a clipboard nobody has touched since.
        announceClipState(send: { sent.append($0) }, pasteboard: pasteboard,
                          clipStateStore: store, log: log, now: 1_000_000)
        log.flush()

        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed while apart"),
                       "nothing changed; got: \(loggedMessages(at: path))")
        let announced = try ClipState.decodePayload(XCTUnwrap(sent.first).payload)
        XCTAssertEqual(announced,
                       ClipState(sha256: sha256Hex(Self.returnedImage), ts: 424242, kind: .image),
                       "the applied hash under the peer's own timestamp, unchanged -- stamping "
                       + "`now` here would win a reconciliation against the machine the picture "
                       + "came from")
        XCTAssertEqual(resolveFreshness(mine: announced,
                                        peer: ClipState(sha256: sha256Hex(Self.returnedImage),
                                                        ts: 424242, kind: .image)),
                       .doNothing,
                       "which is what the peer answers it with")
    }

    /// The `.sendMine` recovery reached through the OTHER source of `mine`.
    /// When the store cannot be read, the `.clipState` case falls back to what
    /// THIS connection announced, and that value has to satisfy the send
    /// verification -- the one path that exists for when a save has already
    /// failed. An image, because `testWinningClipStateWithAnImageSendsAnImageClipCarryingOurStoredTimestamp`
    /// covers the same recovery through the store.
    ///
    /// The store file is removed between the announcement and the peer's
    /// frame, which is what `ClipStateStore.load()` reports as "nothing
    /// stored" -- the same answer a torn file gets. One announcement gate
    /// across both calls, as `wireAgent` uses for a whole connection.
    func testTheAnnouncedFallbackStillSatisfiesTheSendVerification() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.heldImage
        let announcement = ClipStateAnnouncement()
        var sent: [Frame] = []

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { _ in }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 1_000_000)
        XCTAssertEqual(announcement.announced,
                       ClipState(sha256: sha256Hex(Self.heldImage), ts: 1_000_000, kind: .image),
                       "the arrange step must have announced the image, or this test proves nothing")
        try FileManager.default.removeItem(at: store.url)

        // A peer that has lost its clipboard entirely -- a locked PC session,
        // or cleared GPaste history, both routine.
        handleFrame(Frame(type: .clipState,
                          payload: try ClipState(sha256: nil, ts: 500_000, kind: nil).encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: announcement, now: 1_000_001)
        log.flush()

        XCTAssertFalse(loggedMessages(at: path).contains("clipboard changed before the send"),
                       "nobody touched this pasteboard; got: \(loggedMessages(at: path))")
        XCTAssertEqual(sent.filter { $0.type == .imageClip }.count, 1,
                       "an unreadable store must not cost the peer the picture too")
        let frame = try XCTUnwrap(sent.first { $0.type == .imageClip })
        XCTAssertEqual(try ImagePayload.decode(frame.payload).png, Self.heldImage)
    }

    // MARK: - v3.2, Task 6b: the .clipState case actually consults provenance

    /// Tasks 1-6 built `resolveProvenance`, put `origin` on the wire, taught
    /// the PC to record it, made it survive that agent's death and disarmed
    /// the expectation that could poison it -- and NOTHING invoked any of
    /// it. A capability built and never connected is this project's
    /// signature planning defect, and these are what make the connection
    /// observable on the Mac side alone: the pairing harness proves it end
    /// to end, but it drives both implementations at once, so with only
    /// that test a break here and a break in the PC agent's `_resolve_clip_state`
    /// are the same red.
    ///
    /// The bug, at the size a unit test holds it: the PC applied this Mac's
    /// screenshot, GPaste re-encoded the selection, and `_consume_image_reoffer`
    /// recorded the re-encode at the peer's own timestamp PLUS a
    /// millisecond -- deliberately, so the first reconnect resolves
    /// deterministically instead of by hex tie-break. That determinism is
    /// exactly what hands this Mac back its own screenshot with the density
    /// gone. Two attempts to compare image CONTENT both failed, measured,
    /// because GPaste applies the embedded ICC profile and the samples move.

    private static let macsOriginal = Data([0x89, 0x50, 0x4E, 0x47, 0x6D, 0x61, 0x63])
    private static let pcsReEncode = Data([0x89, 0x50, 0x4E, 0x47, 0x67, 0x70, 0x61, 0x73, 0x74, 0x65])

    /// THE FIX, in the direction the reported defect travels. The peer is a
    /// millisecond fresher and holds different bytes, so `resolveFreshness`
    /// alone says `waitForPeer` and this side then APPLIES the degraded copy
    /// when it arrives. `doNothing` is what declines it.
    ///
    /// The pasteboard genuinely holds the original and the store genuinely
    /// records its hash, the same arrangement every other reconciliation
    /// fixture in this file uses, so nothing here passes because a guard
    /// further down happened to refuse.
    func testAPeerHoldingOurOwnScreenshotReEncodedIsDeclined() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(Self.macsOriginal), ts: 1000, kind: .image))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.macsOriginal
        var sent: [Frame] = []
        // What the PC announces after the substitution: its own re-encode,
        // nudged past ours, naming the hash it was GIVEN.
        let peer = ClipState(sha256: sha256Hex(Self.pcsReEncode), ts: 1000.001, kind: .image,
                             origin: sha256Hex(Self.macsOriginal))

        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty, "no frame in either direction")
        XCTAssertTrue(loggedMessages(at: path).contains(where: { $0.contains("reconciled with the peer: doNothing") }),
                      "waitForPeer here is waiting for the degraded copy; got: \(loggedMessages(at: path))")
        XCTAssertTrue(loggedMessages(at: path).contains("the peer's clipboard descends from what we hold: standing down"),
                      "a suppression that leaves no trace is indistinguishable from a bug, and " +
                      "it fires exactly when the user expects something; got: \(loggedMessages(at: path))")
        XCTAssertEqual(pasteboard.writes.count, 0, "the screenshot this Mac still holds is the good one")
    }

    /// The mirror, and the reason the rule is ONE symmetric function rather
    /// than one per side: this Mac holds a derivative of what the peer has,
    /// and `resolveFreshness` alone would say `sendMine` and push it. Both
    /// sides stand down together, so neither is left waiting for a clip the
    /// other already declined.
    ///
    /// The Mac reaches this state by having applied an image from a THIRD
    /// state of the world and then recording an origin -- which this side
    /// never does today. It is tested anyway because the rule is symmetric
    /// by construction and a wiring that only ever consulted one half would
    /// pass the test above.
    func testWeDoNotPushTheAncestorsOwnerADerivativeOfWhatItHolds() throws {
        let path = tempLogPath()
        let log = Log(path: path)
        let store = tempClipStateStore()
        try store.save(ClipState(sha256: sha256Hex(Self.pcsReEncode), ts: 1000.001, kind: .image,
                                 origin: sha256Hex(Self.macsOriginal)))
        let pasteboard = RecordingPasteboard()
        pasteboard.imageToRead = Self.pcsReEncode
        var sent: [Frame] = []
        let peer = ClipState(sha256: sha256Hex(Self.macsOriginal), ts: 1000, kind: .image)

        handleFrame(Frame(type: .clipState, payload: try peer.encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: log, clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement())
        log.flush()

        XCTAssertTrue(sent.isEmpty, "the peer holds our ancestor; our derivative is not news")
        XCTAssertTrue(loggedMessages(at: path).contains(where: { $0.contains("reconciled with the peer: doNothing") }),
                      "got: \(loggedMessages(at: path))")
        XCTAssertTrue(loggedMessages(at: path).contains("what we hold descends from the peer's clipboard: standing down"),
                      "got: \(loggedMessages(at: path))")
    }

    /// The nil trap, pinned AT THE CALL SITE rather than only in the rule.
    /// `nil == nil` is `true` for a Swift `Optional`, so the naive spelling
    /// of provenance fires on our own origin-less state against a peer that
    /// announced nothing -- a locked PC against an ordinary Mac, which
    /// happens daily -- and would kill `resolveFreshness`'s `(_, nil) ->
    /// .sendMine` recovery for EVERY kind of content, not merely for images.
    /// fixtures/provenance.json's nil rows pin the rule; this pins that
    /// wiring it in did not reintroduce the trap one layer up.
    func testAnEmptyPeerIsStillHandedBackWhatItLost() throws {
        let store = tempClipStateStore()
        let held = Data("the clip the peer lost".utf8)
        try store.save(ClipState(sha256: sha256Hex(held), ts: 777, kind: .text))
        let pasteboard = RecordingPasteboard()
        pasteboard.textToRead = held
        var sent: [Frame] = []

        handleFrame(Frame(type: .clipState,
                          payload: try ClipState(sha256: nil, ts: 0, kind: nil).encodePayload()),
                    send: { sent.append($0) }, noteWrittenLocally: { _, _ in },
                    pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                    log: tempLog(), clipStateStore: store,
                    clipStateAnnouncement: ClipStateAnnouncement())

        XCTAssertEqual(sent.filter { $0.type == .clip }.count, 1,
                       "a peer with nothing gets its clipboard back; suppressing this is v1's " +
                       "silent loss, restored by a rule about images")
    }
}
