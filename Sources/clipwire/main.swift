// Sources/clipwire/main.swift
import Foundation

// Namespaced rather than bare top-level `let`: main.swift is the file
// Swift treats as the literal entry point, so top-level statements here
// are correct and required (see the bottom of this file) -- but a bare
// top-level `let` is still a script-local binding, not a genuine shared
// constant, and every other constant in this target (FrameConstants,
// StatusConstants, ChannelConstants) is namespaced the same way for
// consistency and so nothing about its initialization depends on which
// file happens to be main.
enum AgentPaths {
    static let statusURL = URL(fileURLWithPath: expandTilde("~/.local/state/clipwire/status.json"))
    static let logPath = "~/.local/state/clipwire/clipwire.log"
}

enum ProtocolConstants {
    static let version = 1
    static let agentVersion = "0.1.0"

    static var helloPayload: Data {
        (try? JSONEncoder().encode(HelloPayload(version: version, agent: agentVersion))) ?? Data()
    }
}

/// Wire shape of a hello frame's JSON payload: `{"protocol": <int>, "agent": "<version>"}`.
/// `version` maps to the wire key `protocol` (a Swift keyword) via `CodingKeys`,
/// the same pattern `Config` already uses for its own snake_case wire keys.
private struct HelloPayload: Codable {
    let version: Int
    let agent: String?

    enum CodingKeys: String, CodingKey {
        case version = "protocol"
        case agent
    }
}

/// Parses a peer's hello payload. Returns `nil` if the JSON cannot be
/// decoded at all -- `runAgent()`'s `.hello` case treats that the same as
/// a version mismatch, since an undecodable declaration is not something
/// this side can confirm as compatible.
func decodeHello(_ payload: Data) -> (version: Int, agent: String?)? {
    guard let decoded = try? JSONDecoder().decode(HelloPayload.self, from: payload) else {
        return nil
    }
    return (decoded.version, decoded.agent)
}

/// Owns the single in-memory `Status` for this run and serializes every
/// read/mutation behind a lock.
///
/// Three independent execution contexts touch this after startup:
/// `Channel`'s `onFrame`/`onStateChange` callbacks run synchronously on
/// whichever thread calls `channel.run()` (this file's own top-level code,
/// blocked inside `run()` for the rest of the process's life);
/// `PasteboardWatcher`'s poll timer and the heartbeat timer below are two
/// independent `DispatchSourceTimer`s, both scheduled on
/// `.global(qos: .utility)` -- a CONCURRENT queue, so the two timers' own
/// handlers can run on two different worker threads at the same instant,
/// not merely concurrently with the run() thread. A bare `var status:
/// Status` captured directly by all of these closures (the brief's own
/// shape) compiles clean with no warning under Swift 6 -- confirmed
/// empirically by building that exact code: none of
/// `onChange`/`onFrame`/`onStateChange`/`setEventHandler`'s closure
/// parameters are declared `@Sendable`, so the compiler has no static hook
/// to flag the capture -- but it is still a real, silent data race:
/// unsynchronized threads can mutate different fields of the same struct
/// with no ordering guarantee, and two concurrent `Status.write(to:)`
/// calls would race the same temp-file-then-replace pair. This class
/// applies the same fix, to the same class of hazard, that Task 13 already
/// applied to `PasteboardWatcher` (a `stateLock` around `changeCount`/
/// `echo`) and Task 14 to `Channel` (a serial `writeQueue` around pipe
/// writes).
final class AgentStatus: @unchecked Sendable {
    private let lock = NSLock()
    private let url: URL
    private let startedAt = Date()
    private var status: Status
    // Set when the peer's declared protocol version does not match ours;
    // cleared the next time a MATCHING hello is processed. `Channel.run()`
    // reports the eventual drop with a generic "channel closed" reason
    // once the peer notices the same mismatch on its own side and exits --
    // see `applyChannelState` for why that generic reason must not
    // overwrite this more specific one.
    private var protocolMismatchReason: String?
    // True the instant Channel has told us anything at all -- a state
    // change or a frame. Needed because `Channel.run()`'s never-established
    // path (the ordinary "PC is off" case) never calls `onStateChange`:
    // nothing else would ever replace the startup placeholder in
    // `status.reason`, so a Mac that boots with the peer off would show
    // "down — starting" for as long as it stays off, which is the single
    // most common normal state this whole file exists to explain. See
    // `tickHeartbeat`, the one place that already has the reconnect count
    // needed to say something better.
    private var hasHeardFromChannel = false

    init(pid: Int32, url: URL) {
        self.url = url
        self.status = Status(state: .down, reason: "starting", heartbeat: Date(),
                              pid: pid, lastSentAt: nil, lastReceivedAt: nil, reconnects: 0)
    }

    private func locked<T>(_ body: () -> T) -> T {
        lock.lock()
        defer { lock.unlock() }
        return body()
    }

    /// A consistent snapshot, for callers that need to read multiple
    /// fields together without racing an in-flight mutation.
    func snapshot() -> Status {
        locked { status }
    }

    func writeInitial() {
        try? snapshot().write(to: url)
    }

    func recordSent() {
        locked { status.lastSentAt = Date() }
    }

    func recordReceived() {
        locked { status.lastReceivedAt = Date() }
    }

    func recordHelloMatched() {
        locked {
            hasHeardFromChannel = true
            protocolMismatchReason = nil
            status.state = .up
            status.reason = nil
        }
    }

    func recordProtocolMismatch(_ reason: String) {
        locked {
            hasHeardFromChannel = true
            protocolMismatchReason = reason
            status.state = .down
            status.reason = reason
        }
    }

    /// Applies a `Channel.onStateChange` transition. A pinned protocol
    /// mismatch reason wins over whatever generic reason the channel
    /// reports for the drop that follows it, so `clipwire status` shows
    /// the specific explanation instead of "channel closed".
    func applyChannelState(_ state: ChannelState, _ reason: String?) {
        locked {
            hasHeardFromChannel = true
            status.state = state
            status.reason = protocolMismatchReason ?? reason
        }
    }

    /// Ticks the heartbeat and, while the channel has never yet reported
    /// anything at all, replaces the startup placeholder with the actual
    /// attempt count and elapsed time -- see `hasHeardFromChannel`'s doc
    /// comment for why this specific gap is fixed here rather than in
    /// `Channel.swift`. Recomputed every tick (every 5s) for as long as
    /// this condition holds, so the elapsed time keeps growing instead of
    /// freezing at whatever it read the first time; the moment any real
    /// signal arrives (a state change or a frame, matched or not),
    /// `hasHeardFromChannel` flips and this stops touching `reason`
    /// entirely, leaving it to whatever that real signal set.
    func tickHeartbeat(reconnects: Int) {
        let current: Status = locked {
            status.heartbeat = Date()
            status.reconnects = reconnects
            if !hasHeardFromChannel && reconnects > 0 {
                let elapsed = Int(Date().timeIntervalSince(startedAt))
                let attempts = reconnects == 1 ? "1 attempt" : "\(reconnects) attempts"
                status.reason = "peer unreachable for \(elapsed)s (\(attempts))"
            }
            return status
        }
        try? current.write(to: url)
    }
}

/// Handles one decoded frame. Pulled out of `runAgent()`'s inline closure
/// so the two contracts this task exists for are directly assertable: the
/// echo suppression must be armed strictly before the incoming clip is
/// written to the pasteboard, and a received hello -- matched or not --
/// must always produce a reply. Neither was testable when this lived as a
/// closure inside a function that blocks forever and wrote straight to
/// `NSPasteboard.general`; a later refactor could have swapped the
/// arm/write order, or dropped the reply on a mismatch, and all existing
/// tests would still have passed.
///
/// `send`/`noteWrittenLocally` are the two bound methods (`channel.send`,
/// `watcher.noteWrittenLocally`) rather than the concrete `Channel`/
/// `PasteboardWatcher` types themselves: both types are `final` classes
/// with no test-observable hook into these specific calls (`Channel.send`
/// only does anything once a real ssh pipe exists; nothing on either type
/// records that it was invoked), so a test cannot substitute a spy for
/// them directly. Passing the one function each call site actually needs
/// makes both substitutable with a plain closure in a test, with no new
/// protocol required for either. `pasteboard: PasteboardWriting` is that
/// one new protocol, for the write side, per Pasteboard.swift.
///
/// See `Tests/clipwireTests/HandleFrameTests.swift` for the ordering
/// assertions this exists to make possible.
func handleFrame(
    _ frame: Frame,
    send: (Frame) -> Void,
    noteWrittenLocally: (Data) -> Void,
    pasteboard: PasteboardWriting,
    status: AgentStatus,
    log: Log
) {
    switch frame.type {
    case .hello:
        // Reply unconditionally, matched or not. `Channel` exposes no
        // way to force-close the ssh process from here, so the peer
        // noticing the SAME mismatch on its own side (it validates
        // whatever hello it receives from us, exactly as we validate
        // whatever it sends) and exiting is the only mechanism that
        // actually drops the channel; withholding our reply on a
        // mismatch would just leave it open with nothing to trigger
        // the "channel closed" a user would expect. This also replaces
        // the brief's one-shot `channel.send(hello)` called before
        // `channel.run()` even starts: `Channel.send` looks up
        // `stdinPipe` only when its queued write actually runs, and
        // `stdinPipe` is set only inside `Channel.attempt`, which does
        // not exist yet at that point in the brief's control flow --
        // so that send is either silently dropped or wins a race that
        // depends on GCD scheduling, and either way never fires again
        // on a later reconnect. Sending here, in reaction to every
        // received hello, fires exactly once per connection attempt,
        // on every attempt, because `onFrame` is only ever invoked
        // from inside `attempt()`'s read loop, which always runs after
        // `stdinPipe` has already been assigned.
        send(Frame(type: .hello, payload: ProtocolConstants.helloPayload))
        guard let peer = decodeHello(frame.payload) else {
            let reason = "malformed hello from peer — run `clipwire install`"
            status.recordProtocolMismatch(reason)
            log.line(reason)
            return
        }
        guard peer.version == ProtocolConstants.version else {
            let reason = "protocol mismatch: peer speaks \(peer.version), we speak "
                + "\(ProtocolConstants.version) — run `clipwire install`"
            status.recordProtocolMismatch(reason)
            log.line(reason)
            return
        }
        status.recordHelloMatched()
        log.line("peer said hello (agent \(peer.agent ?? "unknown"))")
    case .clip:
        guard !frame.payload.isEmpty,
              let text = String(data: frame.payload, encoding: .utf8) else { return }
        // Arm suppression BEFORE writing to the pasteboard.
        // PasteboardWatcher's lock keeps its own bookkeeping
        // consistent, but it does not own this write, so only this
        // ordering keeps the watcher from observing our write before
        // the suppression exists and bouncing it straight back to the
        // peer.
        noteWrittenLocally(frame.payload)
        pasteboard.writeText(text)
        status.recordReceived()
    }
}

func runAgent() -> Int32 {
    let log = Log(path: AgentPaths.logPath)
    let config: Config
    do {
        config = try Config.load()
    } catch {
        log.line("\(error)")
        // `line(_:)` only enqueues the write; without this it races the
        // process actually exiting and reliably loses (confirmed
        // empirically -- see Log.flush()'s doc comment), leaving nothing
        // in the log to explain a dead agent.
        log.flush()
        // Exit 0 on a config error: the plist uses KeepAlive={SuccessfulExit: false},
        // so a non-zero exit here would give an eternal restart loop.
        return 0
    }

    let status = AgentStatus(pid: ProcessInfo.processInfo.processIdentifier, url: AgentPaths.statusURL)
    status.writeInitial()

    let channel = Channel(config: config, log: log)
    let systemPasteboard = SystemPasteboard()
    let watcher = PasteboardWatcher(
        pasteboard: systemPasteboard,
        pollInterval: Double(config.macPollIntervalMs) / 1000.0)

    watcher.onChange = { payload in
        channel.send(Frame(type: .clip, payload: payload))
        status.recordSent()
    }

    channel.onFrame = { frame in
        handleFrame(frame, send: channel.send, noteWrittenLocally: watcher.noteWrittenLocally,
                    pasteboard: systemPasteboard, status: status, log: log)
    }

    channel.onStateChange = { state, reason in
        status.applyChannelState(state, reason)
    }

    watcher.start()

    let heartbeat = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    heartbeat.schedule(deadline: .now(), repeating: 5)
    heartbeat.setEventHandler {
        status.tickHeartbeat(reconnects: channel.reconnects)
    }
    heartbeat.resume()

    channel.run()   // never returns
    return 0
}

func printStatus() -> Int32 {
    switch Status.read(from: AgentPaths.statusURL) {
    case .healthy(let status):
        print("up — \(status.reconnects) reconnects")
        if let sent = status.lastSentAt { print("last sent:     \(sent)") }
        if let received = status.lastReceivedAt { print("last received: \(received)") }
        return 0
    case .unhealthy(let status, let reason):
        print("\(status.state.rawValue) — \(reason)")
        return 1
    case .agentDead(let why):
        print("agent dead — \(why)")
        return 1
    }
}

func initConfig() -> Int32 {
    let url = Config.defaultURL
    guard !FileManager.default.fileExists(atPath: url.path) else {
        print("config already exists at \(url.path) — not overwriting")
        return 1
    }
    let example = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("config.example.json")
    do {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(contentsOf: example).write(to: url)
        print("wrote \(url.path) — edit it, then run `clipwire install`")
        return 0
    } catch {
        print("could not write config: \(error)")
        return 1
    }
}

func install() -> Int32 {
    let config: Config
    do {
        config = try Config.load()
    } catch {
        // Prints the real ConfigError (missing vs invalid, and why) rather
        // than swallowing it behind one generic line -- consistent with
        // the rest of this file's "failures are visible" intent.
        print("no usable config: \(error)")
        return 1
    }
    let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("agent/clipwire-agent.py")
    let target = config.remoteAgentPath
    let remote = "\(config.user)@\(config.host)"

    func ssh(_ command: String) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-i", expandTilde(config.identityFile),
                          "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                          "-o", "ConnectTimeout=5", remote, command]
        try? task.run()
        task.waitUntilExit()
        return task.terminationStatus
    }

    guard ssh("mkdir -p $(dirname \(target))") == 0 else {
        print("could not create the remote directory")
        return 1
    }

    let scp = Process()
    scp.executableURL = URL(fileURLWithPath: "/usr/bin/scp")
    scp.arguments = ["-i", expandTilde(config.identityFile),
                     "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     "-o", "ConnectTimeout=5",
                     source.path, "\(remote):\(target)"]
    try? scp.run()
    scp.waitUntilExit()
    guard scp.terminationStatus == 0 else {
        print("copy failed")
        return 1
    }

    guard ssh("chmod +x \(target)") == 0 else { return 1 }
    let selftest = ssh("\(target) --selftest")
    print(selftest == 0 ? "installed and verified" : "installed, but --selftest failed")
    return selftest
}

switch Array(CommandLine.arguments.dropFirst()).first {
case "run", nil: exit(runAgent())
case "status":   exit(printStatus())
case "init":     exit(initConfig())
case "install":  exit(install())
default:
    print("usage: clipwire [run|status|init|install]")
    exit(2)
}
