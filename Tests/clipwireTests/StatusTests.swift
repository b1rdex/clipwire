// Tests/clipwireTests/StatusTests.swift
import XCTest
@testable import clipwire

final class StatusTests: XCTestCase {
    private var url: URL!

    override func setUp() {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-status-\(UUID().uuidString).json")
    }

    func testRoundTrip() throws {
        let now = Date()
        try Status(state: .up, reason: nil, heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 3).write(to: url)
        guard case .healthy(let status) = Status.read(from: url, now: now, pidIsAlive: { _ in true }) else {
            return XCTFail("fresh heartbeat with a live pid must be healthy")
        }
        XCTAssertEqual(status.reconnects, 3)
    }

    func testStaleHeartbeatIsAgentDead() throws {
        let written = Date()
        try Status(state: .up, reason: nil, heartbeat: written, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 0).write(to: url)
        let later = written.addingTimeInterval(StatusConstants.heartbeatStaleAfter + 1)
        guard case .agentDead = Status.read(from: url, now: later, pidIsAlive: { _ in true }) else {
            return XCTFail("a stale heartbeat must report agentDead regardless of state")
        }
    }

    func testDeadPidIsAgentDead() throws {
        let now = Date()
        try Status(state: .up, reason: nil, heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 0).write(to: url)
        guard case .agentDead = Status.read(from: url, now: now, pidIsAlive: { _ in false }) else {
            return XCTFail("a dead pid must report agentDead even with a fresh heartbeat")
        }
    }

    func testMissingFileIsAgentDead() {
        let absent = URL(fileURLWithPath: "/nonexistent/clipwire/status.json")
        guard case .agentDead = Status.read(from: absent, now: Date(), pidIsAlive: { _ in true }) else {
            return XCTFail("no status file means nothing is running")
        }
    }

    func testDownStateIsUnhealthyNotDead() throws {
        let now = Date()
        try Status(state: .down, reason: "peer unreachable", heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 7).write(to: url)
        guard case .unhealthy(_, let reason) = Status.read(from: url, now: now, pidIsAlive: { _ in true }) else {
            return XCTFail("a live agent with a down channel is unhealthy, not dead")
        }
        XCTAssertEqual(reason, "peer unreachable")
    }

    // Not in the brief. Self-review (see task-6-report.md) asked whether
    // `read` tells a missing file apart from a present-but-unreadable one.
    // The brief's verbatim implementation routes both through the same
    // `try? Data(contentsOf:)` branch and says "the agent has never run"
    // either way — misleading when the file exists but a JSON decoder would
    // choke on it (or, as here, it isn't a regular file at all). A directory
    // is used instead of chmod 0o000 because permission bits are bypassed
    // when tests run as root, which would make that path flaky in CI; a
    // directory can never decode as Data regardless of privilege level.
    // Mirrors the "fix round" pattern already used in ConfigTests.swift.
    func testExistingButUndecodableFileDoesNotClaimAgentNeverRan() throws {
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        guard case .agentDead(let message) = Status.read(from: url, now: Date(), pidIsAlive: { _ in true }) else {
            return XCTFail("a present-but-undecodable status file means health cannot be determined")
        }
        XCTAssertFalse(message.contains("never run"),
                       "a file that exists is a different failure than no file at all — got: \(message)")
    }
}
