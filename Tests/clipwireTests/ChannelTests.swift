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

    // v3.1 task 1. The spawned command became injectable so the pairing
    // harness can point `Channel` at `python3 agent/clipwire-agent.py` and run
    // the two halves of this project against each other. That leaves
    // `sshArguments` as the one part of the connect path the harness never
    // executes, so it is pinned here on its exact output — every element, in
    // order, and nothing else. The three `contains`-style tests around this
    // one all stay green if the order scrambles, if a flag is duplicated, if
    // an option loses its value, or if an extra argument appears between `-i`
    // and the key path; a pure function from config to strings can be covered
    // completely, and this is what completely looks like.
    //
    // The home directory is computed rather than written out: `expandTilde`
    // calls `NSHomeDirectory()`, so a literal `/Users/<someone>/…` would pass
    // on the author's Mac and fail on the macOS CI runner this suite is about
    // to start running on. `remoteAgentPath` keeps its `~` on purpose — ssh
    // runs the command through the remote user's shell, which expands it
    // there; expanding it here would send the Mac's home path to the PC.
    func testSSHArgumentsAreExactlyThisListInThisOrder() {
        XCTAssertEqual(
            sshArguments(for: config(), host: "pc"),
            [
                "-i", NSHomeDirectory() + "/.ssh/id_ed25519",
                "-o", "IdentitiesOnly=yes",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2",
                "me@pc",
                "~/.local/share/clipwire/clipwire-agent.py",
            ])
    }

    // The same change stopped the production spawn from being a literal at its
    // call site: `Commands.swift` constructs `Channel(config:log:)` with no
    // command at all, so the initializer's DEFAULTS are what ships. Nothing
    // else observes them — `run()` never returns, and the pairing harness,
    // which is why `attempt(host:)` stopped being private, always injects —
    // so a regression that repointed a default would leave the entire suite
    // green while the agent stopped speaking ssh, and the harness (which
    // injects, and therefore never touches the defaults) would not catch it
    // either. Behavioural rather than identity comparison on the argv builder,
    // paired with the exact-output test above: together they pin that the
    // default builds today's argv, and that today's argv is today's argv.
    func testTheDefaultCommandIsStillSSHWithTheProductionArguments() {
        let channel = Channel(config: config(), log: tempLog())
        XCTAssertEqual(channel.executablePath, "/usr/bin/ssh")
        XCTAssertEqual(channel.makeArguments(config(), "pc"),
                       sshArguments(for: config(), host: "pc"),
                       "the default argv builder must still be sshArguments(for:host:)")
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

    // Second-round final review, Finding 2: the test above calls
    // `Channel.ignoreSIGPIPE()` itself, so it pins that function's own
    // body -- not that `run()` actually calls it. Deleting the call at
    // `run()`'s call site would leave the whole suite green while
    // reinstating the exact process kill this exists to prevent, at the
    // exact moment the PC reboots and a dead-pipe write races `ssh` dying.
    //
    // `run()` cannot be invoked from a test: it is a `while true` reconnect
    // loop that never returns and needs a live ssh peer to do anything
    // meaningful, so nothing behavioral can observe it running at all --
    // launching it on a background thread would leak a thread spawning
    // real `ssh` against a fake host for the rest of the test binary's
    // life, which is exactly the kind of real-subprocess dependency every
    // other Channel test in this file avoids. This is the same class of
    // gap `TestModuleDefinitionOrder` (agent/tests/test_watcher.py)
    // already accepts on the Python side, for the identical reason
    // (behavior gated on a real Wayland session there, a real ssh peer
    // here) -- pinned directly against the source there, and here.
    //
    // This does not observe `run()` executing at runtime. It proves the
    // call is textually present in `run()`'s body, before the reconnect
    // loop starts -- which is exactly what a source deletion would remove.
    func testRunCallsIgnoreSIGPIPEBeforeItsReconnectLoop() throws {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let source = try String(
            contentsOf: root.appendingPathComponent("Sources/clipwire/Channel.swift"), encoding: .utf8)

        guard let runRange = source.range(of: "func run() {") else {
            return XCTFail("could not find func run() in Channel.swift -- has it been renamed?")
        }
        guard let loopRange = source.range(of: "while true {", range: runRange.upperBound..<source.endIndex) else {
            return XCTFail("could not find run()'s reconnect loop")
        }

        let runPreamble = source[runRange.upperBound..<loopRange.lowerBound]
        XCTAssertTrue(
            runPreamble.contains("ignoreSIGPIPE()"),
            "run() must call Channel.ignoreSIGPIPE() before its reconnect loop starts -- " +
            "removing that call reinstates a process kill the instant a dead-pipe write " +
            "races ssh exiting, exactly when the PC reboots")
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
