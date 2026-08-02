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

}
