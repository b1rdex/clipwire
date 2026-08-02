// Tests/clipwireTests/HandleFrameHelloTests.swift
//
// A shard of HandleFrameTests, split out in v3.2.1. Extension of the same
// class, so the (class, method) pairs the release is verified against are
// unchanged. See docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import XCTest
@testable import clipwire

extension HandleFrameTests {
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

}
