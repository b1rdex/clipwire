// Tests/clipwireTests/PasteboardTests.swift
import XCTest
@testable import clipwire

final class FakePasteboard: PasteboardReading {
    var changeCount = 0
    var text: Data?

    func set(_ value: String) {
        text = Data(value.utf8)
        changeCount += 1
    }

    func setNonText() {
        text = nil
        changeCount += 1
    }

    func readText() -> Data? { text }
}

final class PasteboardTests: XCTestCase {
    func testEmitsOnChange() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }

        watcher.poll()            // establishes the baseline, emits nothing
        pasteboard.set("hello")
        watcher.poll()
        XCTAssertEqual(seen, [Data("hello".utf8)])
    }

    func testUnchangedCountEmitsNothing() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        pasteboard.set("hello")
        watcher.poll()
        watcher.poll()
        watcher.poll()
        XCTAssertEqual(seen.count, 1, "changeCount unchanged means no work")
    }

    func testNonTextIsSkippedButChangeCountIsConsumed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.setNonText()   // e.g. an image
        watcher.poll()
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
        pasteboard.set("after the image")
        watcher.poll()
        XCTAssertEqual(seen, [Data("after the image".utf8)],
                       "the image must not have wedged the watcher")
    }

    func testEmptyClipIsNotEmitted() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.set("")
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    func testOurOwnWriteIsNotEchoed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()

        watcher.noteWrittenLocally(Data("from the peer".utf8))
        pasteboard.set("from the peer")     // the write we just made
        watcher.poll()
        XCTAssertTrue(seen.isEmpty, "our own write must not bounce back")

        pasteboard.set("typed by the user")
        watcher.poll()
        XCTAssertEqual(seen, [Data("typed by the user".utf8)])
    }

    func testOversizedClipIsSkipped() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxPayloadBytes + 1))
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }
}

// MARK: - Race coverage (added beyond the brief; see task-13-report.md)
//
// Task 11 found that on the PC side, the watcher thread and the main thread
// shared echo-suppression state with no lock, and the watcher read the
// clipboard *before* consuming the suppression. Two back-to-back incoming
// clips then made it compare a stale value and send our own clip back to the
// peer; fixed with a generation counter that let the watcher detect and
// discard a comparison made stale by a write racing its slow (up to 3s,
// forked wl-paste) read.
//
// PasteboardWatcher has the same two actors: the timer's poll() and (in a
// later task) the channel's frame handler, which writes an incoming clip and
// calls noteWrittenLocally() from a different thread. Unlike wl-paste,
// SystemPasteboard.readText() is an in-process NSPasteboard call with no
// subprocess fork, so there is no slow operation whose lock-holding cost
// needs dodging with a generation counter — a plain lock that covers
// poll()'s [read text -> consult echo] step and noteWrittenLocally()'s arm
// is both necessary and sufficient, and is cheap because that whole section
// is fast. This test proves the "necessary" half empirically: it fails
// against an unprotected implementation and passes once the two are made
// mutually exclusive.

/// A double that can pause mid-read, to let a test land a concurrent
/// noteWrittenLocally() call while poll() is between reading the pasteboard
/// and consulting the echo guard.
final class BlockingPasteboard: PasteboardReading {
    var changeCount = 0
    var text: Data?
    var blockNextRead = false
    let readingStarted = DispatchSemaphore(value: 0)
    let proceedWithRead = DispatchSemaphore(value: 0)

    func set(_ value: String) {
        text = Data(value.utf8)
        changeCount += 1
    }

    func readText() -> Data? {
        let snapshot = text
        if blockNextRead {
            blockNextRead = false
            readingStarted.signal()
            _ = proceedWithRead.wait(timeout: .now() + 2)
        }
        return snapshot
    }
}

final class PasteboardConcurrencyTests: XCTestCase {
    /// Simulates two back-to-back incoming clips, A then B, each "written
    /// locally and armed" the way the future channel's frame handler will.
    /// A's read is paused mid-flight so B's arming lands on another thread
    /// while poll() still holds A's text but has not yet consulted the echo
    /// guard. Without mutual exclusion, B's arming would complete during
    /// that window, and poll() would then compare stale text "A" against
    /// echo's new expectation "B" — a mismatch — and echo A back to the peer.
    func testOverlappingIncomingClipDoesNotCauseStaleEcho() {
        // Neither type is Sendable, and rightly so — poll() vs. poll() and
        // start()/stop() vs. themselves are not part of the class's
        // synchronization contract. Access below is safe because every
        // cross-thread touch is ordered by an explicit semaphore handshake,
        // which the compiler cannot see; `nonisolated(unsafe)` says so
        // instead of asserting a broader Sendable conformance that would
        // outrun what this task's fix actually covers.
        nonisolated(unsafe) let pasteboard = BlockingPasteboard()
        nonisolated(unsafe) let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        let seenLock = NSLock()
        watcher.onChange = { data in
            seenLock.lock(); seen.append(data); seenLock.unlock()
        }

        watcher.poll() // baseline: changeCount 0 == lastChangeCount 0, no-op

        // Clip "A" arrives from the peer: written locally, suppression armed —
        // exactly what the future channel's frame handler will do.
        watcher.noteWrittenLocally(Data("A".utf8))
        pasteboard.set("A")

        // Make the read that follows pause mid-flight.
        pasteboard.blockNextRead = true
        let pollFinished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            watcher.poll()
            pollFinished.signal()
        }

        // Wait until poll() is inside readText(), holding "A", about to return.
        XCTAssertEqual(pasteboard.readingStarted.wait(timeout: .now() + 2), .success,
                       "poll() never reached readText()")

        // While that read is outstanding, clip "B" arrives on another thread:
        // written locally, suppression armed for B — the second of two
        // back-to-back incoming clips, as in the Task 11 race.
        let aboutToArmB = DispatchSemaphore(value: 0)
        let armedB = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            pasteboard.set("B")
            aboutToArmB.signal()
            watcher.noteWrittenLocally(Data("B".utf8))
            armedB.signal()
        }
        XCTAssertEqual(aboutToArmB.wait(timeout: .now() + 2), .success,
                       "the concurrent write never reached noteWrittenLocally")

        // noteWrittenLocally(B) must not be able to complete while poll()'s
        // read-then-compare for A is still in flight: if it could, echo would
        // hold B's hash by the time poll() compares A against it, mismatch,
        // and send A — our own already-suppressed clip — back out as new.
        XCTAssertEqual(armedB.wait(timeout: .now() + 0.2), .timedOut,
                       "noteWrittenLocally(B) completed while poll()'s read of A " +
                       "was still outstanding — the two are not mutually exclusive")

        pasteboard.proceedWithRead.signal()

        XCTAssertEqual(pollFinished.wait(timeout: .now() + 2), .success, "poll() never returned")
        XCTAssertEqual(armedB.wait(timeout: .now() + 2), .success,
                       "noteWrittenLocally(B) never completed after poll() released its lock")

        seenLock.lock()
        XCTAssertTrue(seen.isEmpty,
                       "A must have been judged against A's own suppression, not B's")
        seenLock.unlock()

        // B's own suppression, armed after A's read completed, must still hold.
        watcher.poll()
        seenLock.lock()
        XCTAssertTrue(seen.isEmpty, "B's suppression must not have been corrupted either")
        seenLock.unlock()
    }
}
