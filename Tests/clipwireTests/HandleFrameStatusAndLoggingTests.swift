// Tests/clipwireTests/HandleFrameStatusAndLoggingTests.swift
//
// A shard of HandleFrameTests, split out in v3.2.1. Extension of the same
// class, so the (class, method) pairs the release is verified against are
// unchanged. See docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import XCTest
@testable import clipwire

extension HandleFrameTests {

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
    /// never once asks what kind either side held. Mirrors
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
    /// test_the_line_says_none_when_a_side_holds_nothing.
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

}
