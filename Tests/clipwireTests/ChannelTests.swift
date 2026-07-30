// Tests/clipwireTests/ChannelTests.swift
import XCTest
@testable import clipwire

final class ChannelTests: XCTestCase {
    private func config(fallback: String? = nil) -> Config {
        Config(host: "pc", fallbackIP: fallback, user: "me",
               identityFile: "~/.ssh/id_ed25519",
               remoteAgentPath: "~/.local/share/clipwire/clipwire-agent.py",
               macPollIntervalMs: 400)
    }

    private func tempLog() -> Log {
        Log(path: FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-channel-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path)
    }

    func testArgumentsPinEverythingExplicitly() {
        let args = sshArguments(for: config(), host: "pc")
        XCTAssertTrue(args.contains("-o"))
        XCTAssertTrue(args.contains("IdentitiesOnly=yes"),
                      "must not fall back to the default identity list")
        XCTAssertTrue(args.contains("BatchMode=yes"),
                      "must never block on an interactive prompt under launchd")
        XCTAssertTrue(args.contains("ServerAliveInterval=5"))
        XCTAssertTrue(args.contains("ServerAliveCountMax=2"))
        XCTAssertTrue(args.contains("ConnectTimeout=5"))
        XCTAssertTrue(args.contains("me@pc"))
        XCTAssertEqual(args.last, "~/.local/share/clipwire/clipwire-agent.py")
    }

    func testIdentityPathIsExpanded() {
        let args = sshArguments(for: config(), host: "pc")
        guard let index = args.firstIndex(of: "-i") else { return XCTFail("no -i") }
        XCTAssertFalse(args[index + 1].hasPrefix("~"), "ssh gets no shell to expand ~")
    }

    func testFallbackIPIsUsedAsAnAlternateHost() {
        let args = sshArguments(for: config(fallback: "192.168.1.10"), host: "192.168.1.10")
        XCTAssertTrue(args.contains("me@192.168.1.10"))
    }

    // Fix round 1, finding 1. Without `Channel.ignoreSIGPIPE()` having run
    // (which `Channel.run()` does as its first line), this exact write would
    // terminate the whole test process with SIGPIPE instead of failing an
    // assertion — see the task report for the A/B proof and for why that
    // asymmetry (crash instead of a red X on regression) is an accepted
    // limitation of testing a process-global signal disposition from inside
    // the same process whose disposition is under test. This test does not
    // prove `run()` itself calls `ignoreSIGPIPE()` — `run()` never returns
    // and needs a live ssh, so it can't be driven from a test — only that
    // once called, the write behaves as `send()`'s `do/catch` assumes.
    func testIgnoringSIGPIPEMakesADeadPipeWriteThrowInsteadOfTerminating() {
        Channel.ignoreSIGPIPE()
        let pipe = Pipe()
        try! pipe.fileHandleForReading.close()
        XCTAssertThrowsError(
            try pipe.fileHandleForWriting.write(contentsOf: Data("x".utf8)))
    }

    // Final review: the test above proves the EFFECT (a dead-pipe write
    // throws instead of terminating the process) but not that
    // `Channel.ignoreSIGPIPE()` -- which `Channel.run()` calls as its first
    // line -- is what actually armed it. A regression that dropped that
    // call would make that same test CRASH the whole XCTest binary instead
    // of failing red, since it still performs a real write to a pipe with
    // no reader; whether the crash happens depends entirely on the bug this
    // test exists to catch. `signal()` reports the PRIOR disposition on
    // every call, so calling it a second time with the same value reveals
    // what `Channel.ignoreSIGPIPE()`'s call already set -- with no pipe
    // write at all, so a regression here fails red instead of crashing.
    func testIgnoreSIGPIPEActuallyArmsTheIgnoreDisposition() {
        Channel.ignoreSIGPIPE()
        let previousDisposition = signal(SIGPIPE, SIG_IGN)
        // @convention(c) function pointer types are not Equatable in Swift;
        // compare the underlying pointer bit pattern instead.
        XCTAssertEqual(
            unsafeBitCast(previousDisposition, to: Int.self),
            unsafeBitCast(SIG_IGN, to: Int.self),
            "Channel.ignoreSIGPIPE() must already have set SIGPIPE's disposition to SIG_IGN")
    }

    // Final review: `recordSent()` used to fire even when `channel.send`
    // dropped the frame because no pipe existed yet, so `clipwire status`
    // could report a clip that never left the machine. This pins the seam
    // that fix depends on: `Channel.send(_:onSent:)` itself must report
    // `false` when no connection has ever been established, so a caller
    // gating a status update on that report cannot be fooled.
    func testSendReportsFalseWhenNoChannelIsEstablished() {
        let channel = Channel(config: config(), log: tempLog())
        let expectation = XCTestExpectation(description: "onSent called")
        // Safe despite the mutation happening on Channel's private
        // writeQueue: wait(for:) below only returns once fulfill() has been
        // called from inside this same closure, strictly after the
        // assignment -- the same happens-before reasoning already used for
        // the `nonisolated(unsafe)` captures in PasteboardTests.swift's
        // concurrency tests.
        nonisolated(unsafe) var result: Bool?
        channel.send(Frame(type: .clip, payload: Data("x".utf8)), onSent: { sent in
            result = sent
            expectation.fulfill()
        })
        wait(for: [expectation], timeout: 2)
        XCTAssertEqual(result, false, "no pipe exists yet -- the frame cannot have reached it")
    }

    // Fix round 1, finding 3. Injects `Date` values instead of sleeping to
    // pin the 30s floor exactly at its boundary.
    func testBackoffResetRequiresThirtySecondsOfUptime() {
        let start = Date()
        XCTAssertFalse(
            Channel.shouldResetBackoff(establishedAt: start, now: start.addingTimeInterval(29.9)),
            "just under the floor must not reset")
        XCTAssertTrue(
            Channel.shouldResetBackoff(establishedAt: start, now: start.addingTimeInterval(30)),
            "at the floor must reset")
    }

    func testBackoffNeverResetsForAConnectionThatWasNeverEstablished() {
        XCTAssertFalse(Channel.shouldResetBackoff(establishedAt: nil, now: Date()))
    }
}
