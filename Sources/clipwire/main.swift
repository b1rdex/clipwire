// Sources/clipwire/main.swift
import CryptoKit
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
    static let version = 2
    static let agentVersion = "0.1.0"

    static var helloPayload: Data {
        (try? JSONEncoder().encode(
            HelloPayload(version: version, agent: agentVersion,
                         sentAt: Date().timeIntervalSince1970)
        )) ?? Data()
    }
}

/// Wire shape of a hello frame's JSON payload:
/// `{"protocol": <int>, "agent": "<version>", "sent_at": <epoch seconds>}`.
/// `version` maps to the wire key `protocol` (a Swift keyword) via `CodingKeys`,
/// the same pattern `Config` already uses for its own snake_case wire keys.
private struct HelloPayload: Codable {
    let version: Int
    let agent: String?
    // Optional, like `agent`: we always populate it in what WE build (see
    // `helloPayload` above), but decoding stays tolerant of a peer that
    // omits it -- an old v1 peer, or any hand-built test payload -- so
    // `decodeHello` keeps reporting the real version mismatch instead of
    // degrading to "malformed hello" the moment a v1 peer's payload lacks a
    // key v2 introduced. `skewLogLine` reads it back out and treats a
    // missing value as "not measurable", never as an error.
    let sentAt: Double?

    enum CodingKeys: String, CodingKey {
        case version = "protocol"
        case agent
        case sentAt = "sent_at"
    }
}

/// Parses a peer's hello payload. Returns `nil` if the JSON cannot be
/// decoded at all -- `runAgent()`'s `.hello` case treats that the same as
/// a version mismatch, since an undecodable declaration is not something
/// this side can confirm as compatible.
func decodeHello(_ payload: Data) -> (version: Int, agent: String?, sentAt: Double?)? {
    guard let decoded = try? JSONDecoder().decode(HelloPayload.self, from: payload) else {
        return nil
    }
    return (decoded.version, decoded.agent, decoded.sentAt)
}

enum SkewConstants {
    // Above this, the two clocks disagree by enough that a freshness
    // comparison between them can pick the wrong side. Compared with `>`:
    // five seconds exactly is the boundary, not a warning. Written into the
    // message below as a literal rather than interpolated, so the Python
    // twin of that line does not have to reproduce a second float-formatting
    // bridge byte for byte; the tests pin the literal against this constant.
    static let warnSeconds: Double = 5
}

/// The peer's clock offset from ours, as a log line -- or `nil` when it
/// cannot be measured.
///
/// Measures `abs(now - sentAt)` from the HELLO, never the age of a clip: a
/// clip legitimately copied this morning is hours old, so warning on that
/// would fire on nearly every handshake and teach everyone to ignore the log.
///
/// A missing or non-finite `sentAt` means the same thing -- skew is not
/// measurable -- and returns `nil`. An unmeasurable peer clock is not a
/// protocol violation, so this neither warns nor reports a mismatch.
///
/// Mirrors `agent/clipwire-agent.py`'s `skew_log_line` one branch at a time,
/// including the exact text of both outcomes, the way the two "over the frame
/// cap" lines already match. `String(format:)` with no explicit locale is
/// non-localized, so `%.1f` writes the same "." separator Python's `%` does,
/// on any machine.
///
/// The `isFinite` guard is the mirror of the Python side's, where it is
/// load-bearing: `json.loads` accepts the bare literals `NaN`/`Infinity`, and
/// `abs(now - nan) > 5.0` is `False`, so an unguarded implementation there
/// logs `peer clock skew nan` and silently never warns. Foundation's
/// `JSONDecoder` rejects those tokens outright, so on this side such a payload
/// never gets past `decodeHello` -- the guard costs one clause and keeps the
/// two functions readable as one formula.
func skewLogLine(peerSentAt: Double?, now: Double) -> String? {
    guard let sentAt = peerSentAt, sentAt.isFinite else { return nil }
    let skew = abs(now - sentAt)
    if skew > SkewConstants.warnSeconds {
        return String(format: "peer clock skew %.1fs — over 5s, check the clock on both machines",
                      skew)
    }
    return String(format: "peer clock skew %.1fs", skew)
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

/// Lowercase hex, no separators -- the exact shape `hashlib.sha256(data).hexdigest()`
/// produces on the Python side, and the shape every `ClipState.sha256` on the wire
/// must match byte-for-byte: `resolveStartupState`/`resolveFreshness` compare these
/// strings with plain `==`, so a case or separator difference here would make every
/// startup comparison see "hashes differ" and take the `now` branch meant only for
/// content that genuinely changed while nothing was watching -- the systematic
/// clobber the persistent store exists to prevent. `EchoGuard` already computes a
/// `SHA256.Digest` elsewhere in this target and never hexes it, so there is no
/// existing conversion to reuse here; this is the first, and is pinned against a
/// known vector in `Tests/clipwireTests/HandleFrameTests.swift`.
func sha256Hex(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}

/// Reconciles what the pasteboard holds right now against what was last persisted.
/// `nil` (an empty or unreadable pasteboard) is never hashed, matching the wire
/// contract that `sha256` is `null` for exactly that case -- see `resolveStartupState`
/// for the rule this applies once a current hash is in hand.
func resolveCurrentClipState(pasteboard: PasteboardReading, stored: ClipState?, now: Double) -> ClipState {
    let currentHash: String?
    if let data = pasteboard.readText(), !data.isEmpty {
        currentHash = sha256Hex(data)
    } else {
        currentHash = nil
    }
    return resolveStartupState(currentHash: currentHash, stored: stored, now: now)
}

/// Whether THIS connection's one-shot clip-state announcement has already gone out.
/// `handleFrame` checks and claims it via `markSent()`, on a matched hello only;
/// `wireAgent` calls `reset()` on `.clipboardPending`, the signal `Channel` raises
/// exactly once per established connection attempt (see `Channel.attempt()`'s
/// `established` guard) -- so the announcement re-arms on every reconnect, not only
/// the process's first connection ever. That re-arming is the entire point: Mac
/// sleep/wake cycles reconnect constantly, and each one needs its own reconciliation,
/// which is exactly what v1 never did.
///
/// Holds no lock, unlike `AgentStatus`: this is only ever touched from
/// `channel.onFrame`/`channel.onStateChange`, both invoked synchronously from
/// whichever single thread drives `Channel.run()`'s decode loop -- never from
/// `PasteboardWatcher`'s timer or the heartbeat timer, which is what forces a lock
/// onto `AgentStatus`.
final class ClipStateAnnouncement {
    private(set) var sent = false

    func reset() {
        sent = false
    }

    /// Marks it sent and returns whether THIS call is the one that did so --
    /// `false` if an earlier call already claimed it this connection.
    @discardableResult
    func markSent() -> Bool {
        guard !sent else { return false }
        sent = true
        return true
    }
}

/// Builds and sends this side's one-shot clip-state announcement: reconciles
/// whatever the pasteboard currently holds against the persistent store (so
/// unchanged content keeps its true recorded age instead of looking freshly
/// copied -- see `resolveStartupState`), persists the reconciled value, and sends
/// it. The send is unconditional on the save's success -- a local disk failure is
/// not the peer's fault, and must not silently disable reconciliation for this
/// connection the way gating the send behind the save's result would.
///
/// Pulled out of `wireAgent` for the same reason `handleFrame` was pulled out of
/// `runAgent()`: a `send` spy can verify the exact frame this produces without a
/// live channel. See `Tests/clipwireTests/HandleFrameTests.swift`.
func announceClipState(
    send: (Frame) -> Void,
    pasteboard: PasteboardReading,
    clipStateStore: ClipStateStore,
    now: Double
) {
    let resolved = resolveCurrentClipState(pasteboard: pasteboard, stored: clipStateStore.load(), now: now)
    try? clipStateStore.save(resolved)
    guard let payload = try? resolved.encodePayload() else { return }
    send(Frame(type: .clipState, payload: payload))
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
    pasteboard: PasteboardReading & PasteboardWriting,
    status: AgentStatus,
    log: Log,
    clipStateStore: ClipStateStore,
    clipStateAnnouncement: ClipStateAnnouncement,
    now: Double = Date().timeIntervalSince1970
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
        // Matched peers only: a clock reading from one we cannot talk to is
        // noise next to the mismatch itself.
        if let skew = skewLogLine(peerSentAt: peer.sentAt, now: now) {
            log.line(skew)
        }
        // Sent exactly once per connection, immediately after a matched
        // hello -- per the design spec. `clipStateAnnouncement` is what
        // makes "once" true, not the assumption that hello itself only
        // ever arrives once: `wireAgent` resets it on the next
        // `.clipboardPending`, so it re-arms on every reconnect.
        if clipStateAnnouncement.markSent() {
            announceClipState(send: send, pasteboard: pasteboard, clipStateStore: clipStateStore, now: now)
        }
    case .clipState:
        guard let peerState = try? ClipState.decodePayload(frame.payload) else { return }
        // `clipStateStore.load()` should already reflect our own current
        // state -- either from this connection's own announcement above,
        // or from an ordinary local-change/applied-clip save since -- so
        // the fallback below only matters if an earlier save failed. It
        // must still resolve a REAL state from the live pasteboard rather
        // than a bare nil-hash placeholder: a wrong nil here would make
        // both sides resolve waitForPeer against each other's (correctly
        // announced) state and silently lose the clip, reintroducing v1's
        // bug through the fallback path instead of the main one.
        let mine = clipStateStore.load()
            ?? resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: now)
        switch resolveFreshness(mine: mine, peer: peerState) {
        case .sendMine:
            guard let data = pasteboard.readText(), !data.isEmpty else { return }
            // The size bound matches PasteboardWatcher's own send-side guard
            // (Pasteboard.swift): this branch reads the live pasteboard
            // independently, and without it, winning a reconciliation over
            // content at or beyond the cap would build a `ClipPayload` whose
            // encoded frame exceeds `FrameConstants.maxPayloadBytes` -- the
            // peer's `Frame.decode` rejects that as oversized and drops the
            // whole channel. Logged (unlike a merely-empty pasteboard, which
            // is not a skip at all) so a user whose large paste never syncs
            // has something to look at, matching the Python agent's
            // existing "skipping a clip of N bytes" line for the same cap.
            guard data.count + ClipPayloadConstants.timestampBytes <= FrameConstants.maxPayloadBytes else {
                log.line("skipping a clip of \(data.count) bytes: over the frame cap")
                return
            }
            let text = String(decoding: data, as: UTF8.self)
            // `mine.ts`, not `now`: the content has not changed, only been
            // re-announced, so its recorded age must be preserved. Sending
            // with `now` would perpetually refresh it and let it win every
            // future reconciliation regardless of what happens next.
            send(Frame(type: .clip, payload: ClipPayload(ts: mine.ts, text: text).encode()))
        case .waitForPeer, .doNothing:
            // Hashes equal means we agree -- not a signal to resend. A
            // peer that is fresher means we wait. Conflating either with
            // sendMine reintroduces a clobber or a ping-pong.
            break
        }
    case .clip:
        guard let decoded = try? ClipPayload.decode(frame.payload), !decoded.text.isEmpty else { return }
        let textData = Data(decoded.text.utf8)
        // Arm suppression BEFORE writing to the pasteboard, with the
        // PLAIN TEXT bytes -- not `frame.payload`, which carries the
        // 8-byte timestamp prefix. PasteboardWatcher's own poll() hashes
        // whatever `pasteboard.readText()` returns, which is this text
        // alone; arming with the ts-prefixed payload would make
        // EchoGuard's digest never match, `shouldSend` would always
        // return true, and every applied remote clip would bounce
        // straight back out to the peer it came from. PasteboardWatcher's
        // lock keeps its own bookkeeping consistent, but it does not own
        // this write, so only this ordering keeps the watcher from
        // observing our write before the suppression exists.
        noteWrittenLocally(textData)
        pasteboard.writeText(decoded.text)
        // The peer's timestamp, never `now`: this is the entire reason it
        // travels in the frame. Stamping it with `now` would make applied
        // content look freshly copied here and win the next
        // reconciliation against the machine it actually came from.
        try? clipStateStore.save(ClipState(sha256: sha256Hex(textData), ts: decoded.ts))
        status.recordReceived()
    }
}

/// Wires the channel, the pasteboard watcher, and status reporting together.
/// Pulled out of `runAgent()` so the last unpinned link in the echo chain --
/// that the closures actually bind `watcher.noteWrittenLocally` into the
/// frame handler, not something forgotten or a no-op -- is directly
/// exercisable in a test, with no live ssh or real pasteboard needed. See
/// `Tests/clipwireTests/AgentWiringTests.swift`.
///
/// `watcher.onChange` only calls `status.recordSent()` once `channel.send`
/// reports the frame actually reached the pipe (`onSent(true)`) -- a send
/// attempted before any channel is established (`stdinPipe` still nil) must
/// not make `clipwire status` claim a clip that never left the machine.
func wireAgent(
    channel: Channel, watcher: PasteboardWatcher, pasteboard: PasteboardReading & PasteboardWriting,
    status: AgentStatus, log: Log, clipStateStore: ClipStateStore, clipStateAnnouncement: ClipStateAnnouncement
) {
    watcher.onChange = { payload, observedAt in
        // A genuine local change: `observedAt` -- the moment PasteboardWatcher
        // actually read it, not whenever this closure happens to run -- is
        // the timestamp both for what we persist and for what we send. The
        // save must happen regardless of whether the send below ever reaches
        // the peer (no channel yet, or the write fails): the store's job is
        // "what do we hold and how old is it", independent of delivery.
        try? clipStateStore.save(ClipState(sha256: sha256Hex(payload), ts: observedAt))
        let text = String(decoding: payload, as: UTF8.self)
        let framePayload = ClipPayload(ts: observedAt, text: text).encode()
        channel.send(Frame(type: .clip, payload: framePayload), onSent: { sent in
            if sent { status.recordSent() }
        })
    }

    channel.onFrame = { frame in
        handleFrame(frame, send: channel.send, noteWrittenLocally: watcher.noteWrittenLocally,
                    pasteboard: pasteboard, status: status, log: log,
                    clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)
    }

    channel.onStateChange = { state, reason in
        status.applyChannelState(state, reason)
        // Channel raises `.clipboardPending` exactly once per established
        // connection attempt (see `Channel.attempt()`'s `established`
        // guard) -- the reset point that lets the one-shot announcement
        // re-arm on every reconnect rather than firing only on the
        // process's first connection ever.
        if state == .clipboardPending {
            clipStateAnnouncement.reset()
        }
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
        pollInterval: Double(config.macPollIntervalMs) / 1000.0,
        log: log)
    let clipStateStore = ClipStateStore(path: ClipStateStoreConstants.defaultPath)
    let clipStateAnnouncement = ClipStateAnnouncement()

    wireAgent(channel: channel, watcher: watcher, pasteboard: systemPasteboard, status: status, log: log,
              clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)

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

    guard ssh("chmod +x \(target)") == 0 else {
        print("could not set the executable bit on \(target)")
        return 1
    }
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
