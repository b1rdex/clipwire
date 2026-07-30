// Tests/clipwireTests/ChannelTests.swift
import XCTest
@testable import clipwire

final class ChannelTests: XCTestCase {
    private func config(fallback: String? = nil) -> Config {
        Config(host: "pc", fallbackIP: fallback, user: "me",
               identityFile: "~/.ssh/id_ed25519",
               remoteAgentPath: "~/.local/share/clipwire/clipwire-agent.py",
               macPollIntervalMs: 400, pcFallbackPollIntervalMs: 1000,
               maxFrameBytes: FrameConstants.maxPayloadBytes)
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
