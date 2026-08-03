// Tests/clipwireTests/HandleFrameClipStateTests.swift
//
// A shard of HandleFrameTests, split out in v3.2.1. Extension of the same
// class, so the (class, method) pairs the release is verified against are
// unchanged. See docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import XCTest
@testable import clipwire

extension HandleFrameTests {
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

}
