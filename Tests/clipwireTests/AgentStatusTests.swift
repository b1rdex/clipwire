// Tests/clipwireTests/AgentStatusTests.swift
import XCTest
@testable import clipwire

final class AgentStatusTests: XCTestCase {
    private func url() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-agentstatus-test-\(UUID().uuidString).json")
    }

    // The clobber this pins: Channel.attempt() marks `.clipboardPending`
    // the moment the peer's hello decodes, then hands the frame to
    // onFrame -- which is where a protocol mismatch is actually detected.
    // Shortly after, the peer notices the very same mismatch on its own
    // side (it validates whatever hello it receives from us) and exits,
    // so Channel.run() reports the drop with its generic "channel closed"
    // reason. Without pinning, that generic reason overwrites the specific
    // one and a user running `clipwire status` sees no hint that a
    // forgotten `clipwire install` is the actual cause.
    func testProtocolMismatchReasonSurvivesTheGenericChannelClosedThatFollowsIt() {
        let status = AgentStatus(pid: 1, url: url())
        let mismatch = "protocol mismatch: peer speaks 2, we speak 1 — run `clipwire install`"
        status.recordProtocolMismatch(mismatch)
        status.applyChannelState(.down, "channel closed")
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .down)
        XCTAssertEqual(snapshot.reason, mismatch,
                       "the specific reason must survive the generic one reported right after it")
    }

    func testAMatchingHelloClearsAPreviouslyPinnedMismatchReason() {
        let status = AgentStatus(pid: 1, url: url())
        status.recordProtocolMismatch("protocol mismatch: peer speaks 2, we speak 1 — run `clipwire install`")
        status.recordHelloMatched()
        status.applyChannelState(.down, "channel closed")
        XCTAssertEqual(status.snapshot().reason, "channel closed",
                        "once install is fixed and a hello matches, a later generic reason must " +
                        "no longer be masked by the stale mismatch")
    }

    func testRecordHelloMatchedReportsUp() {
        let status = AgentStatus(pid: 1, url: url())
        status.recordHelloMatched()
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .up)
        XCTAssertNil(snapshot.reason)
    }

    func testSentAndReceivedTimestampsAreRecorded() {
        let status = AgentStatus(pid: 1, url: url())
        XCTAssertNil(status.snapshot().lastSentAt)
        status.recordSent()
        XCTAssertNotNil(status.snapshot().lastSentAt)
        XCTAssertNil(status.snapshot().lastReceivedAt)
        status.recordReceived()
        XCTAssertNotNil(status.snapshot().lastReceivedAt)
    }

    func testTickHeartbeatWritesReconnectsAndAFreshHeartbeatToDisk() throws {
        let target = url()
        let status = AgentStatus(pid: 42, url: target)
        status.tickHeartbeat(reconnects: 7)

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        let written = try decoder.decode(Status.self, from: Data(contentsOf: target))
        XCTAssertEqual(written.reconnects, 7)
        XCTAssertEqual(written.pid, 42)
        XCTAssertLessThan(Date().timeIntervalSince(written.heartbeat), 5)
    }

    // MARK: - decodeHello / ProtocolConstants

    func testProtocolConstantsHelloPayloadRoundTripsThroughDecodeHello() {
        let peer = decodeHello(ProtocolConstants.helloPayload)
        XCTAssertEqual(peer?.version, ProtocolConstants.version)
        XCTAssertEqual(peer?.agent, ProtocolConstants.agentVersion)
    }

    func testDecodeHelloReadsAMismatchedVersionAndAgent() {
        let peer = decodeHello(Data(#"{"protocol":999,"agent":"9.9.9"}"#.utf8))
        XCTAssertEqual(peer?.version, 999)
        XCTAssertEqual(peer?.agent, "9.9.9")
    }

    func testDecodeHelloToleratesAMissingAgentField() {
        // The agent field is not consulted for compatibility -- only
        // logged -- so a peer that omits it must not be treated as
        // unparsable.
        let peer = decodeHello(Data(#"{"protocol":1}"#.utf8))
        XCTAssertEqual(peer?.version, 1)
        XCTAssertNil(peer?.agent)
    }

    func testDecodeHelloReturnsNilForMalformedJSON() {
        XCTAssertNil(decodeHello(Data("not json at all".utf8)))
    }

    func testDecodeHelloReturnsNilWhenTheProtocolKeyIsMissing() {
        XCTAssertNil(decodeHello(Data(#"{"agent":"0.1.0"}"#.utf8)),
                      "no protocol field means the version cannot be confirmed compatible")
    }
}
