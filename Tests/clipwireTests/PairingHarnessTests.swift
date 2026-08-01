// Tests/clipwireTests/PairingHarnessTests.swift
//
// The first test in this project's history that runs BOTH implementations
// against each other.
//
// Every defect clipwire ever shipped -- the whitespace trim, GPaste's image
// re-encode, the leaked gdbus child, the safety net declaring a healthy source
// dead, the retina doubling -- was found by running it on the owner's
// machines, never by a test, because the Swift and Python halves had never
// once executed together: each suite drives its own side against scripted
// pipes, and a scripted pipe agrees with whatever the side under test
// believes. This file closes that.
//
// WHAT ACTUALLY RUNS. `Channel` spawns `python3 agent/clipwire-agent.py`
// through the seam v3.1 task 1 opened (an injectable executable path and
// argv, defaulted to `/usr/bin/ssh` + `sshArguments`, so production is
// untouched). The agent finds `Tests/fakes/wl-copy`, `wl-paste` and `gdbus`
// ahead of the real tools on `PATH` and forks them BY NAME, exactly as it
// forks the real ones on the PC. So what crosses is a real frame, over a real
// pipe, between two real implementations: real argv, real EOF, real SIGPIPE.
// The only drawings in the picture are the Linux clipboard (which cannot
// exist on a Mac) and `NSPasteboard` (whose own translation layer is pinned
// separately, in PasteboardTests).
//
// IT ASSERTS ITS OWN WORLD BEFORE IT TESTS ANYTHING, AND REFUSES. See
// `PairingHarness.start()`. A harness that degrades to a weaker run and
// reports success is worse than one that fails: this release's own pipe-write
// experiment did exactly that, and reached a correct conclusion from invalid
// evidence. The precondition that earns its keep is the third one -- that the
// agent is on the GPASTE EVENT PATH, not polling. Measured during task 3:
// with a `gdbus` that answers only `monitor`, the agent logs "GPaste
// unavailable, falling back to polling every 1.0s" AND THE CLIP STILL
// CROSSES. Every clip assertion below would pass while the pump, the worker
// and the safety net -- where v3's two production defects actually lived --
// never ran once. That blindness would arrive as a green suite, which is the
// one failure mode this file exists to remove.
// `testTheHarnessRefusesAnAgentOnThePollingFallback` keeps that measurement
// as a test rather than as a memory.
import XCTest
@testable import clipwire

final class PairingHarnessTests: XCTestCase {
    /// Held rather than passed to `addTeardownBlock` so that a test which
    /// fails mid-way still hangs up the agent: the block form would need the
    /// harness to be `Sendable`, which it is not and has no reason to be.
    private var harness: PairingHarness?

    override func tearDown() {
        harness?.stop()
        harness = nil
        super.tearDown()
    }

    private func connected(eventSource: PairingHarness.EventSource = .gpaste)
    throws -> PairingHarness {
        let harness = try PairingHarness(eventSource: eventSource)
        self.harness = harness
        try harness.start()
        return harness
    }

    // MARK: - a clip crosses the loop: both kinds, both directions

    /// Mac -> PC, text. The user copies on the Mac; `PasteboardWatcher`
    /// observes it, `handleLocalChange` encodes a `.clip` frame, the agent
    /// decodes it and hands it to `wl-copy`.
    ///
    /// Non-ASCII on purpose: the text codec keeps bytes as bytes on both
    /// sides, and a UTF-8 round trip through Swift's `String`, the wire, and
    /// Python's `bytes` is exactly the sort of thing that survives two
    /// separate unit suites and fails between them.
    func testTextCopiedOnTheMacReachesThePCsClipboard() throws {
        let harness = try connected()

        harness.pasteboard.set("copied on the Mac ✓")

        try harness.waitForThePCsClipboard(toHold: Data("copied on the Mac ✓".utf8),
                                           offeredAs: "text/plain;charset=utf-8")
    }

    /// PC -> Mac, text. Nobody writes through `wl-copy` here: the harness
    /// changes the shared clipboard state directly, which is what a person
    /// copying on the PC does. The fake `gdbus` emits an `Update` for any
    /// change by anyone, so this direction exists at all -- an `Update` per
    /// agent write would have covered only the echo direction.
    func testTextCopiedOnThePCReachesTheMacsPasteboard() throws {
        let harness = try connected()

        try harness.copyOnThePC(text: "copied on the PC ✓")

        try harness.waitForTheMacsPasteboard(toHold: .text,
                                             Data("copied on the PC ✓".utf8))
    }

    /// Mac -> PC, image: a `.imageClip` frame (type 0x03), the codec that
    /// carries a PNG rather than text.
    ///
    /// Measured, not assumed: sending this as a `.clip` instead fails here
    /// AND fails three existing tests in HandleFrameTests, which pin the
    /// frame type directly. What this one adds is the consequence rather than
    /// the mistake -- the failure names `wl-copy --type
    /// text/plain;charset=utf-8 <- 95 bytes` in the fakes' invocation log and
    /// a PNG offered under five text MIME types on the peer's clipboard,
    /// which is what the user would actually have been left holding.
    func testAnImageCopiedOnTheMacReachesThePCsClipboard() throws {
        let harness = try connected()

        harness.pasteboard.setImage(PairingHarness.png)

        try harness.waitForThePCsClipboard(toHold: PairingHarness.png, offeredAs: "image/png")
    }

    /// PC -> Mac, image. The last of the four legs, and the one that needs
    /// every part of the event path at once: the fake `gdbus`'s `Update`, the
    /// agent's pump, its worker thread, its two-call read (`--list-types`,
    /// then the body), and `handleFrame`'s `.imageClip` case.
    func testAnImageCopiedOnThePCReachesTheMacsPasteboard() throws {
        let harness = try connected()

        try harness.copyOnThePC(png: PairingHarness.png)

        try harness.waitForTheMacsPasteboard(toHold: .image, PairingHarness.png)
    }

    // MARK: - the harness refuses a world it cannot honestly test

    /// Task 3's measurement, kept as a test instead of as a sentence in a
    /// commit message: with a `gdbus` that answers `monitor` but not
    /// `introspect`, `GPasteWatcher.available()` is false, `make_watcher`
    /// returns the plain poller, and the four tests above would still pass --
    /// on a run that exercised none of the machinery they exist to cover.
    ///
    /// So the harness refuses, and this is the evidence that its refusal is
    /// not vacuous. Without a test in this shape, "the harness asserts the
    /// event path" is a claim about a code path nothing ever takes.
    func testTheHarnessRefusesAnAgentOnThePollingFallback() throws {
        let harness = try PairingHarness(eventSource: .answersOnlyMonitor)
        self.harness = harness

        XCTAssertThrowsError(try harness.start()) { error in
            guard case PairingHarness.Refusal.wrongWorld(let what, _) = error else {
                return XCTFail("expected the harness to refuse the polling fallback, got \(error)")
            }
            XCTAssertTrue(what.contains("falling back to polling"),
                          "the refusal must name what it saw in the agent's own log, so a " +
                          "reader does not have to guess which precondition failed: \(what)")
        }
    }
}

// MARK: -

/// One connection between the two implementations, plus the world it needs.
///
/// Not `Sendable`, and not trying to be: `Channel.attempt(host:)` runs on a
/// thread of its own (as `run()` does in production) and calls back into
/// `handleFrame` from there, so the pasteboard the test thread reads is
/// genuinely shared across threads. `HarnessPasteboard` below carries the
/// lock that makes that safe; everything else here is either immutable after
/// `start()` or guarded by `framesLock`.
private final class PairingHarness {
    /// Which `gdbus` goes on `PATH`.
    enum EventSource {
        /// `Tests/fakes/gdbus`: answers `introspect` and `monitor`, so the
        /// agent takes the GPaste event path.
        case gpaste
        /// A shim that answers only `monitor` -- the world the harness must
        /// refuse. See `testTheHarnessRefusesAnAgentOnThePollingFallback`.
        case answersOnlyMonitor
    }

    enum Refusal: Error, CustomStringConvertible {
        /// A precondition failed: the world is not the one this harness can
        /// test in, so nothing it observed afterwards would mean anything.
        case wrongWorld(String, diagnostics: String)
        /// Something that should have happened did not, inside the timeout.
        case timedOut(String, diagnostics: String)

        var description: String {
            switch self {
            case .wrongWorld(let what, let diagnostics):
                return "the pairing harness refuses to run: \(what)\n\(diagnostics)"
            case .timedOut(let what, let diagnostics):
                return "timed out waiting for \(what)\n\(diagnostics)"
            }
        }
    }

    /// Every wait here has to expire well inside the agent's
    /// SAFETY_NET_POLL_SECONDS (30s). That is not a performance preference:
    /// the safety net polls the clipboard on its own, so a wait longer than
    /// one tick would let a completely dead event path still deliver the clip
    /// -- 30 seconds late, and green. The point of this harness is that such
    /// a run goes red.
    static let waitTimeout: TimeInterval = 15

    /// A real 2x2 PNG carrying `pHYs` (5669 px/m ≈ 144 dpi, what a retina
    /// screenshot has). Written out as bytes rather than built with AppKit so
    /// that what crosses the wire is a fixed, inspectable value and a failure
    /// diff is readable. The density chunk is deliberate groundwork: v3.1's
    /// second half is about `pHYs` surviving a round trip, and the fake
    /// clipboard's substituting mode drops exactly this chunk.
    static let png = Data([
        0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A,
        0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x02,
        0x00, 0x00, 0x00, 0x02, 0x08, 0x06, 0x00, 0x00, 0x00, 0x72, 0xB6, 0x0D, 0x24,
        0x00, 0x00, 0x00, 0x09, 0x70, 0x48, 0x59, 0x73, 0x00, 0x00, 0x16, 0x25,
        0x00, 0x00, 0x16, 0x25, 0x01, 0x49, 0x52, 0x24, 0xF0,
        0x00, 0x00, 0x00, 0x11, 0x49, 0x44, 0x41, 0x54, 0x78, 0xDA, 0x63, 0xF8,
        0xCF, 0xC0, 0x00, 0x42, 0xFF, 0x19, 0x60, 0x0C, 0x00, 0x43, 0xCE, 0x07,
        0xF9, 0x25, 0x02, 0xF9, 0xBE,
        0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
    ])

    /// What the real `wl-copy` offers for a plain-text selection: the type it
    /// was given plus the X11-era aliases. `choose_kind` reads this list, and
    /// the agent's text body fetch asks for the first one by name.
    private static let textTypes = ["text/plain;charset=utf-8", "text/plain",
                                    "TEXT", "STRING", "UTF8_STRING"]

    let root: URL
    let agentURL: URL
    let pasteboard = HarnessPasteboard()

    private let fakesURL: URL
    /// Which `gdbus` this configuration put on `PATH` — the fake, or the
    /// monitor-only shim the refusal test sabotages it with.
    private let gdbusURL: URL
    private let directory: URL
    private let statePath: String
    private let logPath: String
    private let log: Log
    private let watcher: PasteboardWatcher
    private let channel: Channel
    private let connectionEnded = DispatchSemaphore(value: 0)
    private var restoreEnvironment: [String: String?] = [:]
    private var connectionStarted = false
    private var stopped = false

    private let framesLock = NSLock()
    private var received: [Frame] = []

    init(eventSource: EventSource = .gpaste) throws {
        // Tests/clipwireTests/ -> Tests/ -> the repo root. The same walk
        // FixtureTests and ChannelTests already do; `#filePath` is the only
        // thing in a test binary that knows where the source tree is.
        root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        agentURL = root.appendingPathComponent("agent/clipwire-agent.py")
        fakesURL = root.appendingPathComponent("Tests/fakes")
        directory = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pairing-\(UUID().uuidString)")
        statePath = directory.appendingPathComponent("clipboard.json").path
        logPath = directory.appendingPathComponent("clipwire.log").path

        let runtime = directory.appendingPathComponent("runtime")
        let stateHome = directory.appendingPathComponent("state")
        try FileManager.default.createDirectory(at: runtime, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: stateHome, withIntermediateDirectories: true)
        // `WaylandClipboard.ready()` is `os.path.exists($XDG_RUNTIME_DIR/wayland-0)`
        // and nothing else, so an empty file here is a complete Wayland
        // session as far as the agent can tell. Without it the agent's phase
        // never leaves PHASE_PENDING, no watcher is ever built, and every
        // clip queues forever.
        try Data().write(to: runtime.appendingPathComponent("wayland-0"))

        // `Process.executableURL` performs no PATH lookup, but the child
        // inherits this process's environment -- so the fakes have to be on
        // PATH before `Channel` is constructed, and python3 has to be
        // resolved by hand.
        let interpreter = try PairingHarness.resolvePython3()
        var searchPath = [fakesURL.path, interpreter.deletingLastPathComponent().path,
                          "/usr/bin", "/bin"]
        switch eventSource {
        case .gpaste:
            gdbusURL = fakesURL.appendingPathComponent("gdbus")
        case .answersOnlyMonitor:
            let shim = try PairingHarness.writeMonitorOnlyGdbus(in: directory, wrapping: fakesURL)
            searchPath.insert(shim.deletingLastPathComponent().path, at: 0)
            gdbusURL = shim
        }
        // A free function with the restore map handed in, rather than a
        // method: Swift will not let an initializer call one of its own
        // methods before every stored property is initialized, and PATH has
        // to be set BEFORE `Channel` is constructed -- the child inherits
        // this process's environment at spawn time.
        var restore: [String: String?] = [:]
        exportEnvironment("PATH", searchPath.joined(separator: ":"), restoring: &restore)
        exportEnvironment("CLIPWIRE_FAKE_CLIPBOARD_STATE", statePath, restoring: &restore)
        exportEnvironment("XDG_RUNTIME_DIR", runtime.path, restoring: &restore)
        // The agent's own clip-state store. Pointed at a temp directory so no
        // harness run ever touches ~/.local/state on the machine running the
        // suite -- the same rule agent/tests already follows by injecting
        // `clip_state_path`.
        exportEnvironment("XDG_STATE_HOME", stateHome.path, restoring: &restore)
        restoreEnvironment = restore

        log = Log(path: logPath)
        watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.05, log: log)
        channel = Channel(
            config: Config(host: "harness", fallbackIP: nil, user: "harness",
                           identityFile: "~/.ssh/id_ed25519",
                           remoteAgentPath: "agent/clipwire-agent.py",
                           macPollIntervalMs: 50),
            log: log,
            executablePath: interpreter.path,
            // The whole point of task 1's seam. `host` is ignored: there is
            // no host, the peer is a child process on this machine.
            arguments: { [agent = agentURL.path] _, _ in [agent] })

        wireAgent(channel: channel, watcher: watcher, pasteboard: pasteboard,
                  status: AgentStatus(pid: ProcessInfo.processInfo.processIdentifier,
                                      url: directory.appendingPathComponent("status.json")),
                  log: log,
                  clipStateStore: ClipStateStore(
                      path: directory.appendingPathComponent("clip-state.json").path),
                  clipStateAnnouncement: ClipStateAnnouncement())

        // Observe every inbound frame without replacing what the real wiring
        // does with it -- the same technique AgentWiringTests uses on
        // `watcher.onChange`. `start()` reads the HELLO out of this to check
        // which agent it is talking to.
        let wired = channel.onFrame
        channel.onFrame = { [weak self] frame in
            self?.framesLock.lock()
            self?.received.append(frame)
            self?.framesLock.unlock()
            wired?(frame)
        }
    }

    private var invocationLogPath: String { statePath + ".log" }

    // MARK: - starting, and refusing

    /// Spawns the agent, then asserts the world before returning. Every
    /// failure here is a `Refusal`, never a silently weaker run.
    func start() throws {
        try assertTheFakesOnPathAreTheOnesInThisTree()
        try assertTheAgentExistsInThisTree()

        let channel = self.channel
        let ended = connectionEnded
        let thread = Thread {
            // `attempt(host:)` rather than `run()`: `run()` never returns and
            // would leave a thread respawning agents for the rest of the
            // binary's life. One call is one connection, which is also what
            // sshd gives the real agent.
            _ = channel.attempt(host: "harness")
            ended.signal()
        }
        thread.name = "clipwire pairing harness"
        thread.start()
        connectionStarted = true

        try assertTheAgentTalkingToUsIsTheOneInThisTree()
        try assertTheAgentIsOnTheEventPath()
        try assertTheAgentActuallyForkedOurFakes()

        // `runAgent()` does this too, and separately from `wireAgent` --
        // wiring `onChange` is not the same as arming the timer that calls
        // it. Started only once the world has been asserted, so that the
        // Mac's first observation is of something a test deliberately
        // copied, and after the agent's own seed has been taken.
        watcher.start()
    }

    /// The three tools the agent forks by name resolve to `Tests/fakes`.
    ///
    /// This asserts what WOULD be found. It is not redundant with the
    /// invocation-log check below, which asserts what the agent actually
    /// forked: this one names the specific failure -- an executable bit lost
    /// in a fresh checkout, which would give `gdbus introspect` an `OSError`
    /// and drop the agent onto the polling fallback -- at the point where the
    /// message can still say so.
    private func assertTheFakesOnPathAreTheOnesInThisTree() throws {
        for tool in ["wl-copy", "wl-paste", "gdbus"] {
            // What this configuration is SUPPOSED to have, not what a healthy
            // one has: the polling-fallback test deliberately puts a
            // monitor-only `gdbus` ahead of the fakes, and it must be the
            // event-path assertion that refuses it, not this one. Refusing it
            // here would prove only that the harness can compare two strings.
            let expected = (tool == "gdbus" ? gdbusURL : fakesURL.appendingPathComponent(tool)).path
            guard let found = PairingHarness.resolveOnPath(tool) else {
                throw Refusal.wrongWorld(
                    "\(tool) is not on PATH at all, or is not executable — a fresh checkout " +
                    "that lost the executable bit looks exactly like this",
                    diagnostics: diagnostics())
            }
            guard found == expected else {
                throw Refusal.wrongWorld(
                    "\(tool) on PATH is \(found), not this tree's \(expected)",
                    diagnostics: diagnostics())
            }
        }
    }

    private func assertTheAgentExistsInThisTree() throws {
        guard FileManager.default.isReadableFile(atPath: agentURL.path) else {
            throw Refusal.wrongWorld("no agent at \(agentURL.path)", diagnostics: diagnostics())
        }
    }

    /// The agent that answered is the file we spawned.
    ///
    /// `Channel` was given that file's absolute path inside this tree, so
    /// this is belt and braces -- but the belt is the interesting one: it
    /// compares the version in the HELLO frame against `AGENT_VERSION` read
    /// out of the source file right now. A run that somehow reached an
    /// installed copy (`~/.local/share/clipwire/clipwire-agent.py`, which
    /// `clipwire install` puts on the PC and which a future harness could
    /// reach by accident) fails here instead of quietly testing last week's
    /// protocol.
    private func assertTheAgentTalkingToUsIsTheOneInThisTree() throws {
        try wait(for: "a HELLO from the agent") { self.frame(ofType: .hello) != nil }
        guard let hello = frame(ofType: .hello),
              let payload = try? JSONSerialization.jsonObject(with: hello.payload) as? [String: Any]
        else {
            throw Refusal.wrongWorld("the agent's HELLO payload is not JSON",
                                     diagnostics: diagnostics())
        }
        let source = (try? String(contentsOf: agentURL, encoding: .utf8)) ?? ""
        guard let declared = PairingHarness.pythonStringLiteral(named: "AGENT_VERSION", in: source)
        else {
            throw Refusal.wrongWorld(
                "could not read AGENT_VERSION out of \(agentURL.path) — has it been renamed?",
                diagnostics: diagnostics())
        }
        guard payload["agent"] as? String == declared else {
            throw Refusal.wrongWorld(
                "the agent that answered reports version \(payload["agent"] ?? "nothing"), " +
                "but this tree's source declares \(declared)",
                diagnostics: diagnostics())
        }
        guard payload["protocol"] as? Int == 3 else {
            throw Refusal.wrongWorld(
                "the agent speaks protocol \(payload["protocol"] ?? "nothing"), not 3",
                diagnostics: diagnostics())
        }
    }

    /// The precondition this harness exists for. Read from the agent's own
    /// log, which reaches us as `remote:` lines because `Channel.attempt`
    /// pipes the child's stderr into the same `Log` -- so this is the agent's
    /// own account of which watcher it built, not an inference from timing.
    ///
    /// `make_watcher` emits exactly one line per connection and there are
    /// THREE of them, not two -- the third being the trap. `live` below is a
    /// prefix that the degraded variant ("watching the clipboard through
    /// GPaste, already diagnosed as silent this connection, so polling every
    /// 1.0s") also satisfies, and that variant is a GPaste watcher which has
    /// given up on its event source and is polling at 1s: precisely the state
    /// this precondition exists to refuse, wearing the right words. It is
    /// unreachable today -- `Agent._event_source_degraded` starts false and
    /// `make_watcher` runs once per connection -- but "unreachable today" is
    /// how the whole class of blindness this file removes got here, so it is
    /// checked rather than assumed.
    ///
    /// `live` is deliberately a PREFIX and not the whole line: the rest of it
    /// interpolates SAFETY_NET_POLL_SECONDS, and pinning that here would make
    /// a tuning change to the agent look like a broken harness.
    private func assertTheAgentIsOnTheEventPath() throws {
        let live = "watching the clipboard through GPaste"
        let dead = "GPaste unavailable, falling back to polling"
        let givenUp = "already diagnosed as silent"
        do {
            try wait(for: "the agent to report which watcher it built") {
                let text = self.agentLog()
                return text.contains(live) || text.contains(dead)
            }
        } catch {
            // A timeout here means neither line ever appeared: the agent
            // never got as far as building a watcher at all.
            throw Refusal.wrongWorld(
                "the agent never reported a watcher — it never reached clipboard_became_ready, " +
                "so nothing below would have been exercised",
                diagnostics: diagnostics())
        }
        guard !agentLog().contains(dead) else {
            throw Refusal.wrongWorld(
                "the agent is on the polling fallback: \"\(dead)\". Every clip assertion would " +
                "still pass while the pump, the worker and the safety net never ran once",
                diagnostics: diagnostics())
        }
        guard !agentLog().contains(givenUp) else {
            throw Refusal.wrongWorld(
                "the agent built a GPaste watcher that has already given up on its event " +
                "source and is polling: \"\(givenUp)\". The words say GPaste; the behaviour " +
                "is the fallback",
                diagnostics: diagnostics())
        }

        // The settle, and it is a SECOND signal for a second job. The line
        // above is logged by `make_watcher` before `start()` has forked
        // `gdbus monitor`, let alone before that process has taken its
        // baseline digest of the state file. A change written in that window
        // is absorbed into the baseline, no `Update` follows, and the PC->Mac
        // direction silently does not happen.
        try wait(for: "the fake gdbus monitor to start") {
            self.invocationLog().contains("monitor started")
        }
        // ... and the monitor logs that line immediately BEFORE taking the
        // digest, so a few of its 0.05s poll intervals close the last gap.
        Thread.sleep(forTimeInterval: 0.2)
    }

    /// What the agent actually forked, from the fakes' own invocation log.
    ///
    /// The log is not decoration: the agent spawns `wl-copy` with stderr on
    /// /dev/null, so a fake that failed would fail invisibly, and "the agent
    /// never called us at all" is otherwise indistinguishable from "the
    /// clipboard was empty". A PATH check alone proves what would be found;
    /// this proves what ran.
    private func assertTheAgentActuallyForkedOurFakes() throws {
        try wait(for: "the agent to fork our wl-paste and gdbus") {
            let text = self.invocationLog()
            return text.contains("wl-paste") && text.contains("gdbus introspect -> available")
        }
    }

    // MARK: - acting as the two users

    /// A person copying text on the PC. Writes the shared clipboard state
    /// directly rather than going through `wl-copy`, because that is what
    /// this direction IS: no agent write, no echo, just the selection
    /// changing under GPaste.
    func copyOnThePC(text: String) throws {
        try writeClipboardState(types: PairingHarness.textTypes, body: Data(text.utf8))
    }

    /// The same, with an image. `image/png` alone, which is what the fake
    /// offers for anything outside the text aliases.
    func copyOnThePC(png: Data) throws {
        try writeClipboardState(types: ["image/png"], body: png)
    }

    // MARK: - waiting for the other side

    func waitForThePCsClipboard(toHold body: Data, offeredAs type: String) throws {
        try wait(for: "the PC's clipboard to hold \(describe(body)) as \(type)") {
            guard let state = self.pcClipboard() else { return false }
            return state.body == body && state.types.contains(type)
        }
    }

    func waitForTheMacsPasteboard(toHold kind: ClipKind, _ body: Data) throws {
        try wait(for: "the Mac's pasteboard to hold \(describe(body)) as \(kind)") {
            guard let read = self.pasteboard.read() else { return false }
            return read.kind == kind && read.data == body
        }
    }

    // MARK: - shutting down

    /// Hangs up, waits for the connection thread to finish, and puts the
    /// environment back. Safe to call twice.
    ///
    /// The order matters: the temp directory is removed only after the agent
    /// has exited, because the fakes exit 2 the moment their state file's
    /// directory disappears -- correctly, since a state path pointing nowhere
    /// would make every read report an empty clipboard.
    func stop() {
        guard !stopped else { return }
        stopped = true
        watcher.stop()
        channel.hangUp()
        // Only if there is one to wait for: a harness that refused its world
        // before spawning anything (a lost executable bit, say) would
        // otherwise spend ten seconds waiting for a process that was never
        // started, and report a second, invented failure on top of the real
        // one.
        if connectionStarted, connectionEnded.wait(timeout: .now() + 10) == .timedOut {
            // Not fatal to the test that already passed, but never silent:
            // a python3 left running is exactly the leak `hangUp` exists to
            // prevent, and it would go on to fail later tests instead of
            // this one.
            XCTFail("the agent did not exit within 10s of the channel hanging up\n" +
                    diagnostics())
        }
        for (name, value) in restoreEnvironment {
            if let value { setenv(name, value, 1) } else { unsetenv(name) }
        }
        restoreEnvironment = [:]
        try? FileManager.default.removeItem(at: directory)
    }

    // MARK: - the plumbing

    private func frame(ofType type: FrameType) -> Frame? {
        framesLock.lock()
        defer { framesLock.unlock() }
        return received.first { $0.type == type }
    }

    /// Blocks the test thread until `condition` holds. The channel runs on
    /// its own thread and the watcher on a GCD timer, so sleeping here
    /// blocks nothing that has work to do.
    private func wait(for what: String, timeout: TimeInterval = PairingHarness.waitTimeout,
                      until condition: () -> Bool) throws {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            Thread.sleep(forTimeInterval: 0.02)
        }
        guard !condition() else { return }
        throw Refusal.timedOut(what, diagnostics: diagnostics())
    }

    private func agentLog() -> String {
        // `Log.line` only ENQUEUES the write, so an unflushed read spins to
        // the timeout against a stale file and sends the reader hunting in
        // the wrong place.
        log.flush()
        return (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
    }

    private func invocationLog() -> String {
        (try? String(contentsOfFile: invocationLogPath, encoding: .utf8)) ?? ""
    }

    private func pcClipboard() -> (types: [String], body: Data)? {
        guard let data = FileManager.default.contents(atPath: statePath),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let types = state["types"] as? [String] ?? []
        let body = Data(base64Encoded: state["body"] as? String ?? "") ?? Data()
        return (types, body)
    }

    /// Replaces the shared state, atomically -- the fake `gdbus` digests the
    /// whole file every 50ms and `wl-paste` parses it, so a torn write would
    /// produce both a spurious `Update` and a fake that cannot read its own
    /// state.
    ///
    /// Every other key is carried over rather than rebuilt, which is the same
    /// rule `set_body` follows on the Python side and for the same reason:
    /// `substitute` belongs to whoever armed it, and a fresh object here would
    /// silently disarm substitution mode on the first write a test made. That
    /// mode is what the density half of this release needs; disarming it would
    /// leave those tests passing while proving nothing.
    private func writeClipboardState(types: [String], body: Data) throws {
        var state: [String: Any] = [:]
        if let data = FileManager.default.contents(atPath: statePath),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            state = existing
        }
        state["types"] = types
        state["body"] = body.base64EncodedString()
        // Opaque, and equality is the only operation on it. It exists so that
        // re-copying identical bytes still counts as a change, the way it
        // does on a real clipboard.
        state["generation"] = UUID().uuidString
        state["substitute"] = state["substitute"] ?? false
        try JSONSerialization.data(withJSONObject: state)
            .write(to: URL(fileURLWithPath: statePath), options: .atomic)
    }

    private func describe(_ body: Data) -> String {
        if let text = String(data: body, encoding: .utf8), !text.contains("\u{0}") {
            return "\"\(text)\""
        }
        return "\(body.count) bytes"
    }

    /// Everything a failure needs, in the failure itself: CI has no temp
    /// directory left to go and look in.
    private func diagnostics() -> String {
        """
        --- the Mac's log (the agent's own stderr arrives here as `remote:`) ---
        \(agentLog())
        --- what the agent forked (the fakes' invocation log) ---
        \(invocationLog())
        --- the PC's clipboard ---
        \(String(data: FileManager.default.contents(atPath: statePath) ?? Data(),
                 encoding: .utf8) ?? "(no state file yet)")
        """
    }

    /// A `gdbus` that answers `monitor` and refuses `introspect`, by handing
    /// the real fake the INTERFACE name (`org.gnome.GPaste2`) in place of the
    /// bus name -- which is not a hypothetical mistake but the one this
    /// project already shipped once, leaving the watcher on the polling
    /// fallback forever.
    ///
    /// Written into the temp directory rather than committed beside the
    /// fakes: it is sabotage rigging for exactly one test, and a fourth file
    /// in `Tests/fakes` would eventually be mistaken for a fake worth using.
    private static func writeMonitorOnlyGdbus(in directory: URL, wrapping fakes: URL) throws -> URL {
        let shimDirectory = directory.appendingPathComponent("monitor-only")
        try FileManager.default.createDirectory(at: shimDirectory, withIntermediateDirectories: true)
        let shim = shimDirectory.appendingPathComponent("gdbus")
        let real = fakes.appendingPathComponent("gdbus").path
        // Returns the shim itself, not its directory: the caller needs both,
        // the directory for PATH and the file for the world assertion.
        try Data("""
        #!/usr/bin/env python3
        "A gdbus that answers only `monitor` — see PairingHarnessTests."
        import os, sys
        real = \"\(real)\"
        argv = [("org.gnome.GPaste2" if a == "org.gnome.GPaste" else a) for a in sys.argv[1:]]
        os.execv(real, [real] + argv)

        """.utf8).write(to: shim)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: shim.path)
        return shim
    }

    // MARK: - resolving things by hand

    /// `Process.executableURL` does no PATH lookup and python3's absolute
    /// path is not fixed on macOS (`/usr/bin/python3` is a Command Line Tools
    /// shim; Homebrew's lives elsewhere entirely, and the CI runner's is
    /// somewhere else again), so it is resolved once, here, and its directory
    /// goes on PATH -- which makes the interpreter the fakes get from their
    /// own `#!/usr/bin/env python3` the same one the agent runs under.
    private static func resolvePython3() throws -> URL {
        let which = Process()
        which.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        which.arguments = ["python3", "-c", "import sys; print(sys.executable)"]
        let pipe = Pipe()
        which.standardOutput = pipe
        try which.run()
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        which.waitUntilExit()
        let path = String(decoding: output, as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
        guard which.terminationStatus == 0, !path.isEmpty else {
            throw Refusal.wrongWorld("no python3 on PATH — the agent cannot be run at all",
                                     diagnostics: "")
        }
        return URL(fileURLWithPath: path)
    }

    private static func resolveOnPath(_ name: String) -> String? {
        for directory in (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":") {
            let candidate = URL(fileURLWithPath: String(directory))
                .appendingPathComponent(name).path
            if FileManager.default.isExecutableFile(atPath: candidate) { return candidate }
        }
        return nil
    }

    /// `NAME = "value"` out of a Python source file. Deliberately textual:
    /// the point is to compare what the running agent SAYS against what this
    /// tree's source DECLARES, so anything that imported or executed the file
    /// would be measuring the same process twice.
    private static func pythonStringLiteral(named name: String, in source: String) -> String? {
        guard let assignment = source.range(of: "\n\(name) = \"") else { return nil }
        let rest = source[assignment.upperBound...]
        guard let end = rest.firstIndex(of: "\"") else { return nil }
        return String(rest[..<end])
    }
}

/// Sets an environment variable for THIS process -- which is how the spawned
/// agent gets it, since `Channel` builds no environment of its own and a
/// child inherits its parent's -- and records what was there before, so the
/// harness can put the process back the way it found it.
///
/// Recorded once per name: a second write must not overwrite the original
/// value with an intermediate one.
private func exportEnvironment(_ name: String, _ value: String,
                               restoring restore: inout [String: String?]) {
    if restore[name] == nil {
        restore[name] = ProcessInfo.processInfo.environment[name] as String?
    }
    setenv(name, value, 1)
}

/// `FakePasteboard` with a lock around it.
///
/// The harness genuinely needs one: `handleFrame` writes from the channel's
/// own thread while `PasteboardWatcher`'s timer reads from a GCD queue and
/// the test thread reads to assert -- three threads on one object, which is
/// the production shape (`NSPasteboard.general` is shared exactly this way)
/// and which `FakePasteboard`, built for single-threaded unit tests, does not
/// guard. Reusing it here would have made the harness's own reads a data race
/// and its failures intermittent, which is the last thing a harness whose job
/// is evidence can afford.
private final class HarnessPasteboard: PasteboardReading, PasteboardWriting {
    private let lock = NSLock()
    private var text: Data?
    private var image: Data?
    private var count = 0

    var changeCount: Int {
        lock.lock()
        defer { lock.unlock() }
        return count
    }

    /// A real pasteboard write replaces everything on the board, so both
    /// setters clear the other kind rather than accumulating -- the same rule
    /// `FakePasteboard` follows, and for the same reason.
    func set(_ value: String) {
        lock.lock()
        defer { lock.unlock() }
        text = Data(value.utf8)
        image = nil
        count += 1
    }

    func setImage(_ png: Data) {
        lock.lock()
        defer { lock.unlock() }
        image = png
        text = nil
        count += 1
    }

    /// Text wins, then image: `chooseKind`'s rule, applied by the double so
    /// callers see the precedence a real board gives them. Empty content
    /// reads as nothing at all, matching `SystemPasteboard.read()`.
    func read() -> (kind: ClipKind, data: Data)? {
        lock.lock()
        defer { lock.unlock() }
        if let text, !text.isEmpty { return (.text, text) }
        if let image, !image.isEmpty { return (.image, image) }
        return nil
    }

    func write(kind: ClipKind, data: Data) {
        lock.lock()
        defer { lock.unlock() }
        switch kind {
        case .text: text = data; image = nil
        case .image: image = data; text = nil
        }
        count += 1
    }
}
