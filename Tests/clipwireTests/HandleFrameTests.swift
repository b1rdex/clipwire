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
import XCTest
@testable import clipwire

final class HandleFrameTests: XCTestCase {
    /// Records every `writeText` call and, via `onWrite`, lets a test
    /// observe exactly when it happens relative to other calls -- the
    /// ORDER is what these tests pin, not merely that a write occurred.
    /// A spy that only checked occurrence would pass against code that
    /// armed the suppression after writing instead of before.
    final class RecordingPasteboard: PasteboardWriting {
        private(set) var writtenTexts: [String] = []
        var onWrite: (() -> Void)?

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

    // MARK: - Contract 1: arm-before-write

    func testIncomingClipArmsSuppressionBeforeWritingToThePasteboard() {
        var order: [String] = []
        let pasteboard = RecordingPasteboard()
        pasteboard.onWrite = { order.append("write") }
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clip, payload: Data("hello".utf8)),
                    send: { _ in XCTFail("a clip frame must never trigger a reply") },
                    noteWrittenLocally: { _ in order.append("arm") },
                    pasteboard: pasteboard, status: status, log: tempLog())

        XCTAssertEqual(order, ["arm", "write"],
                       "suppression must be armed strictly before the pasteboard write -- reversed " +
                       "order would let the watcher observe the write before the suppression exists " +
                       "and bounce our own clip back to the peer")
        XCTAssertEqual(pasteboard.writtenTexts, ["hello"], "exactly one write, with the decoded text")
    }

    func testEmptyClipTouchesNeitherSuppressionNorThePasteboard() {
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .clip, payload: Data()),
                    send: { _ in }, noteWrittenLocally: { _ in XCTFail("must not arm for an empty clip") },
                    pasteboard: pasteboard, status: status, log: tempLog())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
        XCTAssertNil(status.snapshot().lastReceivedAt)
    }

    func testNonUTF8ClipTouchesNeitherSuppressionNorThePasteboard() {
        let pasteboard = RecordingPasteboard()
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let invalidUTF8 = Data([0xFF, 0xFE, 0xFD])

        handleFrame(Frame(type: .clip, payload: invalidUTF8),
                    send: { _ in }, noteWrittenLocally: { _ in XCTFail("must not arm for undecodable text") },
                    pasteboard: pasteboard, status: status, log: tempLog())

        XCTAssertTrue(pasteboard.writtenTexts.isEmpty)
    }

    // MARK: - Contract 2: a received hello always produces a reply

    func testMatchingHelloProducesExactlyOneReplyAndReportsUp() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        handleFrame(Frame(type: .hello, payload: ProtocolConstants.helloPayload),
                    send: { sent.append($0) },
                    noteWrittenLocally: { _ in XCTFail("a hello must not touch the pasteboard") },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog())

        XCTAssertEqual(sent.count, 1)
        XCTAssertEqual(sent.first?.type, .hello)
        XCTAssertEqual(status.snapshot().state, .up)
    }

    func testMismatchedHelloStillProducesExactlyOneReply() {
        var sent: [Frame] = []
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let mismatched = Data(#"{"protocol":999,"agent":"9.9.9"}"#.utf8)

        handleFrame(Frame(type: .hello, payload: mismatched),
                    send: { sent.append($0) }, noteWrittenLocally: { _ in },
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog())

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
                    pasteboard: RecordingPasteboard(), status: status, log: tempLog())

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
                    log: tempLog())

        guard let reply = sent.first, let decoded = decodeHello(reply.payload) else {
            return XCTFail("expected a decodable hello reply")
        }
        XCTAssertEqual(decoded.version, ProtocolConstants.version)
    }
}
