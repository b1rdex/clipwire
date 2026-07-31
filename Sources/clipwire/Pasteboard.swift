// Sources/clipwire/Pasteboard.swift
import AppKit
import Foundation

protocol PasteboardReading {
    var changeCount: Int { get }
    func readText() -> Data?
}

/// The one write operation an incoming clip needs. Kept separate from
/// `PasteboardReading` (rather than folded into one protocol) because the
/// two sides are consumed by different owners: `PasteboardWatcher` only
/// ever reads, and the frame handler that applies an incoming clip
/// (`handleFrame` in main.swift) only ever writes. Splitting them means a
/// test can substitute a recording spy for the write side alone, without
/// needing to fake `changeCount`/`readText` too — see `HandleFrameTests.swift`.
protocol PasteboardWriting {
    func writeText(_ text: String)
}

final class SystemPasteboard: PasteboardReading, PasteboardWriting {
    private let pasteboard = NSPasteboard.general
    var changeCount: Int { pasteboard.changeCount }

    func readText() -> Data? {
        guard let string = pasteboard.string(forType: .string) else { return nil }
        return Data(string.utf8)
    }

    func writeText(_ text: String) {
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}

/// Polls NSPasteboard.changeCount. The comparison is an integer read in-process,
/// so a sub-second interval costs nothing — unlike the PC side, which has to
/// fork a process and read the whole clipboard.
///
/// Two actors touch this watcher's state: its own `poll()`, invoked on the
/// timer's background queue, and `noteWrittenLocally(_:)`, called by the
/// channel's frame handler (a later task) from a different thread when it
/// writes an incoming clip to the pasteboard. Task 11 found the PC analogue
/// of this — the watcher thread and the main thread shared echo-suppression
/// state with no lock, and the watcher read the clipboard *before* consuming
/// the suppression, so two back-to-back incoming clips made it compare a
/// stale value and echo one of them back to the peer.
///
/// `stateLock` guards `echo` *and* `lastChangeCount` together, and covers the
/// entire [read changeCount -> compare -> read text -> consult echo] sequence
/// in `poll()` as one critical section, matched by `noteWrittenLocally`
/// taking the same lock for its arm. This is stricter than it looks like it
/// needs to be, and deliberately so: an earlier version of this fix moved
/// only `echo` under the lock and left `changeCount` read (and
/// `lastChangeCount` written) beforehand, unlocked. That leaves a gap where a
/// second incoming clip can arrive — armed *and* written — between this
/// poll's changeCount read and its lock acquisition. The read-and-compare
/// that follows still lands on consistent data (it reads whatever is
/// actually current and correctly matches it against `echo`), but
/// `lastChangeCount` gets recorded one generation behind what was actually
/// consumed. The next poll then sees a changeCount it hasn't recorded,
/// treats already-suppressed content as a fresh unobserved change, finds
/// `echo` already spent, and resends it. Reading `changeCount` under the
/// same lock as the rest closes this: whatever generation `poll()` records
/// is provably the one it just read text and consulted `echo` for, because
/// nothing else can touch `echo` in between. Unlike the PC side, none of
/// this needs a generation counter: `SystemPasteboard.readText()` is an
/// in-process NSPasteboard call, not a forked `wl-paste` that can take up to
/// 3 seconds, so there is no slow operation whose lock-holding cost a
/// counter would be needed to dodge — and a counter keyed off `echo`'s own
/// arm count would not even catch this specific gap, since the arm that
/// matters here can complete before `poll()` starts, leaving the counter
/// unchanged across the whole call. A single lock around the full sequence
/// is both sufficient and cheap.
///
/// This lock is still not a complete contract on its own: it says nothing
/// about the ORDER in which the future frame handler writes to the
/// pasteboard versus calling `noteWrittenLocally`, because `PasteboardWatcher`
/// does not perform that write. For the suppression to be armed before its
/// own change becomes observable, that caller must call `noteWrittenLocally`
/// *before* writing to the pasteboard, not after — arm-then-write, which is
/// also already the convention the Python agent's `_write_clip` follows on
/// the PC side. Ordering alone (without this lock) would still race, since
/// `noteWrittenLocally` itself is not atomic with `poll()`'s read; this lock
/// alone (with the wrong order) would still let a poll observe a
/// written-but-unarmed change and echo it. Both are required together; the
/// second is this class's responsibility, the first belongs to the task
/// that builds the frame handler.
final class PasteboardWatcher {
    var onChange: ((Data, Double) -> Void)?

    private let pasteboard: PasteboardReading
    private let pollInterval: TimeInterval
    private var lastChangeCount: Int
    private var echo = EchoGuard()
    private var timer: DispatchSourceTimer?
    // Optional, and defaulted to nil, so every existing call site (this
    // class predates any need to log) keeps compiling unchanged; only
    // `runAgent()` passes a real one. `Log` is `Sendable` and `line(_:)`
    // only enqueues onto its own serial queue, so calling it from inside
    // `pollLocked()`'s critical section below is safe and does not hold
    // `stateLock` for any meaningful extra time.
    private let log: Log?

    // Guards `echo` and `lastChangeCount` together — see the class doc
    // comment for why both, not just `echo`, need to be under this lock.
    private let stateLock = NSLock()

    init(pasteboard: PasteboardReading, pollInterval: TimeInterval, log: Log? = nil) {
        self.pasteboard = pasteboard
        self.pollInterval = pollInterval
        self.lastChangeCount = pasteboard.changeCount
        self.log = log
    }

    func noteWrittenLocally(_ payload: Data) {
        stateLock.lock()
        echo.noteWrittenLocally(payload)
        stateLock.unlock()
    }

    func poll() {
        guard let (toSend, observedAt) = pollLocked() else { return }
        // Invoked after the lock is released, both because it can be slow
        // (it hands off to the channel) and because a callback that
        // re-entered the watcher while the lock was still held would
        // deadlock against a non-reentrant NSLock.
        onChange?(toSend, observedAt)
    }

    /// The entire read-and-decide sequence, as one critical section shared
    /// with `noteWrittenLocally`. `defer` releases the lock on every path,
    /// including the early "nothing changed" return, so a raised guard can
    /// never leak a held lock into the next `noteWrittenLocally` call.
    ///
    /// The timestamp is read here, under the same lock as the text it is
    /// paired with, because it must be the moment of OBSERVATION -- this
    /// poll's read -- not the moment `onChange` eventually runs, which is
    /// deliberately invoked outside the lock and can lag behind it. This
    /// timestamp becomes the outgoing clip's `ts`; a receiving peer stores
    /// it unchanged (see `handleFrame`'s `.clip` case), so inflating it here
    /// would misstate how old the content actually is everywhere downstream.
    private func pollLocked() -> (Data, Double)? {
        stateLock.lock()
        defer { stateLock.unlock() }

        let current = pasteboard.changeCount
        guard current != lastChangeCount else { return nil }
        // Record the new count before any early return, so non-text content
        // cannot wedge the watcher into rescanning the same item forever —
        // and, per the class doc comment, record the generation this same
        // locked call is about to read and consult echo for, not one
        // observed earlier and now stale.
        lastChangeCount = current

        guard let text = pasteboard.readText(), !text.isEmpty else { return nil }
        // `wireAgent` wraps this text in `ClipPayload(ts:text:)` before it
        // ever reaches the wire, adding an 8-byte prefix -- so the bound
        // here must leave room for it. Checking `text.count` alone (exact
        // before Task 9, when this text WAS the frame payload) would let
        // text at exactly the cap encode to a frame 8 bytes over it, which
        // the peer's `Frame.decode` rejects as oversized, dropping the
        // whole channel over a single large-but-not-overlong clip.
        guard text.count + ClipPayloadConstants.timestampBytes <= FrameConstants.maxPayloadBytes else {
            // Logged so a user whose large local copy never reaches the
            // peer has something to look at, matching the Python agent's
            // existing "skipping a clip of N bytes" line for the same cap.
            log?.line("skipping a clip of \(text.count) bytes: over the frame cap")
            return nil
        }
        guard echo.shouldSend(text) else { return nil }
        return (text, Date().timeIntervalSince1970)
    }

    func start() {
        // Cancelling any previous timer before installing a new one keeps a
        // second start() from leaking the first timer as an orphaned,
        // still-firing source.
        timer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + pollInterval, repeating: pollInterval)
        timer.setEventHandler { [weak self] in self?.poll() }
        timer.resume()
        self.timer = timer
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}
