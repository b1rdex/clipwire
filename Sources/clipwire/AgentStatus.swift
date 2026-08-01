// Sources/clipwire/AgentStatus.swift
import Foundation

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
/// applies the same fix, to the same class of hazard, that the FIRST plan's
/// Task 13 already applied to `PasteboardWatcher` (a `stateLock` around
/// `changeCount`/`echo`) and its Task 14 to `Channel` (a serial `writeQueue`
/// around pipe writes) -- the plan whose reports are under
/// `2026-07-30-clipwire-implementation/`, not this one's. Three plans now
/// number independently, and the collision is live: v3's own Task 13 is the
/// Mac's image path and touched neither lock. `PasteboardWatcher`'s class
/// comment carries the same warning for its own Task 11 collision.
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

    /// `.clipboardPending`, not `.up`. A matched hello proves only that the
    /// peer PROCESS is alive: the PC agent sends its hello the instant sshd
    /// spawns it, which after a reboot is minutes before GNOME login and the
    /// Wayland session that makes its clipboard readable at all. Reporting
    /// `.up` there overwrote -- microseconds later, from that same frame's
    /// handler -- the `.clipboardPending` that `Channel.attempt()` had just
    /// set, so `clipwire status` claimed a healthy channel for the whole
    /// pre-login window while nothing could sync. `recordPeerClipboardReady`
    /// below is what promotes now, and the spec names precisely this as the
    /// reason clip-state is its own frame rather than fields on `hello`: it
    /// "gives `status` its long-missing protocol basis for reporting
    /// `clipboard-pending` durably".
    ///
    /// Observability only. Nothing reads `status.state` to gate a send; the
    /// FUNCTIONAL `.clipboardPending` that re-arms the one-shot announcement
    /// comes from `Channel`'s `onStateChange` in `wireAgent`, not from here,
    /// so this cannot feed back into the protocol.
    func recordHelloMatched() {
        locked {
            hasHeardFromChannel = true
            protocolMismatchReason = nil
            status.state = .clipboardPending
            status.reason = nil
        }
    }

    /// The peer's clip-state announcement arrived, so its clipboard is
    /// readable and this channel can actually sync. The PC agent sends that
    /// frame from inside `clipboard_became_ready` and nowhere else, so the
    /// frame's mere existence is the proof -- a null hash means an EMPTY
    /// clipboard, not an unavailable one, and syncing works fine in that
    /// state.
    ///
    /// A pinned protocol mismatch outranks this, mirroring
    /// `applyChannelState`'s existing rule. Nothing about a version mismatch
    /// stops the peer's own frames from already being in flight, and
    /// promoting on one would erase the specific "run `clipwire install`"
    /// diagnosis -- reporting a healthy channel that is about to close --
    /// which is the whole reason that reason is pinned.
    func recordPeerClipboardReady() {
        locked {
            hasHeardFromChannel = true
            guard protocolMismatchReason == nil else { return }
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
