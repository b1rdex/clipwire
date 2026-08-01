// Tests/clipwireTests/PasteboardTests.swift
import XCTest
@testable import clipwire

/// Conforms to both `PasteboardReading` and `PasteboardWriting` -- one fake
/// object playing both roles, the same as `SystemPasteboard` does in
/// production (see AgentWiringTests.swift, where a write through this
/// object must be observable to a `PasteboardWatcher` reading the same
/// instance, exactly as an incoming clip's write is observable to the real
/// watcher polling `NSPasteboard.general`).
///
/// Since Task 8 this double holds BOTH kinds and applies the same
/// text-wins rule `chooseKind` applies to a real board, so a test can put
/// an image on it and see what the production code does with one. It is
/// deliberately not a `SystemPasteboard` wrapping a fake backend: these
/// tests are about `PasteboardWatcher` and the frame handler, which only
/// ever see the `PasteboardReading`/`PasteboardWriting` protocols --
/// `SystemPasteboard`'s own translation of NSPasteboard types is pinned
/// separately, in `CanonicalReadTests` below, against a fake backend.
final class FakePasteboard: PasteboardReading, PasteboardWriting {
    var changeCount = 0
    var text: Data?
    var image: Data?

    /// A real pasteboard write replaces everything on the board, so these
    /// setters clear the other kind rather than accumulating both -- a
    /// double that let stale image bytes survive a text copy would make
    /// the text-wins rule look exercised when it never was.
    func set(_ value: String) {
        text = Data(value.utf8)
        image = nil
        changeCount += 1
    }

    func setImage(_ png: Data) {
        image = png
        text = nil
        changeCount += 1
    }

    func setNonText() {
        text = nil
        image = nil
        changeCount += 1
    }

    /// Text wins, then image -- `chooseKind`'s rule, applied by the double
    /// so callers of `read()` see the same precedence the real board gives
    /// them. Empty content reads as nothing at all, matching
    /// `SystemPasteboard.read()`, which returns nil rather than an empty
    /// body (and `WaylandClipboard.read()` on the PC, which does the same).
    func read() -> (kind: ClipKind, data: Data)? {
        if let text, !text.isEmpty { return (.text, text) }
        if let image, !image.isEmpty { return (.image, image) }
        return nil
    }

    func write(kind: ClipKind, data: Data) {
        switch kind {
        case .text: text = data; image = nil
        case .image: image = data; text = nil
        }
        changeCount += 1
    }
}

/// A board that reports content of a given kind and hands back ZERO bytes for
/// it -- a state `FakePasteboard` cannot express, because its `read()`
/// deliberately mirrors `SystemPasteboard`'s and reports nothing at all for an
/// empty body. That mirroring is what makes it unable to exercise
/// `pollLocked`'s own emptiness guard: with the production read already
/// filtering empties, the guard would look tested while nothing reached it.
final class EmptyBodyPasteboard: PasteboardReading {
    var changeCount = 0
    var kind: ClipKind = .text

    func bump(_ kind: ClipKind) {
        self.kind = kind
        changeCount += 1
    }

    func read() -> (kind: ClipKind, data: Data)? { (kind, Data()) }
}

final class PasteboardTests: XCTestCase {
    func testEmitsOnChange() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }

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
        watcher.onChange = { _, _, observedAt in seenTimestamps.append(observedAt) }

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
        watcher.onChange = { _, data, _ in seen.append(data) }
        pasteboard.set("hello")
        watcher.poll()
        watcher.poll()
        watcher.poll()
        XCTAssertEqual(seen.count, 1, "changeCount unchanged means no work")
    }

    /// The only test in this suite that puts an UNREADABLE board in front of
    /// the watcher: content `chooseKind` declines outright, so `read()`
    /// reports nothing at all. Deliberately not an image -- since Task 13 an
    /// image is emitted rather than skipped, which is
    /// `testAnImageOnThePasteboardIsEmittedAsAnImage`'s business.
    ///
    /// `setNonText()` is the whole of its teeth. Task 13 briefly deleted that
    /// call while rewriting the comment beside it, which left three polls of
    /// an unchanged board and a first assertion that passed because nothing
    /// had happened -- a duplicate of `testEmitsOnChange` carrying a failure
    /// message about an event that never occurred. Nothing warned, because
    /// the only remaining reference to `setNonText()` was in a doc comment.
    func testNonTextIsSkippedButChangeCountIsConsumed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()
        // A board offering something this agent syncs neither kind of -- an
        // RTF-only or file-URL copy.
        pasteboard.setNonText()
        watcher.poll()
        watcher.poll()
        XCTAssertTrue(seen.isEmpty, "content of neither kind must not be emitted as either")
        pasteboard.set("after the unsyncable item")
        watcher.poll()
        XCTAssertEqual(seen, [Data("after the unsyncable item".utf8)],
                       "and it must not have wedged the watcher into rescanning it forever")
    }

    /// Replaces `testAnImageOnThePasteboardIsNotEmittedAsText`, which pinned
    /// the scope boundary this task removes: until now `pollLocked` guarded
    /// `read.kind == .text`, because `onChange`'s only consumer wrapped
    /// whatever it was handed in a `ClipPayload` -- the TEXT codec -- so an
    /// emitted image would have reached the wire as a mojibake
    /// transliteration of a PNG. `onChange` now carries the kind, and
    /// `outgoingClipFrame` picks the codec from it.
    ///
    /// `setNonText()` cannot stand in for this: it leaves the double with
    /// nothing to read at all, so a watcher that emitted images would still
    /// emit nothing there. This one holds a real image, which since Task 8
    /// reads back as a genuine `(.image, bytes)` pair.
    func testAnImageOnThePasteboardIsEmittedAsAnImage() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [(kind: ClipKind, data: Data)] = []
        watcher.onChange = { kind, data, _ in seen.append((kind, data)) }
        watcher.poll()

        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        pasteboard.setImage(png)
        watcher.poll()
        XCTAssertEqual(seen.map(\.kind), [.image],
                       "an image must be emitted, and emitted AS an image")
        XCTAssertEqual(seen.first?.data, png)

        watcher.poll()
        pasteboard.set("after the image")
        watcher.poll()
        XCTAssertEqual(seen.map(\.kind), [.image, .text])
        XCTAssertEqual(seen.last?.data, Data("after the image".utf8),
                       "and the image must not have wedged the watcher either")
    }

    /// The echo half, for images. `EchoGuard` has compared `(kind, digest)`
    /// since Task 9, but every call site armed and checked `.text` until now,
    /// so the kind was a no-op in practice. An applied image that bounced
    /// back would ping-pong against a peer that applies any incoming clip
    /// unconditionally.
    func testOurOwnImageWriteIsNotEchoed() {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [(kind: ClipKind, data: Data)] = []
        watcher.onChange = { kind, data, _ in seen.append((kind, data)) }
        watcher.poll()

        watcher.noteWrittenLocally(kind: .image, payload: png)
        pasteboard.setImage(png)            // the write we just made
        watcher.poll()
        XCTAssertTrue(seen.isEmpty, "our own image write must not bounce back")

        let copied = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x99])
        pasteboard.setImage(copied)
        watcher.poll()
        XCTAssertEqual(seen.map(\.data), [copied],
                       "a genuine later image copy must still be emitted")
    }

    /// The image twin of `testOversizedClipIsSkipped`/`testOversizedClipIsLoggedWithItsSize`,
    /// with the size in the line for the same reason: a user whose large
    /// screenshot silently never reaches the peer has nothing to look at
    /// otherwise. The verdict clause is byte-identical to the PC agent's own
    /// three image skips and to `handleFrame`'s `.sendMine` branch.
    func testAnOversizedImageIsSkippedAndLoggedWithItsSize() {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pasteboard-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4, log: log)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()

        let oversized = FrameConstants.maxImageBytes + 1
        pasteboard.setImage(Data(repeating: 0x89, count: oversized))
        watcher.poll()
        log.flush()

        XCTAssertTrue(seen.isEmpty)
        let contents = try? String(contentsOfFile: logPath, encoding: .utf8)
        XCTAssertEqual(contents?.contains("skipping an image of \(oversized) bytes: over the image limit"),
                       true,
                       "expected the skip to be logged with its size; got: \(contents ?? "<unreadable>")")
    }

    /// The skip must be logged ONCE per clipboard change, not once per tick.
    /// `pollLocked` records `lastChangeCount` before any early return, so an
    /// unsendable item cannot wedge the watcher into rescanning it forever --
    /// and a regression there is not merely wasteful, it is a log a user
    /// cannot read: the poll interval is 400ms by default, so an oversized
    /// screenshot left on the pasteboard would write over 200,000 identical
    /// lines a day.
    ///
    /// Written against the image guard because this task added it, but the
    /// property is the whole function's. Nothing in this suite pinned it
    /// before -- verified by moving the `lastChangeCount` assignment below
    /// every early return, which left all 232 tests green.
    func testAnUnsendableItemIsLoggedOncePerChangeNotOncePerPoll() {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pasteboard-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4, log: log)

        pasteboard.setImage(Data(repeating: 0x89, count: FrameConstants.maxImageBytes + 1))
        watcher.poll()
        watcher.poll()
        watcher.poll()
        log.flush()

        let contents = (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
        XCTAssertEqual(contents.components(separatedBy: "over the image limit").count - 1, 1,
                       "one line per clipboard change, not one per tick; got: \(contents)")
    }

    /// The same property as the test above, through the OTHER loud early
    /// return, and it is the one that test cannot reach: a read that comes
    /// back with nothing. Both are "record `lastChangeCount` before any early
    /// return"; the oversize return is loud in `pollLocked` itself, while this
    /// one is loud one layer DOWN -- `SystemPasteboard.readPNG` logs
    /// `could not convert the pasteboard image to PNG: dropping it` and then
    /// reports nothing, so through the `PasteboardReading` seam the two are
    /// indistinguishable. That is exactly why this test drives a real
    /// `SystemPasteboard` over a fake backend instead of a `FakePasteboard`:
    /// the line that hurts lives below the seam every other watcher test uses.
    ///
    /// A TIFF `NSBitmapImageRep` cannot decode is the realistic source. Left
    /// on the pasteboard it is polled forever, so a watcher that failed to
    /// consume the changeCount would log this line at the 400ms default
    /// interval -- over 200,000 a day, crossing the SSH channel into the Mac's
    /// own log, which is the same flood this branch already paid to remove
    /// once in Task 7.
    ///
    /// Verified to fail against `lastChangeCount = current` moved below only
    /// the unreadable-read return -- the mutation that all 234 other tests
    /// pass.
    func testAnUnreadableItemIsLoggedOncePerChangeNotOncePerPoll() {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pasteboard-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        // Offered as an image, with bytes no TIFF decoder accepts -- the same
        // fixture `testAFailedConversionReadsAsNothingAndIsLogged` uses to
        // pin the conversion failure itself.
        let board = FakePasteboardBackend(types: [.tiff], data: [.tiff: Data("not a tiff".utf8)])
        let watcher = PasteboardWatcher(pasteboard: SystemPasteboard(board, log: log),
                                        pollInterval: 0.4, log: log)

        board.changeCount = 1   // the user copied it; nothing has changed since
        watcher.poll()
        watcher.poll()
        watcher.poll()
        log.flush()

        let contents = (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
        XCTAssertEqual(contents.components(separatedBy: "could not convert").count - 1, 1,
                       "one line per clipboard change, not one per tick; got: \(contents)")
    }

    /// The other half of the image boundary, and deliberately NOT the shape
    /// its text sibling
    /// (`testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt`)
    /// takes: an image at exactly `maxImageBytes` is EMITTED, where text at
    /// exactly `maxTextBytes` is skipped. `maxImageBytes` bounds the image
    /// itself, so this encodes to a payload eight bytes over it -- 4,194,312
    /// -- which is still comfortably inside `maxPayloadBytes` (8,388,608).
    /// Writing the image guard the way the text one is written would refuse
    /// exactly the maximum-size screenshot the three separated caps exist to
    /// permit.
    func testAnImageAtExactlyTheImageLimitIsStillEmitted() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()

        let png = Data(repeating: 0x89, count: FrameConstants.maxImageBytes)
        pasteboard.setImage(png)
        watcher.poll()

        XCTAssertEqual(seen.count, 1,
                       "an image of exactly the cap is what the separated caps exist to permit")
        XCTAssertEqual(try? ImagePayload.encode(ts: 1, png: png).count,
                       FrameConstants.maxImageBytes + ClipPayloadConstants.timestampBytes,
                       "sanity check on the boundary itself")
    }

    func testEmptyClipIsNotEmitted() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set("")
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    /// The guard the test above cannot reach. `pollLocked` refuses an empty
    /// body under EITHER kind, and until this task that clause rode along on
    /// the same `guard` as the text-only kind check -- so generalising the
    /// kind check is exactly the edit that could unpin it, silently, for
    /// images. The PC agent had this same guard become unpinned when its own
    /// read was generalised, which is why it is now explicit and tested on
    /// both sides.
    ///
    /// Zero bytes has nothing to say under either codec: `ImagePayload.decode`
    /// refuses an empty body outright (`ClipPayloadError.emptyBody`), and an
    /// empty text clip is applied as a silent no-op by both agents -- so
    /// either would be a frame sent for no effect.
    func testAnEmptyBodyIsNotEmittedUnderEitherKind() {
        for kind in [ClipKind.text, .image] {
            let pasteboard = EmptyBodyPasteboard()
            let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
            var seen: [Data] = []
            watcher.onChange = { _, data, _ in seen.append(data) }
            watcher.poll()
            pasteboard.bump(kind)
            watcher.poll()
            XCTAssertTrue(seen.isEmpty, "an empty \(kind.rawValue) body must not be emitted")
        }
    }

    func testOurOwnWriteIsNotEchoed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()

        watcher.noteWrittenLocally(kind: .text, payload: Data("from the peer".utf8))
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
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxTextBytes + 1))
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    /// The pre-v2 guard checked the watched TEXT against the frame cap
    /// directly, which was exact back when that text WAS the frame payload.
    /// Since Task 9, `wireAgent` wraps this text in `ClipPayload(ts:text:)`
    /// before it reaches the wire, adding an 8-byte prefix -- so text at
    /// exactly the cap would encode to a payload 8 bytes OVER the TEXT
    /// limit, and the peer's `Frame.decode` would reject the resulting
    /// frame as oversized if it also exceeded the (larger) frame cap.
    /// `testOversizedClipIsSkipped` above (`max + 1`) cannot see this: it is
    /// oversized under either the old or the new guard, so it sails past
    /// the boundary this test targets.
    /// Since Task 4, `FrameConstants.maxTextBytes` (this guard) and
    /// `FrameConstants.maxPayloadBytes` (the wire's frame cap, enforced only
    /// by `Frame.decode`) are separate constants that happen to still share
    /// this number -- this test targets the text-content limit specifically.
    func testTextAtExactlyTheCapIsSkippedBecauseTheEncodedFrameWouldExceedIt() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxTextBytes))
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
        watcher.onChange = { _, data, _ in seen.append(data) }
        watcher.poll()
        let text = String(repeating: "x",
                          count: FrameConstants.maxTextBytes - ClipPayloadConstants.timestampBytes)
        pasteboard.set(text)
        watcher.poll()
        XCTAssertEqual(seen, [Data(text.utf8)],
                       "must still be emitted -- the encoded ClipPayload is exactly at the cap, not over it")
        XCTAssertEqual(ClipPayload(ts: 1, text: text).encode().count, FrameConstants.maxTextBytes,
                       "sanity check on the boundary itself")
    }

    /// A user whose large local copy silently never reaches the peer has
    /// nothing to look at otherwise -- the Python agent already logs its
    /// analogous skip ("skipping a clip of N bytes: over the text limit").
    /// `log` is optional and defaulted to `nil` on every other test in this
    /// file precisely so this is the only one that needs to pass a real one.
    func testOversizedClipIsLoggedWithItsSize() {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pasteboard-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4, log: log)
        watcher.poll()
        let oversized = FrameConstants.maxTextBytes
        pasteboard.set(String(repeating: "x", count: oversized))
        watcher.poll()
        log.flush()

        let contents = try? String(contentsOfFile: logPath, encoding: .utf8)
        XCTAssertEqual(contents?.contains("skipping a clip of \(oversized) bytes"), true,
                       "expected the skip to be logged with its size; got: \(contents ?? "<unreadable>")")
    }
}

// MARK: - Race coverage (added beyond the brief; see the FIRST plan's
// task-13-report.md, under 2026-07-30-clipwire-implementation/ -- that Task
// 13 built this watcher and added this lock. Not v3's Task 13, which shares
// the number and the file but is the Mac's image path.)
//
// The v2 plan's Task 11 (not v3's, which is the send branch's verification --
// the two plans number independently) found that on the PC side, the watcher
// thread and the main thread shared echo-suppression state with no lock, and
// the watcher read the clipboard *before* consuming the suppression. Two
// back-to-back incoming
// clips then made it compare a stale value and send our own clip back to the
// peer; fixed with a generation counter that let the watcher detect and
// discard a comparison made stale by a write racing its slow (up to 3s,
// forked wl-paste) read.
//
// PasteboardWatcher has the same two actors: the timer's poll() and the
// channel's frame handler, which writes an incoming clip and calls
// noteWrittenLocally() from a different thread. A plain lock covering
// poll()'s [read -> consult echo] step and noteWrittenLocally()'s arm is
// both necessary and sufficient here, with no generation counter — and the
// reason is a correctness argument, not a speed one. Since Task 8,
// SystemPasteboard.read() is NOT unconditionally cheap: an image goes
// through an NSBitmapImageRep decode and a PNG re-encode inside the lock,
// which on a screenshot is comparable to the forked wl-paste call the PC
// side had to dodge. It stays bounded because the changeCount guard returns
// before the read on an unchanged board, so that conversion runs at most
// once per clipboard change. See PasteboardWatcher's own class comment for
// why a counter would not have caught this gap regardless. This test proves
// the "necessary" half empirically: it fails against an unprotected
// implementation and passes once the two are made mutually exclusive.

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

    func read() -> (kind: ClipKind, data: Data)? {
        let snapshot = text
        if blockNextRead {
            blockNextRead = false
            readingStarted.signal()
            _ = proceedWithRead.wait(timeout: .now() + 2)
        }
        guard let snapshot, !snapshot.isEmpty else { return nil }
        return (.text, snapshot)
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
        watcher.onChange = { _, data, _ in
            seenLock.lock(); seen.append(data); seenLock.unlock()
        }

        watcher.poll() // baseline: changeCount 0 == lastChangeCount 0, no-op

        // Clip "A" arrives from the peer: written locally, suppression armed —
        // exactly what the future channel's frame handler will do.
        watcher.noteWrittenLocally(kind: .text, payload: Data("A".utf8))
        pasteboard.set("A")

        // Make the read that follows pause mid-flight.
        pasteboard.blockNextRead = true
        let pollFinished = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            watcher.poll()
            pollFinished.signal()
        }

        // Wait until poll() is inside read(), holding "A", about to return.
        XCTAssertEqual(pasteboard.readingStarted.wait(timeout: .now() + 2), .success,
                       "poll() never reached read()")

        // While that read is outstanding, clip "B" arrives on another thread:
        // written locally, suppression armed for B — the second of two
        // back-to-back incoming clips, as in the Task 11 race.
        let aboutToArmB = DispatchSemaphore(value: 0)
        let armedB = DispatchSemaphore(value: 0)
        DispatchQueue.global().async {
            pasteboard.set("B")
            aboutToArmB.signal()
            watcher.noteWrittenLocally(kind: .text, payload: Data("B".utf8))
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

// MARK: - Fix round 1: changeCount read outside stateLock (see the FIRST
// plan's task-13-report.md, under 2026-07-30-clipwire-implementation/ --
// same collision as the MARK above)
//
// Review found that the first fix left `changeCount` itself read, and
// `lastChangeCount` written, *before* `stateLock` was acquired — a gap the
// BlockingPasteboard test above cannot see, because it interleaves inside
// read(), which was already under the lock. This double interleaves in
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
/// `read()`), to let a test land a complete second clip cycle in the gap
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

    func read() -> (kind: ClipKind, data: Data)? {
        guard !text.isEmpty else { return nil }
        return (.text, text)
    }
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
        watcher.onChange = { _, data, _ in
            seenLock.lock(); seen.append(data); seenLock.unlock()
        }

        // Clip A: armed and written, but not yet polled.
        watcher.noteWrittenLocally(kind: .text, payload: Data("A".utf8))
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
            watcher.noteWrittenLocally(kind: .text, payload: Data("B".utf8))
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

// MARK: - Task 8: one canonical read, kind-aware (mirrors test_clipboard.py's
// TestCanonicalRead)

/// A double for the narrow slice of `NSPasteboard` that `SystemPasteboard`
/// actually touches. `NSPasteboard` itself is not injectable -- there is one
/// `general` board per session, shared with every other app on the machine --
/// so without this the TIFF-to-PNG conversion could only be exercised by
/// writing to the user's real clipboard, which the test suite must never do.
///
/// `PasteboardBackend` is deliberately narrow (six members) rather than a
/// mirror of `NSPasteboard`: the smaller it is, the less of AppKit a fake has
/// to imitate convincingly, and every member here is one the production code
/// provably calls.
final class FakePasteboardBackend: PasteboardBackend {
    var changeCount = 0
    var types: [NSPasteboard.PasteboardType]?
    private var bodies: [NSPasteboard.PasteboardType: Data]
    private(set) var clearCount = 0
    private(set) var written: [(type: NSPasteboard.PasteboardType, data: Data?)] = []

    init(types: [NSPasteboard.PasteboardType], data: [NSPasteboard.PasteboardType: Data] = [:]) {
        self.types = types
        self.bodies = data
    }

    /// Real `NSPasteboard.string(forType:)` returns nil for bytes that are
    /// not valid UTF-8 rather than substituting replacement characters, and
    /// this double matches that: `SystemPasteboard.read()`'s text branch
    /// goes through it precisely so the bytes it returns are always valid
    /// UTF-8, and a double that lossily decoded instead would hide a
    /// regression that swapped it for a raw `data(forType:)` read.
    func string(forType dataType: NSPasteboard.PasteboardType) -> String? {
        guard let data = bodies[dataType] else { return nil }
        return String(data: data, encoding: .utf8)
    }

    func data(forType dataType: NSPasteboard.PasteboardType) -> Data? { bodies[dataType] }

    @discardableResult
    func clearContents() -> Int {
        clearCount += 1
        bodies = [:]
        types = []
        changeCount += 1
        return changeCount
    }

    @discardableResult
    func setData(_ data: Data?, forType dataType: NSPasteboard.PasteboardType) -> Bool {
        written.append((dataType, data))
        guard let data else { return true }
        bodies[dataType] = data
        types = (types ?? []) + [dataType]
        return true
    }
}

final class CanonicalReadTests: XCTestCase {
    /// One row of `fixtures/clipkind.json`. `expect` decodes as `ClipKind?`
    /// rather than `String?` so a typo'd value ("txt") throws here instead of
    /// silently reading as null and turning a row into a weaker assertion
    /// than it looks.
    ///
    /// `uti` is NOT optional, deliberately: adding a row to the shared table
    /// without deciding what the Mac does with it then fails to decode, which
    /// is the whole point of the column. `types` (the Wayland/X11 vocabulary
    /// the PC's `choose_kind` consumes) is decoded too, unused here but named
    /// so a reader of this file can see there are two vocabularies and one
    /// shared verdict.
    struct ClipKindRow: Decodable {
        let name: String
        let types: [String]
        let uti: [String]
        let expect: ClipKind?
    }

    private func loadClipKindFixture() throws -> [ClipKindRow] {
        // Tests/clipwireTests/ -> repo root -> fixtures/clipkind.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/clipkind.json"))
        let rows = try JSONDecoder().decode([ClipKindRow].self, from: data)
        XCTAssertFalse(rows.isEmpty, "fixtures/clipkind.json must not be empty")
        return rows
    }

    /// Same fixture the Python suite reads (test_clipboard.py's
    /// `test_the_kind_matches_the_shared_fixture`). Two implementations of
    /// one rule stay honest only if both are pinned to the same table.
    ///
    /// The table pins the DECISION, in a shared `expect` column, and gives
    /// each side its own vocabulary column: `types` is what a Wayland/X11
    /// clipboard offers, `uti` is what `NSPasteboard` offers. That split is
    /// not cosmetic. The two sides' sets of USABLE image types genuinely
    /// differ -- the PC considers only `image/png` (GPaste re-offers PNG for
    /// whatever it holds, verified on the live machine), while this side
    /// also accepts `public.tiff` as a fallback, for sources that offer TIFF
    /// without PNG (AppKit's own `NSImage`/`writeObjects` is one; verified
    /// directly -- it offers `public.tiff` and its legacy NeXT alias and no
    /// PNG at all). Screenshots are NOT such a source: `screencapture -x -c`
    /// offers `public.png` first, so the common case never reaches the
    /// conversion. A single-vocabulary table with a translation at this
    /// boundary could not express a per-side difference like that without
    /// mapping one side's types onto the other's and lying about one of
    /// them.
    ///
    /// The row named "image, each side's own usable format only" is the one
    /// that actually EXERCISES that divergence rather than merely being
    /// compatible with it: one user situation (an image and nothing else),
    /// `image/png` in the Wayland column, `public.tiff` in the UTI column,
    /// and one shared verdict. Every other image row lists a type both sides
    /// accept, so the split would be untested without it.
    func testTheKindMatchesTheSharedFixture() throws {
        for row in try loadClipKindFixture() {
            XCTAssertEqual(chooseKind(offeredTypes: row.uti), row.expect, row.name)
        }
    }

    /// The row that closes the gap Task 7's review found: without it, every
    /// remaining row passes an implementation that matched any image type at
    /// all, so "only the image types this side can actually use are
    /// considered" could drift with the shared table unable to notice. Named
    /// here so deleting the row from the fixture fails loudly rather than
    /// quietly reducing coverage.
    func testTheSharedFixturePinsAnUnusableImage() throws {
        let rows = try loadClipKindFixture()
        guard let row = rows.first(where: { $0.name == "unusable image only" }) else {
            return XCTFail("fixtures/clipkind.json must keep the unusable-image row")
        }
        XCTAssertNil(row.expect, "an image in a format this side cannot use must select no kind")
        XCTAssertNil(chooseKind(offeredTypes: row.uti))
    }

    /// A one-pixel bitmap encoded as TIFF, built through `NSBitmapImageRep`
    /// only -- no `NSImage`, no drawing context, nothing that needs a window
    /// server -- so this runs identically under `swift test` on a headless
    /// machine and on a desktop.
    private func makeOnePixelTIFF() -> Data? {
        let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: 1, pixelsHigh: 1, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
            colorSpaceName: .deviceRGB, bytesPerRow: 4, bitsPerPixel: 32)
        return rep?.representation(using: .tiff, properties: [:])
    }

    private static let pngMagic = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])

    /// A board offering TIFF and no PNG must still sync, as PNG: the wire
    /// format is PNG (`ImagePayload` carries PNG bytes, and the PC's
    /// `wl-copy` is handed `--type image/png`), so a peer handed TIFF bytes
    /// under that contract would store an unopenable image.
    ///
    /// Renamed from `testAScreenshotIsConvertedToPNG` (the task brief's
    /// name) because that premise turned out to be false: a real screenshot
    /// offers `public.png` FIRST -- verified with `screencapture -x -c` --
    /// and so takes `readPNG()`'s direct path, never this one. The genuine
    /// TIFF-only source is an app writing an image through AppKit's
    /// `NSImage`/`writeObjects`, which offers `public.tiff` and its legacy
    /// NeXT alias and nothing else (verified on a private named pasteboard).
    /// The test is unchanged and still load-bearing; only the claim about
    /// which real-world source reaches it was wrong.
    func testATIFFOnlyBoardIsConvertedToPNG() throws {
        let tiff = try XCTUnwrap(makeOnePixelTIFF())
        let board = FakePasteboardBackend(types: [.tiff], data: [.tiff: tiff])
        let read = try XCTUnwrap(SystemPasteboard(board).read())
        XCTAssertEqual(read.kind, .image)
        XCTAssertEqual(read.data.prefix(8), Self.pngMagic,
                       "the body on the wire must be PNG, not the TIFF the pasteboard held")
        XCTAssertNotEqual(read.data, tiff, "the TIFF must not have been passed through unconverted")
    }

    /// PNG already on the board is taken as-is: re-encoding bytes that are
    /// already in the wire format would change the hash of identical
    /// content, and both sides' reconciliation compares hashes.
    func testAnOfferedPNGIsTakenWithoutReEncoding() throws {
        let png = Self.pngMagic + Data([0x00, 0x00, 0x00, 0x0D])
        let board = FakePasteboardBackend(types: [.png], data: [.png: png])
        let read = try XCTUnwrap(SystemPasteboard(board).read())
        XCTAssertEqual(read.kind, .image)
        XCTAssertEqual(read.data, png, "PNG on the board must reach the wire byte-for-byte")
    }

    /// Spreadsheets put a bitmap of the copied cells on the pasteboard
    /// alongside the text; preferring the image would turn every copied
    /// range into a picture of a table -- a regression of the primary flow
    /// in exchange for the new one. Byte-for-byte the same rule
    /// `choose_kind` applies on the PC.
    func testTextWinsOverAnImage() throws {
        let board = FakePasteboardBackend(
            types: [.string, .png],
            data: [.string: Data("hi".utf8), .png: Data([0x89])])
        let read = try XCTUnwrap(SystemPasteboard(board).read())
        XCTAssertEqual(read.kind, .text)
        XCTAssertEqual(read.data, Data("hi".utf8))
    }

    /// A failed conversion is nothing -- never the unconverted TIFF, never a
    /// placeholder. Substituting either would put bytes on the wire that
    /// claim to be PNG and are not.
    ///
    /// It is logged HERE rather than by the caller, unlike what the task
    /// brief specified: `read()` returns a bare optional, so nil for a failed
    /// conversion is indistinguishable at every call site from nil for an
    /// empty pasteboard -- the ordinary, uneventful case that must stay
    /// silent -- and `resolveCurrentClipState`, one of those call sites, has
    /// no logger at all. The optional `log` follows `PasteboardWatcher`'s
    /// existing idiom for exactly this: defaulted to nil so every other call
    /// site compiles unchanged, with only `runAgent()` passing a real one.
    func testAFailedConversionReadsAsNothingAndIsLogged() throws {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-pasteboard-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        let board = FakePasteboardBackend(types: [.tiff], data: [.tiff: Data("not a tiff".utf8)])

        XCTAssertNil(SystemPasteboard(board, log: log).read(),
                     "unconvertible image bytes must read as nothing, not be passed through")

        log.flush()
        let contents = try? String(contentsOfFile: logPath, encoding: .utf8)
        XCTAssertEqual(contents?.contains("could not convert"), true,
                       "a dropped image must not be silent; got: \(contents ?? "<unreadable>")")
    }

    /// The read-side half of the fixture's unusable-image row, at the
    /// `read()` level rather than `chooseKind`'s: an image this side cannot
    /// convert is not read at all -- no body fetch, no guess.
    func testAnUnusableImageIsNotRead() {
        let jpeg = NSPasteboard.PasteboardType("public.jpeg")
        let board = FakePasteboardBackend(types: [jpeg], data: [jpeg: Data([0xFF, 0xD8, 0xFF])])
        XCTAssertNil(SystemPasteboard(board).read())
    }

    /// *** The reason `string(forType:)` is in `PasteboardBackend` at all. ***
    ///
    /// Bytes that are not valid UTF-8, offered under `public.utf8-plain-text`:
    /// `chooseKind` selects `.text` (the type IS on offer), and the body read
    /// then refuses them, so nothing is synced. That is the correct outcome
    /// for content this protocol cannot carry.
    ///
    /// Swapping the read back to `data(forType: .string)` -- trimming the
    /// protocol toward the task brief's shape -- would not drop this content;
    /// it would sync it WRONG, and permanently. The raw bytes get hashed for
    /// the clip-state we store and announce, while the clip that actually
    /// goes out is built with `String(decoding:as:UTF8.self)` and carries
    /// U+FFFD substitutions. The peer stores the hash of the substituted
    /// text, the two stores disagree about that clip forever, and every
    /// reconnect resolves `sendMine` and re-sends it. This test is what makes
    /// that swap fail instead of passing green.
    ///
    /// Real `NSPasteboard` behaves exactly as the double does here, verified
    /// directly rather than assumed: `setData(Data([0xFF, 0xFE]),
    /// forType: .string)` then `string(forType: .string)` is nil, while
    /// `data(forType: .string)` returns `fffe`.
    ///
    /// (This is NOT what stops an applied remote clip echoing back -- that
    /// path is byte-identical either way, since `handleFrame` arms and writes
    /// the same `Data(decoded.text.utf8)`. See `PasteboardBackend`'s own doc
    /// comment.)
    func testInvalidUTF8UnderTheStringTypeReadsAsNothing() {
        let board = FakePasteboardBackend(types: [.string], data: [.string: Data([0xFF, 0xFE])])
        XCTAssertNil(SystemPasteboard(board).read(),
                     "bytes that are not valid UTF-8 must not enter the text path -- " +
                     "hashing them raw while sending them substituted makes the two " +
                     "stores disagree permanently")
    }

    /// An empty body is nothing to sync, matching `WaylandClipboard.read()`,
    /// which returns None for an empty stdout.
    func testAnEmptyTextBodyReadsAsNothing() {
        let board = FakePasteboardBackend(types: [.string], data: [.string: Data()])
        XCTAssertNil(SystemPasteboard(board).read())
    }

    func testAnEmptyBoardReadsAsNothing() {
        XCTAssertNil(SystemPasteboard(FakePasteboardBackend(types: [])).read())
    }

    /// `changeCount` is what `PasteboardWatcher` polls; it has to come from
    /// the real board rather than being invented by the wrapper, or the
    /// watcher would never observe a change.
    func testChangeCountComesFromTheBoard() {
        let board = FakePasteboardBackend(types: [])
        let pasteboard = SystemPasteboard(board)
        XCTAssertEqual(pasteboard.changeCount, 0)
        board.setData(Data("hi".utf8), forType: .string)
        board.changeCount += 1
        XCTAssertEqual(pasteboard.changeCount, board.changeCount)
    }

    /// Both write kinds land under the type a reader of that kind asks for:
    /// text under `.string` (what `read()`'s text branch and every other app
    /// on the machine reads), an image under `.png`.
    ///
    /// `clearContents()` first, and exactly once: without it the previous
    /// item's other representations survive, so writing an incoming image
    /// over an old text clip would leave BOTH on the board -- and text wins,
    /// so the very next read would return the stale text instead of the
    /// image just applied.
    func testWriteReplacesTheBoardContentsUnderTheRightType() {
        let board = FakePasteboardBackend(types: [.string], data: [.string: Data("old".utf8)])
        let png = Self.pngMagic
        SystemPasteboard(board).write(kind: .image, data: png)

        XCTAssertEqual(board.clearCount, 1, "the previous item's types must be cleared exactly once")
        XCTAssertEqual(board.written.count, 1)
        XCTAssertEqual(board.written.first?.type, .png)
        XCTAssertEqual(board.written.first?.data, png)
        XCTAssertNil(board.data(forType: .string), "the stale text must be gone, or it would win the next read")
    }

    func testWritingTextLandsUnderTheStringType() {
        let board = FakePasteboardBackend(types: [])
        SystemPasteboard(board).write(kind: .text, data: Data("hello".utf8))

        XCTAssertEqual(board.clearCount, 1)
        XCTAssertEqual(board.written.first?.type, .string)
        XCTAssertEqual(board.written.first?.data, Data("hello".utf8))
    }

    /// The round trip, on one object: what `write` puts on a board is what
    /// `read` gets back from it. This is the property that lets the Mac skip
    /// the read-back the PC side needs -- see `write(kind:data:)`'s own
    /// comment.
    func testWhatIsWrittenIsWhatIsReadBack() throws {
        let board = FakePasteboardBackend(types: [])
        let pasteboard = SystemPasteboard(board)
        let png = Self.pngMagic + Data([0x01, 0x02])
        pasteboard.write(kind: .image, data: png)

        let read = try XCTUnwrap(pasteboard.read())
        XCTAssertEqual(read.kind, .image)
        XCTAssertEqual(read.data, png, "NSPasteboard returns the bytes it was given, unlike GPaste")
    }
}
