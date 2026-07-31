// Tests/clipwireTests/PasteboardTests.swift
import XCTest
@testable import clipwire

/// Conforms to both `PasteboardReading` and `PasteboardWriting` -- one fake
/// object playing both roles, the same as `SystemPasteboard` does in
/// production (see AgentWiringTests.swift, where a write through this
/// object must be observable to a `PasteboardWatcher` reading the same
/// instance, exactly as an incoming clip's write is observable to the real
/// watcher polling `NSPasteboard.general`).
final class FakePasteboard: PasteboardReading, PasteboardWriting {
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

    func writeText(_ text: String) { set(text) }
}

final class PasteboardTests: XCTestCase {
    func testEmitsOnChange() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { data, _ in seen.append(data) }

        watcher.poll()            // establishes the baseline, emits nothing
        pasteboard.set("hello")
        watcher.poll()
        XCTAssertEqual(seen, [Data("hello".utf8)])
    }

    /// The timestamp half of `onChange`'s contract: it must be the moment of
    /// OBSERVATION (this poll), not some later moment `onChange` itself runs.
    func testOnChangeCarriesAnObservationTimestamp() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seenTimestamps: [Double] = []
        watcher.onChange = { _, observedAt in seenTimestamps.append(observedAt) }

        watcher.poll()
        let before = Date().timeIntervalSince1970
        pasteboard.set("hello")
        watcher.poll()
        let after = Date().timeIntervalSince1970

        XCTAssertEqual(seenTimestamps.count, 1)
        guard let ts = seenTimestamps.first else { return }
        XCTAssertTrue(ts >= before && ts <= after,
                      "expected \(ts) to fall within [\(before), \(after)]")
    }

    func testUnchangedCountEmitsNothing() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { data, _ in seen.append(data) }
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
        watcher.onChange = { data, _ in seen.append(data) }
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
        watcher.onChange = { data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set("")
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    func testOurOwnWriteIsNotEchoed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { data, _ in seen.append(data) }
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
        watcher.onChange = { data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxPayloadBytes + 1))
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    /// The pre-v2 guard checked the watched TEXT against the frame cap
    /// directly, which was exact back when that text WAS the frame payload.
    /// Since Task 9, `wireAgent` wraps this text in `ClipPayload(ts:text:)`
    /// before it reaches the wire, adding an 8-byte prefix -- so text at
    /// exactly the cap would encode to a frame 8 bytes OVER it, and the
    /// peer's `Frame.decode` would reject it as oversized and drop the whole
    /// channel over a single large-but-not-overlong clip.
    /// `testOversizedClipIsSkipped` above (`max + 1`) cannot see this: it is
    /// oversized under either the old or the new guard, so it sails past
    /// the boundary this test targets. Matches the plan's own global
    /// constraint: "Max payload stays 4 MiB, now including the 8-byte
    /// timestamp prefix."
    func testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxPayloadBytes))
        watcher.poll()
        XCTAssertTrue(seen.isEmpty,
                      "text of exactly the cap would encode to a ClipPayload 8 bytes over it")
    }

    /// The other half of the same boundary: a fix that over-trims (e.g.
    /// subtracting more than the 8-byte prefix actually costs) would
    /// silently shrink the supported clip size below what the wire format
    /// actually allows. Text at `cap - timestampBytes` must still be
    /// emitted, and must encode to a `ClipPayload` of EXACTLY the cap.
    func testTextLeavingExactRoomForTheTimestampPrefixIsStillEmitted() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { data, _ in seen.append(data) }
        watcher.poll()
        let text = String(repeating: "x",
                          count: FrameConstants.maxPayloadBytes - ClipPayloadConstants.timestampBytes)
        pasteboard.set(text)
        watcher.poll()
        XCTAssertEqual(seen, [Data(text.utf8)],
                       "must still be emitted -- the encoded ClipPayload is exactly at the cap, not over it")
        XCTAssertEqual(ClipPayload(ts: 1, text: text).encode().count, FrameConstants.maxPayloadBytes,
                       "sanity check on the boundary itself")
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
        watcher.onChange = { data, _ in
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

// MARK: - Fix round 1: changeCount read outside stateLock (see task-13-report.md)
//
// Review found that the first fix left `changeCount` itself read, and
// `lastChangeCount` written, *before* `stateLock` was acquired — a gap the
// BlockingPasteboard test above cannot see, because it interleaves inside
// readText(), which was already under the lock. This double interleaves in
// the changeCount getter instead, landing a complete second clip (arm and
// write) in that earlier, still-unlocked gap:
//
//   1. poll() reads changeCount (N, clip A's generation) — unlocked.
//   2. Before poll() reaches stateLock.lock(), clip B arrives complete:
//      armed and written; the real changeCount becomes N+1.
//   3. poll() locks, reads text (already B, since B is fully written by
//      now), and shouldSend(B) correctly matches and suppresses it — but
//      lastChangeCount was recorded as N, one generation behind what was
//      actually read and consumed.
//   4. Next tick: changeCount(N+1) != lastChangeCount(N) looks like a fresh,
//      unobserved change. text is still B, but echo is now nil (consumed in
//      step 3) — so shouldSend(B) returns true on an unarmed guard, and
//      onChange(B) fires: our own already-suppressed clip goes out again.

/// A double that pauses inside the `changeCount` getter itself (not
/// `readText()`), to let a test land a complete second clip cycle in the gap
/// between poll() observing a changeCount and poll() acquiring the lock that
/// guards the read-and-compare that follows.
final class ChangeCountGapPasteboard: PasteboardReading {
    private(set) var realCount = 0
    var text = Data()
    var pauseOnNextRead = false
    let pausedInChangeCount = DispatchSemaphore(value: 0)
    let resumeChangeCount = DispatchSemaphore(value: 0)

    func set(_ value: String) {
        text = Data(value.utf8)
        realCount += 1
    }

    var changeCount: Int {
        let snapshot = realCount
        if pauseOnNextRead {
            pauseOnNextRead = false
            pausedInChangeCount.signal()
            _ = resumeChangeCount.wait(timeout: .now() + 2)
        }
        return snapshot
    }

    func readText() -> Data? { text }
}

final class PasteboardGenerationTests: XCTestCase {
    /// Pins the reviewer's exact trace: clip A's own processing poll() is
    /// paused right after reading changeCount (unlocked at the time of the
    /// bug) and before reaching the lock. Clip B arrives completely — armed
    /// and written — in that gap, on another thread. The bug does not show
    /// up on the poll that raced B in (it happens to read B's text and
    /// correctly suppress it) — it shows up on the *next* poll, once
    /// `lastChangeCount` turns out to have recorded the wrong generation.
    func testStaleLastChangeCountDoesNotResendAlreadySuppressedClip() {
        // Safe because every cross-thread touch below is ordered by an
        // explicit semaphore handshake; see the identical note on
        // PasteboardConcurrencyTests above.
        nonisolated(unsafe) let pasteboard = ChangeCountGapPasteboard()
        nonisolated(unsafe) let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        let seenLock = NSLock()
        watcher.onChange = { data, _ in
            seenLock.lock(); seen.append(data); seenLock.unlock()
        }

        // Clip A: armed and written, but not yet polled.
        watcher.noteWrittenLocally(Data("A".utf8))
        pasteboard.set("A")

        // Pause the poll that is about to process A right after it reads
        // changeCount — the gap the reviewer's trace exploits.
        pasteboard.pauseOnNextRead = true
        let pollFinished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            watcher.poll()
            pollFinished.signal()
        }

        XCTAssertEqual(pasteboard.pausedInChangeCount.wait(timeout: .now() + 2), .success,
                       "poll() never reached the changeCount read")

        // While that read is paused, clip B arrives *completely* on another
        // thread: armed, then written. Dispatched (not waited on here) so
        // this cannot deadlock against a fixed implementation, where poll()
        // may already hold stateLock at this point and this call would
        // correctly block until poll() releases it.
        let armed = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            watcher.noteWrittenLocally(Data("B".utf8))
            pasteboard.set("B")
            armed.signal()
        }

        // Give B's arm-and-write a wide, uncontended head start before
        // letting A's paused poll resume. This must be an unconditional
        // sleep, not a wait on `armed`: waiting here would deadlock against
        // a fixed implementation, where poll() already holds stateLock at
        // the pause point, so B legitimately cannot complete until the
        // resume below releases it. Pre-fix, nothing contends for anything
        // at this point, so 100ms is a three-plus-orders-of-magnitude margin
        // over the couple of in-process operations B needs — enough to land
        // "B arrives complete" deterministically before the resume, which is
        // the reviewer's exact precondition. Without this, the interleaving
        // is unconstrained and can instead land B's arm without its write
        // (or vice versa), which is a different bug (arm/write are not one
        // atomic event — see the class doc comment) and not what this test
        // targets.
        Thread.sleep(forTimeInterval: 0.1)

        pasteboard.resumeChangeCount.signal()

        XCTAssertEqual(pollFinished.wait(timeout: .now() + 2), .success, "poll() never returned")
        XCTAssertEqual(armed.wait(timeout: .now() + 2), .success,
                       "the concurrent arm+write of B never completed")

        // First tick: whichever of A or B this poll actually read, it must
        // have matched what was armed at read time and been suppressed.
        seenLock.lock()
        XCTAssertTrue(seen.isEmpty, "the first poll must have suppressed what it read")
        seenLock.unlock()

        // Second tick: nothing new has been written since. If the first
        // poll recorded the generation it actually consumed, this is a
        // no-op. If it recorded a stale (earlier) generation instead, this
        // looks like a fresh change and re-sends already-suppressed content.
        watcher.poll()
        seenLock.lock()
        XCTAssertTrue(seen.isEmpty,
                       "a clip already suppressed on the previous poll must not be " +
                       "resent just because lastChangeCount lagged the generation " +
                       "that poll actually read and consumed")
        seenLock.unlock()
    }
}
