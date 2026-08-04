// Tests/clipwireTests/PairingHarness.swift
//
// The harness itself, split out of PairingHarnessTests.swift in v3.2.1. The
// tests stay in that file; this is the machinery they drive. Moved verbatim --
// see docs/superpowers/specs/2026-08-02-v3.2.1-test-split-design.md.
import XCTest
@testable import clipwire

// MARK: -

/// One connection between the two implementations, plus the world it needs.
///
/// Not `Sendable`, and not trying to be: `Channel.attempt(host:)` runs on a
/// thread of its own (as `run()` does in production) and calls back into
/// `handleFrame` from there, so the pasteboard the test thread reads is
/// genuinely shared across threads. `HarnessPasteboard` below carries the
/// lock that makes that safe; everything else here is either immutable after
/// `start()` or guarded by `framesLock`.
final class PairingHarness {
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
    /// diff is readable. The density chunk is what makes this fixture the
    /// density bug's: the fake clipboard's substituting mode drops exactly this
    /// chunk, the way GPaste does, and `pHYs` NOT surviving the round trip is
    /// the defect `testARetinaScreenshotStillComesBackFromThePCAtDoubleSize`
    /// records as unfixed.
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

    /// What GPaste re-offers an image under once it has taken the selection
    /// back -- spec 1.2's "one entry becomes twenty-three".
    ///
    /// THE SAME MEMBERS as `TWENTY_THREE` in
    /// `agent/tests/test_watcher_uuid_tier.py`, deliberately, so the one
    /// scenario staged at two altitudes is staged with one fixture. Copied
    /// rather than shared because nothing can import a Python constant into a
    /// Swift test target; if either list moves, move both.
    ///
    /// IT HOLDS TWENTY-TWO, not the twenty-three the spec measured and the
    /// Python constant's name claims. Said here rather than quietly rounded:
    /// the missing member is not invented back, because the real list came off
    /// a live machine and cannot be reconstructed from this side. Nothing in
    /// the scenario rests on the count -- what has to be true is that the list
    /// still reads as an IMAGE to the agent's `choose_kind`, since `probe()`
    /// returns the offered TYPE LIST for an image and the unchanged BODY for
    /// text, and a body cannot move across a re-offer. `text/ico` below is the
    /// near miss; `choose_kind` matches `text/plain` prefixes only.
    static let gpasteImageTypes = [
        "MULTIPLE", "SAVE_TARGETS", "TARGETS", "TIMESTAMP", "application/ico",
        "audio/x-riff", "image/avif", "image/bmp", "image/ico", "image/icon",
        "image/jpeg", "image/jxl", "image/png", "image/tiff",
        "image/vnd.microsoft.icon", "image/webp", "image/x-MS-bmp",
        "image/x-bmp", "image/x-ico", "image/x-icon", "image/x-win-bitmap",
        "text/ico",
    ]

    let root: URL
    let agentURL: URL
    let pasteboard = HarnessPasteboard()

    /// What the agent's own log says when it built the watcher this harness
    /// requires. A PREFIX of the real line, deliberately: the rest of it
    /// interpolates SAFETY_NET_POLL_SECONDS, and pinning that here would make
    /// a tuning change to the agent look like a broken harness.
    ///
    /// Internal rather than private because a test reads it: `start()`
    /// already refuses a world without this line, but a test whose subject is
    /// "the watcher stayed live" has to assert that in its OWN method, or it
    /// green-lights a run where the agent never started at all.
    static let liveWatcherLine = "watching the clipboard through GPaste"
    /// The one line both implementations log for a reconciliation, byte for
    /// byte -- `FreshnessDecision`'s raw values ARE the PC agent's SEND_MINE /
    /// WAIT_FOR_PEER / DO_NOTHING constants. Which is exactly why reading a
    /// decision out of this log has to filter on the `remote: ` prefix
    /// `Channel.attempt` gives the agent's stderr: the two sides' lines are
    /// otherwise indistinguishable.
    private static let reconciliation = "reconciled with the peer: "

    private let fakesURL: URL
    /// Which `gdbus` this configuration put on `PATH` — the fake, or the
    /// monitor-only shim the refusal test sabotages it with.
    private let gdbusURL: URL
    private let directory: URL
    private let statePath: String
    private let logPath: String
    /// The two clip-state stores, one per side, both inside this run's temp
    /// directory. They are what a reconnect actually reconciles -- each side's
    /// answer to "what do I hold and how old is it" -- and which of
    /// `.imageClip`'s two branches ran is visible in the Mac's as whether the
    /// two hashes differ, so the test reads them directly rather than
    /// inferring their contents from behaviour.
    private let macClipStatePath: String
    private let agentClipStatePath: String
    private let log: Log
    private let watcher: PasteboardWatcher
    private let channel: Channel
    private let connectionEnded = DispatchSemaphore(value: 0)
    private var restoreEnvironment: [String: String?] = [:]
    private var connectionStarted = false
    private var stopped = false
    /// How many reconciliation lines each side had logged when the CURRENT
    /// connection was dialed, so that a line from an earlier one can never
    /// answer for this one. Every connection produces exactly one per side.
    private var macsReconciliationsAtConnect = 0
    private var pcsReconciliationsAtConnect = 0

    private let framesLock = NSLock()
    private var received: [Frame] = []

    /// `tierSeconds` overrides the spawned agent's polling intervals by
    /// exporting environment variables before the agent is spawned -- see the
    /// `exportEnvironment` calls below, and `_env_seconds` in the agent, which
    /// is what reads them back out. `nil` in any member means "leave that one
    /// at production's constant," which is what every call site gets from the
    /// default here: there is no separate configuration type in this file to
    /// hang the option on (`eventSource` and `substituting` are init
    /// parameters only, not stored properties, for the same reason -- nothing
    /// after `init` needs either one back), so the option lives here, beside
    /// them.
    ///
    /// THREE MEMBERS, AND `slow` IS NOT THE ONE THAT MAKES THE SLOW TIER
    /// TICK. That trap cost this release a task: the names come from the
    /// agent's own variables, and there they mean
    ///
    /// - `fast`      -- the fast tier's thread, `CLIPWIRE_FAST_TIER_SECONDS`;
    /// - `safetyNet` -- the rate the SLOW TIER ACTUALLY TICKS AT,
    ///   `CLIPWIRE_SAFETY_NET_SECONDS`. Every verdict `_observe_tick` reaches
    ///   is measured in this one, so it is the only member that lets a test
    ///   here exercise the safety net at all;
    /// - `slow`      -- `CLIPWIRE_SLOW_TIER_SECONDS`, which starts no timer:
    ///   its one consumer is the agent's uuid-failure fallback threshold. It
    ///   defaults to whatever `safetyNet` resolved to, so a test that wants
    ///   fast ticks sets `safetyNet` and leaves this `nil`.
    ///
    /// Spelled out rather than left to the names because setting `slow` alone
    /// looks exactly like it should work, does nothing observable, and leaves
    /// a safety-net test passing on a 30-second tick that never fired inside
    /// its window.
    init(eventSource: EventSource = .gpaste, substituting: Bool = false,
         tierSeconds: (fast: Double?, slow: Double?, safetyNet: Double?)
             = (nil, nil, nil)) throws {
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
        macClipStatePath = directory.appendingPathComponent("clip-state.json").path

        let runtime = directory.appendingPathComponent("runtime")
        let stateHome = directory.appendingPathComponent("state")
        // `clip_state_path()`'s own layout: `$XDG_STATE_HOME/clipwire/clip-state.json`.
        agentClipStatePath = stateHome
            .appendingPathComponent("clipwire/clip-state.json").path
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
        // Sub-second tiers, so a test can exercise in seconds what production
        // does in minutes. Only set when `tierSeconds` asks: an unset
        // variable is what production runs, and a harness that always
        // overrode them would never exercise the real defaults, only ever
        // its own substitute for them.
        if let fast = tierSeconds.fast {
            exportEnvironment("CLIPWIRE_FAST_TIER_SECONDS", "\(fast)", restoring: &restore)
        }
        if let slow = tierSeconds.slow {
            exportEnvironment("CLIPWIRE_SLOW_TIER_SECONDS", "\(slow)", restoring: &restore)
        }
        if let safetyNet = tierSeconds.safetyNet {
            exportEnvironment("CLIPWIRE_SAFETY_NET_SECONDS", "\(safetyNet)", restoring: &restore)
        }
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
                  clipStateStore: ClipStateStore(path: macClipStatePath),
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

        // Armed HERE, in the initializer, and not from a test body: the fake
        // `gdbus monitor` digests this whole file once at startup and treats
        // any later difference as a clipboard change, so a write made after
        // the agent is up would raise a spurious `Update` for a selection
        // nobody copied to. Before `start()` there is no monitor to fool.
        if substituting {
            try mutateClipboardState { $0["substitute"] = true }
        }
    }

    private var invocationLogPath: String { statePath + ".log" }

    // MARK: - starting, and refusing

    /// Spawns the agent, then asserts the world before returning. Every
    /// failure here is a `Refusal`, never a silently weaker run.
    func start() throws {
        try assertTheFakesOnPathAreTheOnesInThisTree()
        try assertTheAgentExistsInThisTree()

        dialAgain()
        try assertTheWorldIsTheOneWeCanTestIn(after: Marks())

        // `runAgent()` does this too, and separately from `wireAgent` --
        // wiring `onChange` is not the same as arming the timer that calls
        // it. Started only once the world has been asserted, so that the
        // Mac's first observation is of something a test deliberately
        // copied, and after the agent's own seed has been taken.
        watcher.start()
    }

    /// Drops the channel and dials again, which spawns a BRAND NEW agent --
    /// exactly what sshd gives the real one, one process per connection, and
    /// the event that freshness reconciliation exists for. `run()` offers no
    /// way to say "now reconnect", which is why `attempt(host:)` is driven
    /// directly here.
    ///
    /// The current connection's reconciliation is waited for BEFORE the drop,
    /// not out of politeness: the baselines recorded below are what keep an
    /// earlier connection's line from answering for the next one, and a line
    /// still in flight would make them wrong by one.
    ///
    /// Then every precondition `start()` asserts is asserted again, against
    /// those baselines. The stale-read hazard is the whole reason they are
    /// counts rather than `contains`: the agent's log and the fakes'
    /// invocation log both ACCUMULATE across connections, so a second
    /// connection would satisfy every one of them on the first connection's
    /// output and return instantly, having confirmed nothing about the agent
    /// now running. That is the degrade-rather-than-refuse this whole file
    /// exists to make impossible.
    ///
    /// `whileApart` runs in the window where there is genuinely no agent: the
    /// old one has exited and the new one has not been dialed. That window is
    /// not a convenience, it is the only place two of this file's tests can
    /// be staged at all. "The PC's clipboard changed while the Mac was away"
    /// is what `resolve_startup_state` exists for -- an empty clipboard after
    /// a reboot or a lock, and a clip the user copied on the PC while the Mac
    /// slept -- and it is unreachable while an agent is watching, because a
    /// watched change goes over the live connection instead and never reaches
    /// a reconciliation. Its writes go straight to the shared state file, the
    /// same way `copyOnThePC` does, so nothing is forked and the `Marks` taken
    /// above stay honest.
    func reconnect(whileApart: (() throws -> Void)? = nil) throws {
        try decisionOnThisConnection()
        try waitForThePCToReconcileOnThisConnection()
        let marks = Marks(self)
        channel.hangUp()
        guard connectionEnded.wait(timeout: .now() + 10) != .timedOut else {
            throw Refusal.timedOut("the agent to exit after the channel hung up — a leaked " +
                                   "python3 would go on to fail a later test instead of this one",
                                   diagnostics: diagnostics())
        }
        // With the old agent gone and the new one not yet dialed, so a change
        // made here is one nothing observed -- which is the whole point.
        try whileApart?()
        // After the old agent has exited and its stderr handler has been
        // torn down (`attempt()` does both before returning), so nothing can
        // still be appending to either log as these are taken.
        macsReconciliationsAtConnect = reconciliations(remote: false).count
        pcsReconciliationsAtConnect = reconciliations(remote: true).count
        dialAgain()
        try assertTheWorldIsTheOneWeCanTestIn(after: marks)
    }

    private func dialAgain() {
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
    }

    private func assertTheWorldIsTheOneWeCanTestIn(after marks: Marks) throws {
        try assertTheAgentTalkingToUsIsTheOneInThisTree(after: marks)
        try assertTheAgentIsOnTheEventPath(after: marks)
        try assertTheAgentActuallyForkedOurFakes(after: marks)
    }

    /// What each per-connection signal stood at before a connection was
    /// dialed. Zero for the first one, which is what makes `start()` and
    /// `reconnect()` share one set of assertions rather than two that can
    /// drift apart.
    private struct Marks {
        var hellos = 0
        var watcherReports = 0
        var monitorStarts = 0
        var introspects = 0
        var pastes = 0

        init() {}

        init(_ harness: PairingHarness) {
            hellos = harness.frames(ofType: .hello).count
            watcherReports = harness.agentLog().occurrences(of: PairingHarness.liveWatcherLine)
            let invocations = harness.invocationLog()
            monitorStarts = invocations.occurrences(of: "monitor started")
            introspects = invocations.occurrences(of: "gdbus introspect -> available")
            pastes = invocations.occurrences(of: " wl-paste ")
        }
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
    private func assertTheAgentTalkingToUsIsTheOneInThisTree(after marks: Marks) throws {
        // Against the mark, and the NEWEST hello rather than the first one:
        // `received` accumulates across connections, so `first` would hand
        // back the previous agent's handshake, pass every check below, and
        // return before this connection had said anything at all.
        try wait(for: "a HELLO from the agent") {
            self.frames(ofType: .hello).count > marks.hellos
        }
        guard let hello = frames(ofType: .hello).last,
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
    private func assertTheAgentIsOnTheEventPath(after marks: Marks) throws {
        let live = PairingHarness.liveWatcherLine
        let dead = "GPaste unavailable, falling back to polling"
        let givenUp = "already diagnosed as silent"
        do {
            // The live line is counted against the mark (see `reconnect()`);
            // the two refusals stay whole-log `contains`, which is strictly
            // more conservative -- once either has been seen on ANY
            // connection of this run, nothing measured afterwards is worth
            // believing.
            try wait(for: "the agent to report which watcher it built") {
                let text = self.agentLog()
                return text.occurrences(of: live) > marks.watcherReports || text.contains(dead)
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
            self.invocationLog().occurrences(of: "monitor started") > marks.monitorStarts
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
    private func assertTheAgentActuallyForkedOurFakes(after marks: Marks) throws {
        try wait(for: "the agent to fork our wl-paste and gdbus") {
            let text = self.invocationLog()
            return text.occurrences(of: " wl-paste ") > marks.pastes
                && text.occurrences(of: "gdbus introspect -> available") > marks.introspects
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

    /// GPaste taking the selection back and re-offering the same picture
    /// under its own type list -- spec 1.2, and the production incident this
    /// release exists to prevent. THREE things are true of it at once, and
    /// each is load-bearing:
    ///
    /// - the offered TYPES change, so the agent's `probe()` token moves;
    /// - `generation` does NOT, so the fake `gdbus`'s `history_uuid` -- which
    ///   it derives from that field alone -- stays frozen. That is what makes
    ///   this a re-offer rather than a copy: GPaste re-offering its own
    ///   content creates no history entry;
    /// - `body` does NOT, because the picture did not change;
    /// - and `silent` is set, which makes the fake `gdbus monitor` absorb
    ///   this one state change into its baseline and emit no `Update`.
    ///
    /// Deliberately NOT routed through `writeClipboardState`, which stamps a
    /// fresh `generation` on every write: that is right for a person copying
    /// and wrong here, and going through it would move the uuid, hand the
    /// slow tier the very evidence it is supposed to be denied, and leave the
    /// test passing for a reason unrelated to the fix.
    ///
    /// `silent` DOES NOT SELF-CLEAR. Only an agent write through `wl-copy`
    /// clears it (`fake_clipboard.set_body`), and `mutateClipboardState`
    /// carries every untouched key forward -- so this must be the last direct
    /// clipboard write a test makes, or every later one is invisible to the
    /// monitor too.
    func silentTakeover(types: [String]) throws {
        try mutateClipboardState { state in
            state["types"] = types
            state["silent"] = true
        }
    }

    /// A PC that comes back holding nothing: a reboot, or the locked session
    /// the README documents. An empty `types` list is the fake's own spelling
    /// of it -- `wl-paste` refuses with exit 1 ("No selection") for exactly
    /// that state, which is what the agent reads as an empty clipboard and
    /// announces as a null `sha256`.
    ///
    /// Deliberately not "delete the state file". That is also an empty
    /// selection as far as `fake.load` is concerned, but it takes the state
    /// path's own directory check into territory the fakes exit 2 for, and an
    /// empty selection modelled two ways is one way too many.
    func emptyThePCsClipboard() throws {
        try writeClipboardState(types: [], body: Data())
    }

    // MARK: - waiting for the other side

    func waitForThePCsClipboard(toHold body: Data, offeredAs type: String) throws {
        try wait(for: "the PC's clipboard to hold \(describe(body)) as \(type)") {
            guard let state = self.pcClipboard() else { return false }
            return state.body == body && state.types.contains(type)
        }
    }

    /// How many times the agent's own `wl-paste --list-types` came back
    /// offering exactly this list, read from the fakes' invocation log.
    ///
    /// The only window this side has onto the agent's slow tier. `_armed`,
    /// `_signals` and `_last_uuid` are fields of a Python object in another
    /// process; what crosses the boundary is what the agent FORKED, and this
    /// is the one fork whose recorded output says which selection the tier
    /// actually saw. So it is two claims in one number: that the tier is
    /// ticking at all, and that a tick observed the divergence rather than a
    /// test merely having written one to a file.
    ///
    /// IT COUNTS PROBES, NOT TICKS, and the gap is not closable from here:
    /// the worker's own clipboard read forks `--list-types` too, so a tick
    /// that signals the worker contributes two. Every caller therefore asks
    /// for a count that is safe under that inflation -- see
    /// `waitForTheAgentToProbeAndSee`, which documents the arithmetic at the
    /// one place it is relied on.
    func probesThatSaw(_ types: [String]) -> Int {
        invocationLog().occurrences(of: "wl-paste --list-types -> "
                                    + types.joined(separator: " ") + "\n")
    }

    /// Waits until the agent's FAST tier has read GPaste's history uuid at
    /// least once, from the fakes' own invocation log.
    ///
    /// A precondition with teeth, not a settle. The slow tier's verdict rests
    /// on `uuid_frozen`, which is a DELTA between two of its own ticks -- and
    /// the earlier of the two records whatever the fast tier had read by
    /// then, `None` included. So a slow tick that lands before the fast
    /// tier's first successful call poisons the comparison: the next tick
    /// sees "unmeasured", reaches no verdict, and a test asserting that no
    /// verdict was reached passes WITHOUT THE SCENARIO HAVING RUN.
    ///
    /// Not hypothetical. Measured at roughly one run in six, and the cause is
    /// documented in `Tests/fakes/fake_clipboard.py`: an agent's FIRST fork of
    /// a fake has been seen taking 0.2-0.8 s, which at a sub-second fast tier
    /// is several slow ticks. Waiting for the call itself removes the guess.
    func waitForTheAgentsFastTierToReadAHistoryUuid() throws {
        try wait(for: "the agent's fast tier to read a history uuid through gdbus") {
            self.invocationLog().contains("call GetElementAtIndex(0) -> uuid")
        }
    }

    /// Waits until `count` of the agent's probes have come back offering
    /// exactly this list.
    ///
    /// `count` is a number of PROBES and callers want a number of TICKS, so
    /// the arithmetic lives here rather than at each call site. In a quiet
    /// stretch every probe is a tick; the inflation is bounded, because the
    /// only other thing that forks `--list-types` is the worker's clipboard
    /// read, and the worker runs at most once per selection change (the tick
    /// that observes a moved token signals it; the ticks that see a settled
    /// one do not). So for a selection the agent has just started offering,
    /// `n + 1` probes guarantee at least `n` ticks, and for one it has been
    /// offering all along there is no worker read at all.
    ///
    /// THE `+ 1` BELOW IS NOT SLACK, and it was measured rather than
    /// reasoned into existence: without it this wait returns too early and
    /// the test above it goes GREEN AGAINST AN AGENT WITH THE FIX REMOVED.
    /// The fake writes its invocation-log line while it is SERVING the probe,
    /// which is strictly before the agent has read the result, let alone
    /// judged it -- so the count reaching its target says the probe happened,
    /// never that a verdict followed. Waiting for one further probe is what
    /// closes it, and it closes it exactly rather than probably: the poll
    /// thread is strictly sequential -- probe, `_on_tick`, wait, probe -- so
    /// a LATER probe existing is proof that the previous one's verdict has
    /// already been reached and logged. A sleep would have been a guess about
    /// the same thing.
    func waitForTheAgentToProbeAndSee(_ types: [String], atLeast count: Int) throws {
        try wait(for: "\(count) of the agent's own probes to see \(types.count) offered types, "
                 + "and a further probe proving the last of them was judged") {
            self.probesThatSaw(types) >= count + 1
        }
    }

    func waitForTheMacsPasteboard(toHold kind: ClipKind, _ body: Data) throws {
        try wait(for: "the Mac's pasteboard to hold \(describe(body)) as \(kind)") {
            guard let read = self.pasteboard.read() else { return false }
            return read.kind == kind && read.data == body
        }
    }

    // MARK: - watching the substitution, and the two stores

    /// Waits until the fake clipboard has actually SUBSTITUTED an image the
    /// agent wrote -- GPaste taking the selection over and re-offering
    /// something else -- and hands back the bytes it stored instead.
    ///
    /// Read from the fakes' own invocation log (`... 95 bytes substituted as
    /// 84 bytes`) rather than inferred from the stored body differing. The two
    /// are not the same claim: `wl-copy` falls back to storing what it was
    /// given whenever `substitute_png` raises, and says so in that same line, so
    /// a fixture the re-encoder could not model would leave the clipboard
    /// holding the original and every assertion afterwards passing against a
    /// substitution that never happened.
    func waitForThePCToReEncodeTheImageItWasSent() throws -> Data {
        try wait(for: "the fake clipboard to re-encode the image the agent wrote") {
            self.invocationLog().contains("bytes substituted as")
        }
        guard let state = pcClipboard(), state.types.contains("image/png"), !state.body.isEmpty
        else {
            throw Refusal.wrongWorld(
                "the fake clipboard re-encoded an image but is not offering one",
                diagnostics: diagnostics())
        }
        return state.body
    }

    /// Waits for the PC's agent to record a hash in its own clip-state store.
    ///
    /// For the re-offer that is not merely a settle, it is the precondition of
    /// the reconnect: `_consume_image_reoffer` is what replaces the hash of
    /// what was HANDED to `wl-copy` with the hash of what the clipboard
    /// actually offers, at `peer_ts + REOFFER_TS_NUDGE_SECONDS`. Reconnect
    /// before it lands and the PC's next startup finds a clipboard its store
    /// does not describe -- and correctly says so.
    func waitForThePCsAgentToRecord(_ sha256: String) throws {
        try wait(for: "the PC's agent to record \(sha256.prefix(12))… in its clip-state store") {
            self.pcsClipState()?["sha256"] as? String == sha256
        }
    }

    func waitForTheMacsStoreToRecord(_ sha256: String) throws {
        try wait(for: "the Mac's store to record \(sha256.prefix(12))… as its canonical hash") {
            self.macsClipState()?.sha256 == sha256
        }
    }

    /// The Mac's persisted state, read through this side's own store rather
    /// than as JSON: a future encoding change should break the dedicated store
    /// tests, not this one.
    func macsClipState() -> ClipState? {
        ClipStateStore(path: macClipStatePath).load()
    }

    /// The PC's, as raw JSON. Deliberately not decoded through `ClipState`:
    /// this is the Python side's file, and reading it with this side's decoder
    /// would quietly assert the two agree about the format at a point where
    /// the test only wants to know what the agent wrote.
    func pcsClipState() -> [String: Any]? {
        guard let data = FileManager.default.contents(atPath: agentClipStatePath) else { return nil }
        return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
    }

    // MARK: - counting the content that actually crossed

    /// How many clip frames of EITHER kind the PC has sent this Mac, across
    /// every connection this harness has made. A caller after "this
    /// connection's" count takes a baseline first, the same way `Marks` does.
    ///
    /// Both types, not just `.imageClip`: "no frame in either direction" is
    /// a claim about content crossing at all, and a suppression that leaked
    /// a `.clip` instead would satisfy a count that only looked for images.
    func clipsFromThePC() -> Int {
        frames(ofType: .clip).count + frames(ofType: .imageClip).count
    }

    /// The other direction, and it cannot be counted the same way: frames
    /// this side SENDS never pass through `channel.onFrame`. What witnesses
    /// them is the far end actually acting on one -- `_write_clip` forking
    /// `wl-copy` -- read from the fakes' own invocation log, which is the
    /// same evidence `assertTheAgentActuallyForkedOurFakes` already trusts.
    ///
    /// Strictly this counts writes rather than frames, and that asymmetry is
    /// the honest one: a clip frame the PC decoded and refused to apply is
    /// not content crossing, and a write with no frame behind it cannot
    /// happen -- `_write_clip` has exactly two callers, both on the receive
    /// path.
    func writesOnThePC() -> Int {
        invocationLog().occurrences(of: " wl-copy ")
    }

    // MARK: - reading a decision back out of the log

    /// The decision THIS connection's reconciliation reached on the Mac,
    /// waiting for it if it has not happened yet.
    ///
    /// Indexed from the baseline `reconnect()` records rather than taking the
    /// last line, so a connection that somehow logged two would still be read
    /// as its own first decision instead of silently reporting a later one.
    @discardableResult
    func decisionOnThisConnection() throws -> String {
        try wait(for: "the Mac to reconcile with the peer on this connection") {
            self.reconciliations(remote: false).count > self.macsReconciliationsAtConnect
        }
        return reconciliations(remote: false)[macsReconciliationsAtConnect]
    }

    /// The same for the PC, and used only as an ordering guarantee: its
    /// reconciliation line is written to the stderr this side pipes into the
    /// shared log, AFTER its own startup announcement. So once this returns,
    /// anything that announcement would have logged is already in the file.
    func waitForThePCToReconcileOnThisConnection() throws {
        try wait(for: "the PC to reconcile with us on this connection") {
            self.reconciliations(remote: true).count > self.pcsReconciliationsAtConnect
        }
    }

    /// The PC's own verdict, indexed from its own baseline exactly as the
    /// Mac's is.
    ///
    /// Not redundant with `clipsFromThePC()`, and the nil case is what proves
    /// it: with the present-value guard dropped on the PYTHON side alone, the
    /// Mac still resolves `sendMine` and still sends -- the PC applies any
    /// clip it is handed, whatever it resolved -- so the clip arrives, the
    /// counts are satisfied, and the only witness that the PC stood down when
    /// it should have been waiting is this line. One rule with two
    /// implementations needs a reading of each, or a break in either is a
    /// break in neither.
    @discardableResult
    func pcsDecisionOnThisConnection() throws -> String {
        try waitForThePCToReconcileOnThisConnection()
        return reconciliations(remote: true)[pcsReconciliationsAtConnect]
    }

    /// Whether the shared log holds a line. Both sides' lines land in it --
    /// `Channel.attempt` pipes the agent's stderr in with a `remote: ` prefix
    /// -- so a search without one covers the PC too, which is what
    /// `clipboard changed while apart` needs: v3's false one was found there.
    func logHolds(_ line: String) -> Bool {
        agentLog().contains(line)
    }

    /// The decision word from each reconciliation line one side logged, oldest
    /// first. `remote:` is the whole discriminator: the two implementations
    /// share this sentence byte for byte, on purpose, so nothing in the text
    /// after the prefix says which machine wrote it.
    private func reconciliations(remote: Bool) -> [String] {
        agentLog().split(separator: "\n").compactMap { line -> String? in
            guard line.contains(PairingHarness.reconciliation),
                  line.contains("remote: ") == remote,
                  let start = line.range(of: PairingHarness.reconciliation)
            else { return nil }
            let rest = line[start.upperBound...]
            guard let end = rest.firstIndex(of: " ") else { return String(rest) }
            return String(rest[..<end])
        }
    }

    // MARK: - draining, before asserting that something is absent

    /// Hangs up and waits for the agent to exit, so that everything it wrote
    /// to its stderr is in the shared log before a test asserts on what is
    /// NOT in there.
    ///
    /// THE RACE THIS CLOSES IS NOT THEORETICAL AND WAS NOT CHEAP. `agentLog()`
    /// flushes THIS side's queue, but a line the PC wrote reaches that file
    /// only once `Channel.attempt`'s readability handler has read it -- so a
    /// negative assertion can be evaluated microseconds before the very line
    /// it denies arrives. Measured with the agent's fix deliberately removed:
    /// `testAGPasteReofferDoesNotDegradeTheConnection` caught the regression
    /// on 2 runs in 3, and reported success on the third. Counting the
    /// agent's own probes proves the TICK happened and cannot prove its log
    /// line crossed a pipe; only this can. The density test above documents
    /// the same hazard and closes it by waiting for a later line from the PC,
    /// which works only where a later line is guaranteed -- for a verdict
    /// that must never be reached, there is none.
    ///
    /// `attempt()` drains that handler and tears it down before returning,
    /// and this waits for exactly that.
    ///
    /// DELIBERATELY NOT `stop()`, which does the same two things and then
    /// deletes the temp directory the log file lives in -- after which every
    /// `logHolds` reads an empty string and every negative assertion in the
    /// suite passes for the worst reason there is.
    func hangUpAndDrainTheAgentsLog() throws {
        channel.hangUp()
        guard connectionEnded.wait(timeout: .now() + 10) != .timedOut else {
            throw Refusal.timedOut("the agent to exit so its log could be read to the end",
                                   diagnostics: diagnostics())
        }
        // Put the count back. `stop()` waits on this same semaphore, and a
        // consumed one would leave it blocking for ten seconds and then
        // reporting a leaked python3 that had in fact exited right here.
        connectionEnded.signal()
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

    /// Every frame of a kind, in arrival order -- across ALL connections this
    /// harness has made. A caller after "this connection's" one takes the
    /// count as a baseline first; see `Marks`.
    private func frames(ofType type: FrameType) -> [Frame] {
        framesLock.lock()
        defer { framesLock.unlock() }
        return received.filter { $0.type == type }
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

    /// The PC clipboard's opaque change token -- the field the fake `gdbus`
    /// derives GPaste's history uuid from, and therefore the difference
    /// between "a new entry was created" and "the same entry was re-offered".
    ///
    /// Exposed so a test can pin that its own staging stayed the scenario it
    /// meant to stage. `silentTakeover` leaves this alone by construction;
    /// a version that stopped doing so would turn the re-offer into a COPY,
    /// the agent's uuid would move, no verdict could be reached for a reason
    /// unrelated to the fix, and every assertion downstream would still pass.
    func pcsClipboardGeneration() -> String? {
        guard let data = FileManager.default.contents(atPath: statePath),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return state["generation"] as? String
    }

    private func pcClipboard() -> (types: [String], body: Data)? {
        guard let data = FileManager.default.contents(atPath: statePath),
              let state = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        let types = state["types"] as? [String] ?? []
        let body = Data(base64Encoded: state["body"] as? String ?? "") ?? Data()
        return (types, body)
    }

    private func writeClipboardState(types: [String], body: Data) throws {
        try mutateClipboardState { state in
            state["types"] = types
            state["body"] = body.base64EncodedString()
            // Opaque, and equality is the only operation on it. It exists so
            // that re-copying identical bytes still counts as a change, the
            // way it does on a real clipboard.
            state["generation"] = UUID().uuidString
        }
    }

    /// Changes the shared state, atomically -- the fake `gdbus` digests the
    /// whole file every 50ms and `wl-paste` parses it, so a torn write would
    /// produce both a spurious `Update` and a fake that cannot read its own
    /// state.
    ///
    /// Every key not touched by `mutate` is carried over rather than rebuilt,
    /// which is the same rule `set_body` follows on the Python side and for
    /// the same reason: `substitute` belongs to whoever armed it, and a fresh
    /// object here would silently disarm substitution mode on the first write
    /// a test made. That mode is what the density half of this release needs;
    /// disarming it would leave those tests passing while proving nothing.
    private func mutateClipboardState(_ mutate: (inout [String: Any]) -> Void) throws {
        var state: [String: Any] = [:]
        if let data = FileManager.default.contents(atPath: statePath),
           let existing = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            state = existing
        }
        state["substitute"] = state["substitute"] ?? false
        mutate(&state)
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

/// How many times a needle appears, non-overlapping.
///
/// Every per-connection signal this harness waits on is counted rather than
/// searched for, because both logs it reads ACCUMULATE across connections: a
/// `contains` that was exactly right for the first connection answers instantly
/// -- and wrongly -- for the second, on evidence the previous agent produced.
/// That is the degrade-rather-than-refuse failure this file exists to make
/// impossible, so it must not arrive through the harness's own plumbing.
private extension String {
    func occurrences(of needle: String) -> Int {
        guard !needle.isEmpty else { return 0 }
        return components(separatedBy: needle).count - 1
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
final class HarnessPasteboard: PasteboardReading, PasteboardWriting {
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
