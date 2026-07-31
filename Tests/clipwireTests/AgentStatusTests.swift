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

    /// Inverted by the final wave, deliberately: this used to assert `.up`.
    /// A matched hello proves the peer PROCESS is alive, which is not the
    /// same as the channel being able to sync -- the PC agent sends its
    /// hello the instant sshd spawns it, after a reboot minutes before the
    /// Wayland session its clipboard needs. Promoting here overwrote the
    /// `.clipboardPending` that `Channel.attempt()` had set microseconds
    /// earlier, from that same frame. The reason must still be CLEARED,
    /// which is unchanged and is what keeps a stale mismatch from sticking.
    func testRecordHelloMatchedReportsClipboardPendingNotUp() {
        let status = AgentStatus(pid: 1, url: url())
        status.recordHelloMatched()
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .clipboardPending,
                       "a live peer process is not yet a channel that can sync")
        XCTAssertNil(snapshot.reason)
    }

    /// The spec's stated reason for splitting clip-state off `hello` at all:
    /// it "gives `status` its long-missing protocol basis for reporting
    /// `clipboard-pending` durably". A matched hello only proves the PEER
    /// PROCESS is alive -- the PC agent sends its hello immediately, long
    /// before the Wayland session exists -- so it cannot mean the channel can
    /// actually sync. The peer's clip-state announcement is the first and
    /// only frame that does prove it: the agent sends it from inside
    /// `clipboard_became_ready`.
    func testPeerClipboardReadyIsWhatReportsUp() {
        let status = AgentStatus(pid: 1, url: url())
        status.recordHelloMatched()
        XCTAssertEqual(status.snapshot().state, .clipboardPending,
                       "a matched hello proves the peer process is alive, not that it can sync")

        status.recordPeerClipboardReady()
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .up)
        XCTAssertNil(snapshot.reason)
    }

    /// The non-obvious half. `recordProtocolMismatch` pins a specific
    /// diagnosis precisely so a user running `clipwire status` is told to run
    /// `clipwire install` instead of being shown something generic -- and a
    /// clip-state frame can arrive after one (nothing about a version
    /// mismatch stops the peer's own frames from being in flight). Promoting
    /// unconditionally would erase that diagnosis and report a healthy
    /// channel that is about to close. Mirrors `applyChannelState`'s existing
    /// rule that a pinned mismatch outranks whatever is reported next.
    func testPeerClipboardReadyDoesNotOverwriteAPinnedProtocolMismatch() {
        let status = AgentStatus(pid: 1, url: url())
        let mismatch = "protocol mismatch: peer speaks 3, we speak 2 — run `clipwire install`"
        status.recordProtocolMismatch(mismatch)

        status.recordPeerClipboardReady()

        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .down, "a mismatched peer's clipboard readiness changes nothing")
        XCTAssertEqual(snapshot.reason, mismatch)
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

    // MARK: - Fix round 1, Finding 2: the "starting" reason must not go stale
    //
    // Channel.run()'s never-established path (the ordinary "PC is off"
    // case) never calls onStateChange at all, so nothing would otherwise
    // ever replace the startup placeholder -- a Mac that boots with the
    // peer off would show "down — starting" for as long as it stays off,
    // which is the single most common normal state `clipwire status`
    // exists to explain.

    func testZeroReconnectsIsStillLegitimatelyStarting() {
        let status = AgentStatus(pid: 1, url: url())
        status.tickHeartbeat(reconnects: 0)
        XCTAssertEqual(status.snapshot().reason, "starting",
                       "no attempts have happened yet -- this is not the gap being fixed")
    }

    func testStaleStartingReasonIsReplacedOnceReconnectsClimb() {
        let status = AgentStatus(pid: 1, url: url())
        XCTAssertEqual(status.snapshot().reason, "starting")

        status.tickHeartbeat(reconnects: 3)
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .down)
        XCTAssertNotEqual(snapshot.reason, "starting",
                          "must not still say 'starting' once real reconnect attempts have been made")
        XCTAssertTrue(snapshot.reason?.contains("(3 attempts)") == true,
                      "expected an attempt count in: \(snapshot.reason ?? "nil")")
        XCTAssertTrue(snapshot.reason?.contains("peer unreachable") == true)
    }

    func testSingularAttemptWordingForExactlyOneReconnect() {
        let status = AgentStatus(pid: 1, url: url())
        status.tickHeartbeat(reconnects: 1)
        let reason = status.snapshot().reason
        XCTAssertTrue(reason?.contains("(1 attempt)") == true, "expected singular wording in: \(reason ?? "nil")")
        XCTAssertFalse(reason?.contains("(1 attempts)") == true)
    }

    func testTheSynthesizedReasonTracksTheLatestAttemptCountNotTheFirst() {
        let status = AgentStatus(pid: 1, url: url())
        status.tickHeartbeat(reconnects: 1)
        XCTAssertTrue(status.snapshot().reason?.contains("(1 attempt)") == true)

        status.tickHeartbeat(reconnects: 4)
        XCTAssertTrue(status.snapshot().reason?.contains("(4 attempts)") == true,
                      "the count must track the latest tick, not freeze at the first one ever computed")
    }

    func testOnceChannelHasReportedAnythingTheHeartbeatStopsSynthesizingAReason() {
        let status = AgentStatus(pid: 1, url: url())
        status.applyChannelState(.down, "channel closed")
        status.tickHeartbeat(reconnects: 5)
        XCTAssertEqual(status.snapshot().reason, "channel closed",
                       "once Channel has reported anything at all, the heartbeat must not override " +
                       "it with a synthesized reason, however many attempts have piled up since")
    }

    func testAfterAMatchingHelloTheHeartbeatDoesNotReintroduceTheStaleReason() {
        let status = AgentStatus(pid: 1, url: url())
        status.recordHelloMatched()
        status.tickHeartbeat(reconnects: 2)
        let snapshot = status.snapshot()
        XCTAssertEqual(snapshot.state, .clipboardPending)
        // The reason-is-nil half is this test's actual subject: whatever
        // the state, a channel that HAS reported must not be given a
        // synthesized "peer unreachable" reason by the heartbeat.
        XCTAssertNil(snapshot.reason, "an established channel must not grow a synthesized " +
                     "'unreachable' reason just because reconnects is nonzero from an earlier drop")
    }

    func testAPinnedProtocolMismatchIsNotOverwrittenByTheSynthesizedReason() {
        let status = AgentStatus(pid: 1, url: url())
        let mismatch = "protocol mismatch: peer speaks 2, we speak 1 — run `clipwire install`"
        status.recordProtocolMismatch(mismatch)
        status.tickHeartbeat(reconnects: 10)
        XCTAssertEqual(status.snapshot().reason, mismatch)
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

    func testDecodeHelloExposesTheSentAtItDecodes() {
        let peer = decodeHello(Data(#"{"protocol":2,"agent":"0.1.0","sent_at":1000.5}"#.utf8))
        XCTAssertEqual(peer?.sentAt, 1000.5)
    }

    func testDecodeHelloToleratesAMissingSentAt() {
        // Same tolerance as the agent field, for the same reason: a peer
        // that omits it must still report its real version, not degrade to
        // "malformed hello".
        let peer = decodeHello(Data(#"{"protocol":1}"#.utf8))
        XCTAssertEqual(peer?.version, 1)
        XCTAssertNil(peer?.sentAt ?? nil)
    }

    // MARK: - skewLogLine
    //
    // The twin of agent/clipwire-agent.py's skew_log_line, asserted against
    // the same strings: both sides are meant to log the same quantity in the
    // same shape, the way the two "over the text limit" lines already do.
    // agent/tests/test_frame.py's TestSkewLogLine is the mirror of this
    // section, case for case.

    func testSkewLogLineReportsASmallDifferenceWithoutAWarning() {
        XCTAssertEqual(skewLogLine(peerSentAt: 1000.0, now: 1000.5), "peer clock skew 0.5s")
    }

    func testSkewLogLineMeasuresAnAbsoluteDifferenceSoDirectionDoesNotMatter() {
        XCTAssertEqual(skewLogLine(peerSentAt: 1000.5, now: 1000.0),
                       skewLogLine(peerSentAt: 1000.0, now: 1000.5))
    }

    func testSkewLogLineWarnsAboveTheThreshold() {
        XCTAssertEqual(skewLogLine(peerSentAt: 1000.0, now: 1006.0),
                       "peer clock skew 6.0s — over 5s, check the clock on both machines")
    }

    func testSkewLogLineDoesNotWarnExactlyAtTheThreshold() {
        // "Warn ABOVE five seconds": the boundary itself is not a warning.
        XCTAssertEqual(skewLogLine(peerSentAt: 1000.0, now: 1005.0), "peer clock skew 5.0s")
    }

    func testTheSkewWarningTextQuotesTheThresholdConstant() {
        // The threshold is a literal inside the message (no second
        // float-formatting bridge to keep byte-identical with Python), so
        // pin the literal against the constant here instead.
        XCTAssertEqual(SkewConstants.warnSeconds, 5)
        XCTAssertEqual(
            skewLogLine(peerSentAt: 0, now: 1000)?.contains("over \(Int(SkewConstants.warnSeconds))s"),
            true)
    }

    func testSkewLogLineSaysNothingWhenItCannotBeMeasured() {
        // A peer that omits sent_at, and the non-finite values a peer could
        // in principle hand us. Not measurable is not a violation: no line,
        // no warning. (Foundation's JSONDecoder rejects the bare NaN and
        // Infinity JSON literals outright, so on THIS side those never even
        // reach here through decodeHello -- the guard mirrors the Python
        // side, where json.loads does accept them.)
        XCTAssertNil(skewLogLine(peerSentAt: nil, now: 1000))
        XCTAssertNil(skewLogLine(peerSentAt: .nan, now: 1000))
        XCTAssertNil(skewLogLine(peerSentAt: .infinity, now: 1000))
        XCTAssertNil(skewLogLine(peerSentAt: -.infinity, now: 1000))
    }
}
