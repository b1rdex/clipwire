// Sources/clipwire/Pasteboard.swift
import AppKit
import Foundation

protocol PasteboardReading {
    var changeCount: Int { get }
    func readText() -> Data?
}

final class SystemPasteboard: PasteboardReading {
    private let pasteboard = NSPasteboard.general
    var changeCount: Int { pasteboard.changeCount }

    func readText() -> Data? {
        guard let string = pasteboard.string(forType: .string) else { return nil }
        return Data(string.utf8)
    }
}

/// Polls NSPasteboard.changeCount. The comparison is an integer read in-process,
/// so a sub-second interval costs nothing — unlike the PC side, which has to
/// fork a process and read the whole clipboard.
///
/// Two actors touch `echo`: this class's own `poll()`, invoked on the
/// timer's background queue, and `noteWrittenLocally(_:)`, called by the
/// channel's frame handler (a later task) from a different thread when it
/// writes an incoming clip to the pasteboard. Task 11 found the PC analogue
/// of this — the watcher thread and the main thread shared echo-suppression
/// state with no lock, and the watcher read the clipboard *before* consuming
/// the suppression, so two back-to-back incoming clips made it compare a
/// stale value and echo one of them back to the peer. The same shape exists
/// here: without synchronization, a second incoming clip's arming could land
/// between this poll's read of the pasteboard and its comparison against
/// `echo`, corrupting that comparison. `stateLock` closes it by making
/// [read text -> consult echo] one atomic step with respect to
/// `noteWrittenLocally`'s mutation. Unlike the PC side, this does not need a
/// generation counter: `SystemPasteboard.readText()` is an in-process
/// NSPasteboard call, not a forked `wl-paste` that can take up to 3 seconds,
/// so there is no slow operation whose lock-holding cost the counter was
/// built to dodge. A plain lock is both sufficient and cheap here.
///
/// This lock alone is not a complete contract: it says nothing about the
/// ORDER in which the future frame handler writes to the pasteboard versus
/// calling `noteWrittenLocally`, because `PasteboardWatcher` does not
/// perform that write. For the suppression to be armed before its own
/// change becomes observable, that caller must call `noteWrittenLocally`
/// *before* writing to the pasteboard, not after — arm-then-write. Ordering
/// alone (without this lock) would still race, since `noteWrittenLocally`
/// itself is not atomic with `poll()`'s read; this lock alone (with the
/// wrong order) would still let a poll observe a written-but-unarmed change
/// and echo it. Both are required together; the second is this class's
/// responsibility, the first belongs to the task that builds the frame
/// handler.
final class PasteboardWatcher {
    var onChange: ((Data) -> Void)?

    private let pasteboard: PasteboardReading
    private let pollInterval: TimeInterval
    private var lastChangeCount: Int
    private var echo = EchoGuard()
    private var timer: DispatchSourceTimer?

    // Guards `echo` only. `lastChangeCount` is touched solely by poll(), and
    // GCD never invokes a dispatch source's handler reentrantly with itself,
    // so poll() cannot race against another poll() and lastChangeCount needs
    // no lock of its own.
    private let stateLock = NSLock()

    init(pasteboard: PasteboardReading, pollInterval: TimeInterval) {
        self.pasteboard = pasteboard
        self.pollInterval = pollInterval
        self.lastChangeCount = pasteboard.changeCount
    }

    func noteWrittenLocally(_ payload: Data) {
        stateLock.lock()
        echo.noteWrittenLocally(payload)
        stateLock.unlock()
    }

    func poll() {
        let current = pasteboard.changeCount
        guard current != lastChangeCount else { return }
        // Record the new count before any early return, so non-text content
        // cannot wedge the watcher into rescanning the same item forever.
        lastChangeCount = current

        // Read the pasteboard and consult (and, on a match, consume) the
        // echo guard as a single step under `stateLock`, so noteWrittenLocally
        // cannot arm a *different* payload in the gap between this read and
        // this comparison. onChange is deliberately invoked after the lock is
        // released, both because it can be slow (it hands off to the channel)
        // and because a callback that re-entered the watcher while the lock
        // was still held would deadlock against a non-reentrant NSLock.
        stateLock.lock()
        let text = pasteboard.readText()
        let toSend: Data?
        if let text, !text.isEmpty, text.count <= FrameConstants.maxPayloadBytes, echo.shouldSend(text) {
            toSend = text
        } else {
            toSend = nil
        }
        stateLock.unlock()

        guard let toSend else { return }
        onChange?(toSend)
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
