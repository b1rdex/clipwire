// Tests/clipwireTests/HandleFrameImageAndProvenanceTests.swift
//
// A shard of HandleFrameTests, split out in v3.2.1. Extension of the same
// class, so the (class, method) pairs the release is verified against are
// unchanged. See docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import XCTest
@testable import clipwire

extension HandleFrameTests {
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
