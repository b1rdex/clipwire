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

    // MARK: - File descriptor ownership
    //
    // Found in production, not by a test: the shipped agent ran for 3 days 19
    // hours across 1168 reconnects and accumulated 2556 open PIPE descriptors,
    // filling its descriptor table solid (fds 0…2559 of a 2560 ceiling). Past
    // saturation `Pipe()` can no longer allocate, so `Process.run()` dup2's
    // fd −1 and throws `NSPOSIXErrorDomain Code=9 "Bad file descriptor"` —
    // logged as "failed to start ssh", with `status.json` stuck on
    // `"reason": "cannot start ssh"`. Partial allocations are worse than the
    // clean failure: a half-built stdin pipe gives the remote agent immediate
    // EOF, so it prints "stdin closed, exiting" and returns 0, and the log
    // reads like a healthy connection that keeps closing rather than like the
    // resource exhaustion it is.
    //
    // `attempt()` opened three `Pipe()`s per connection and closed none of
    // them; the only `close()` in Channel.swift lives in `hangUp()`, which
    // production never calls. Three parent-side ends leak per successful
    // attempt (stdin-write, stdout-read, stderr-read — `Process` closes the
    // child ends itself); all six leak when the spawn throws, plus a live
    // dispatch source behind the uncleared `readabilityHandler`.
    //
    // Counting `/dev/fd` rather than shelling out to `lsof` keeps this to one
    // directory listing. The absolute count includes whatever XCTest itself
    // holds open, so only the delta across a fixed number of attempts is
    // meaningful, and one warm-up attempt runs first so that per-Channel and
    // per-Log one-time state is already allocated when the baseline is taken.
    private func openFileDescriptorCount() -> Int {
        (try? FileManager.default.contentsOfDirectory(atPath: "/dev/fd").count) ?? -1
    }

    // The grace period is not slack for flakiness: teardown of the stderr read
    // end is deliberately EOF-driven from inside the readability handler
    // itself, because cancelling a dispatch source is asynchronous and an
    // already-running handler invocation would hit `availableData` on a
    // descriptor the run thread had just closed — an ObjC
    // `NSFileHandleOperationException`, which Swift cannot catch, taking the
    // whole agent down. Closing from the handler's own last invocation is
    // race-free by construction, and costs this wait.
    private func settleAsyncDescriptorTeardown() {
        Thread.sleep(forTimeInterval: 0.3)
    }

    private func assertNoDescriptorLeak(
        executablePath: String, attempts: Int = 20,
        _ message: String, file: StaticString = #filePath, line: UInt = #line
    ) {
        let channel = Channel(config: config(), log: tempLog(),
                              executablePath: executablePath, arguments: { _, _ in [] })
        _ = channel.attempt(host: "pc")
        settleAsyncDescriptorTeardown()

        let before = openFileDescriptorCount()
        for _ in 0..<attempts { _ = channel.attempt(host: "pc") }
        settleAsyncDescriptorTeardown()
        let after = openFileDescriptorCount()

        // Two descriptors of headroom absorbs the log file's open/close churn
        // racing the listing, without coming close to hiding a per-attempt
        // leak: the smallest one this catches is 3 per attempt over 20.
        XCTAssertLessThanOrEqual(
            after - before, 2,
            "\(message) — \(attempts) attempts leaked \(after - before) descriptors "
            + "(\(before) open before, \(after) after)",
            file: file, line: line)
    }

    func testRepeatedConnectionsDoNotLeakFileDescriptors() {
        // `/usr/bin/true` exits immediately with no output, which is the same
        // shape as the production drop this has to survive: stdout reaches EOF
        // and `attempt()` falls through to `waitUntilExit()`.
        assertNoDescriptorLeak(
            executablePath: "/usr/bin/true",
            "a connection that opens and closes must return its pipes")
    }

    // MARK: - Autorelease pools on the reconnect thread
    //
    // Sibling of the descriptor leak above, found while fixing it, and the
    // reason both are pinned here: `run()` is a `while true` loop on the main
    // thread of a plain Swift executable, which never drains an autorelease
    // pool of its own. Every ObjC object the loop touches — `Process`,
    // `Pipe`, and one `NSData` per `availableData` — therefore accumulates
    // for the life of the process. Measured on this machine at 400 spawn/
    // wait cycles: +2128 KB and +2112 KB of RSS across two bare runs, versus
    // +736 KB and +128 KB with a pool. Roughly 5 KB per reconnect, and the
    // inner read loop is the worse of the two, because a connection that
    // stays up for hours never leaves `attempt()` at all — it just keeps
    // appending `NSData` to a pool nothing will drain.
    //
    // Pinned against the source rather than by observing behavior, which is
    // this file's existing convention when neither is available at runtime:
    // `testRunCallsIgnoreSIGPIPEBeforeItsReconnectLoop` above does the same,
    // for the same reason. `run()` never returns and needs a live ssh peer,
    // and the inner loop only iterates more than once against a peer that
    // streams — so an RSS assertion here would be measuring allocator noise,
    // not the pool. What a regression would actually do is delete these
    // calls from the source, and that is what these tests watch.
    private func channelSource() throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(
            contentsOf: root.appendingPathComponent("Sources/clipwire/Channel.swift"), encoding: .utf8)
    }

    func testTheReconnectLoopDrainsAnAutoreleasePoolPerAttempt() throws {
        let source = try channelSource()
        guard let runRange = source.range(of: "func run() {"),
              let loopRange = source.range(of: "while true {", range: runRange.upperBound..<source.endIndex),
              let bodyEnd = source.range(of: "\n    }", range: loopRange.upperBound..<source.endIndex)
        else { return XCTFail("could not locate run()'s reconnect loop -- has it been restructured?") }

        let loopBody = source[loopRange.upperBound..<bodyEnd.lowerBound]
        XCTAssertTrue(
            loopBody.contains("autoreleasepool"),
            "run()'s reconnect loop must drain an autorelease pool each time round -- "
            + "it is a bare `while true` on a thread with no pool of its own, so without "
            + "this every Process and Pipe it creates is retained until the agent exits")
    }

    func testTheReadLoopDrainsAnAutoreleasePoolPerChunk() throws {
        let source = try channelSource()
        // Anchored on the assignment itself, not on the first `availableData`
        // in range: the surrounding comments discuss `availableData` in prose,
        // and matching one of those would pass or fail on the wording of a
        // comment rather than on what the loop executes.
        guard let loopRange = source.range(of: "outer: while task.isRunning {"),
              let chunkRange = source.range(of: "let chunk =", range: loopRange.upperBound..<source.endIndex),
              let lineEnd = source.range(of: "\n", range: chunkRange.upperBound..<source.endIndex)
        else { return XCTFail("could not locate attempt()'s read loop -- has it been restructured?") }

        let readStatement = source[chunkRange.lowerBound..<lineEnd.lowerBound]
        XCTAssertTrue(
            readStatement.contains("availableData"),
            "the read loop's chunk must still come from availableData -- otherwise this "
            + "test is pinning a statement that no longer reads the channel")
        XCTAssertTrue(
            readStatement.contains("autoreleasepool"),
            "each availableData in attempt()'s read loop must drain an autorelease pool -- "
            + "a connection that stays up for hours never returns from attempt(), so its "
            + "per-read NSData would pile up with nothing to release it")
    }

    func testAFailedSpawnDoesNotLeakFileDescriptors() {
        // The throw path leaks hardest — six ends and a live readability
        // handler — and it is the path production ends up pinned on once the
        // table is full, so it is also the path that decides whether the agent
        // can ever recover on its own instead of needing a restart.
        assertNoDescriptorLeak(
            executablePath: "/nonexistent/clipwire-no-such-executable",
            "a spawn that throws must still return its pipes")
    }
}
