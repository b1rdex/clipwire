// Tests/clipwireTests/ClipStateStoreTests.swift
import XCTest
@testable import clipwire

final class ClipStateStoreTests: XCTestCase {
    private var url: URL!
    private var store: ClipStateStore!
    private var nestedRoot: URL!

    override func setUp() {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-clip-state-\(UUID().uuidString).json")
        store = ClipStateStore(path: url.path)
        nestedRoot = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-clip-state-\(UUID().uuidString)")
    }

    // tearDown always runs, pass or fail or throw, so this is the only
    // cleanup point that can't leave droppings behind on a failing `try` in
    // the middle of a test -- unlike a cleanup line at the end of a test
    // body, which a thrown error skips entirely.
    override func tearDown() {
        try? FileManager.default.removeItem(at: url)
        try? FileManager.default.removeItem(at: nestedRoot)
    }

    // MARK: - load/save round trip

    func testRoundTrip() throws {
        let state = ClipState(sha256: "deadbeefcafe", ts: 1785400000.5, kind: .text)
        try store.save(state)
        XCTAssertEqual(store.load(), state)
    }

    /// Different kinds on the two saves, not just different hashes: the
    /// second save must overwrite kind too, not only sha256/ts.
    func testSecondSaveOverwritesTheFirst() throws {
        try store.save(ClipState(sha256: "aa", ts: 1, kind: .text))
        try store.save(ClipState(sha256: "bb", ts: 2, kind: .image))
        XCTAssertEqual(store.load(), ClipState(sha256: "bb", ts: 2, kind: .image))
    }

    /// The deliberate asymmetry the final wave introduced, pinned so it is a
    /// decision rather than an oversight. `ClipState.decodePayload` rejects a
    /// `sha256` that is not 64 lowercase hex; `load()` decodes directly and
    /// does not, unlike the PC agent's `load_clip_state`, which shares its
    /// decoder with the wire path and so validates its store file too.
    ///
    /// Harmless in both directions, and this test says why: the store only
    /// ever holds `sha256Hex` output, so a malformed hash means corruption --
    /// and a corrupt one simply fails to match the current clipboard, taking
    /// `resolveStartupState`'s "content changed while apart" branch (`ts =
    /// now`, and now a log line saying so). That is the same outcome as the
    /// `nil` `load()` already returns for a torn file, so validating here
    /// would buy nothing while adding a fourth failure reason to `load()`'s
    /// documented contract. It also never reaches the wire: what gets
    /// announced is the LOCALLY computed hash, never the stored one.
    func testLoadDoesNotValidateTheStoredHashTheWayTheWireDecoderDoes() throws {
        let malformed = ClipState(sha256: "not a hex digest at all", ts: 100, kind: .text)
        try store.save(malformed)
        XCTAssertEqual(store.load(), malformed, "the store is deliberately lenient")

        XCTAssertThrowsError(try ClipState.decodePayload(malformed.encodePayload()),
                             "the same value must NOT be accepted off the wire")
        XCTAssertEqual(
            resolveStartupState(currentHash: sha256Hex(Data("current".utf8)),
                                stored: malformed, now: 999),
            ClipState(sha256: sha256Hex(Data("current".utf8)), ts: 999, kind: .text),
            "a corrupt stored hash resolves to `now`, exactly as nothing-stored does")
    }

    func testNilHashRoundTrips() throws {
        let state = ClipState(sha256: nil, ts: 0, kind: nil)
        try store.save(state)
        XCTAssertEqual(store.load(), state)
    }

    // MARK: - load() never throws

    func testMissingFileLoadsAsNil() {
        XCTAssertNil(store.load(), "no file at all means nothing is known yet")
    }

    /// Mirrors `StatusTests.testExistingButUndecodableFileDoesNotClaimAgentNeverRan`:
    /// a directory in place of a file exists but can never decode as `Data`,
    /// regardless of privilege level -- unlike `chmod 0o000`, which root
    /// bypasses and would make this flaky in CI.
    func testUnreadableFileLoadsAsNil() throws {
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        XCTAssertNil(store.load(), "a present-but-unreadable file must read as absent, not throw")
    }

    func testMalformedFileLoadsAsNil() throws {
        try Data("not json at all".utf8).write(to: url)
        XCTAssertNil(store.load(),
                      "a torn or corrupt state file must read as absent, never as a corrupt timestamp")
    }

    func testFileMissingRequiredKeyLoadsAsNil() throws {
        try Data(#"{"sha256": "aa"}"#.utf8).write(to: url)
        XCTAssertNil(store.load(), "valid JSON missing the required ts key is still not a ClipState")
    }

    // MARK: - atomic write

    func testSaveLeavesNoTempFileBehind() throws {
        try store.save(ClipState(sha256: "aa", ts: 1, kind: .text))
        let tmp = url.appendingPathExtension("tmp")
        XCTAssertFalse(FileManager.default.fileExists(atPath: tmp.path),
                       "the temp file used for the atomic replace must not linger")
    }

    func testSaveCreatesIntermediateDirectories() throws {
        let nested = nestedRoot.appendingPathComponent("nested/clip-state.json")
        let nestedStore = ClipStateStore(path: nested.path)
        try nestedStore.save(ClipState(sha256: "aa", ts: 1, kind: .text))
        XCTAssertEqual(nestedStore.load(), ClipState(sha256: "aa", ts: 1, kind: .text))
    }

    // MARK: - concurrent saves must not corrupt the file

    /// Task 9 gave this type its first two concurrent callers: the
    /// pasteboard watcher's timer thread (a local change) and the channel's
    /// decode thread (an applied remote clip) can now call `save()` at
    /// genuinely the same time. Both write the same fixed temp path with a
    /// plain, non-atomic `Data.write(to:)`; unsynchronized, one thread's
    /// write could in principle interleave with another's, and whichever
    /// `replaceItemAt` runs next would move a corrupt file into place --
    /// `load()` then reads it as "nothing stored," and the next connection
    /// stamps stale content with `now`, winning a reconciliation it should
    /// have lost.
    ///
    /// This hammers the store with 200 genuinely concurrent writers as a
    /// robustness/regression guard. Two other empirical approaches were
    /// tried and deliberately NOT kept, and are recorded here rather than
    /// silently dropped: (1) a standalone byte-corruption probe (2 MB and
    /// 20 MB payloads, a synchronized start gate, 300 trials) never
    /// reproduced a torn file on this machine's macOS/APFS, lock or no lock
    /// -- the write+rename pair appears to be serialized by the OS more
    /// strongly than `Data.write(to:)` documents, at least here; (2) a
    /// wall-clock timing test asserting that locked concurrent saves take
    /// close to N times a single call's duration, unlocked ones close to
    /// 1x -- an initial small sample showed a clean ~2x gap, but a wider
    /// run (8 trials each way) showed real overlap between the two
    /// distributions (without-lock ratios up to 1.42x the serial estimate;
    /// with-lock ratios as low as 0.47x), so no fixed threshold could
    /// avoid misclassifying some runs in either direction. Shipping that
    /// assertion would have been a flakier test than no test. The fix
    /// itself does not depend on any of this: it removes reliance on an
    /// atomicity guarantee `Data.write(to:)` does not document, costs
    /// nothing measurable, and mirrors `AgentStatus`'s existing lock around
    /// the identical temp-and-replace pattern in StatusFile.swift.
    func testConcurrentSavesNeverLeaveAFileThatFailsToLoad() {
        let concurrentStore = store!
        DispatchQueue.concurrentPerform(iterations: 200) { i in
            try? concurrentStore.save(ClipState(sha256: "writer-\(i)", ts: Double(i), kind: .text))
        }
        XCTAssertNotNil(concurrentStore.load(),
                        "concurrent saves must never leave a file that fails to load")
    }

    // MARK: - init(path:) expands ~

    /// No real home directory or filesystem access here -- pure string
    /// check that `init(path:)` actually calls `expandTilde`, per
    /// `Config.swift`/`Log.swift`'s existing pattern. Every other test in
    /// this file injects an absolute temp path, so forgetting `expandTilde`
    /// entirely would otherwise pass the whole rest of the suite.
    func testInitExpandsTilde() {
        let expanded = ClipStateStore(path: "~/x/y.json").url.path
        XCTAssertTrue(expanded.hasPrefix(NSHomeDirectory()), "expected \(expanded) to start under the home directory")
        XCTAssertFalse(expanded.contains("~"), "expected no literal tilde left in \(expanded)")
    }

    func testDefaultPathIsPinned() {
        XCTAssertEqual(ClipStateStoreConstants.defaultPath, "~/.local/state/clipwire/clip-state.json")
    }

    // MARK: - resolveStartupState — the four cases that make the wake flow work

    /// The load-bearing case: content that has not changed since it was
    /// last recorded must keep its real age. Returning `now` here instead
    /// would make a clip copied while the peer was asleep look freshly
    /// copied, winning it every reconciliation and clobbering the peer
    /// systematically in the other direction.
    /// Content that has not changed since it was last recorded must keep
    /// its real recorded kind too, not just its timestamp: `stored` here is
    /// deliberately `.image`, the "wrong" guess a bug hardcoding `.text`
    /// onto this branch would produce, so such a bug cannot pass by
    /// coincidence.
    func testStoredHashMatchesCurrentReturnsStoredTimestampNotNow() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .image)
        let result = resolveStartupState(currentHash: "aa", stored: stored, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "aa", ts: 100, kind: .image))
    }

    /// New content observed here is always `.text`, hardcoded --
    /// `resolveCurrentClipState`'s only production caller derives
    /// `currentHash` from `pasteboard.readText()` alone; see
    /// `resolveStartupState`'s own doc comment.
    func testStoredHashDiffersReturnsNow() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .text)
        let result = resolveStartupState(currentHash: "bb", stored: stored, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "bb", ts: 999, kind: .text))
    }

    func testNothingStoredReturnsNow() {
        let result = resolveStartupState(currentHash: "aa", stored: nil, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "aa", ts: 999, kind: .text))
    }

    /// A `nil` current hash always wins over whatever is on disk, and it
    /// takes precedence even when something IS stored: `resolveFreshness`
    /// never compares timestamps when either side's hash is `nil`, so `ts`
    /// is unread downstream here -- this pins current behaviour (`now`)
    /// rather than asserting a hard requirement on its exact value.
    func testCurrentHashNilReturnsNilHashRegardlessOfStored() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .text)
        let result = resolveStartupState(currentHash: nil, stored: stored, now: 999)
        XCTAssertNil(result.sha256)
        XCTAssertEqual(result.ts, 999)
        XCTAssertNil(result.kind, "a nil hash must carry a nil kind")
    }

    func testCurrentHashNilAndNothingStoredReturnsNilHash() {
        let result = resolveStartupState(currentHash: nil, stored: nil, now: 999)
        XCTAssertNil(result.sha256)
    }

    // MARK: - Task 6: a v2 store on disk must be rejected, not loaded as kindless

    /// protocol v3 adds `kind` to the clip-state wire AND store format
    /// (this task). A store file written by a v2 agent -- a real scenario
    /// after any upgrade, not a hypothetical -- has a real (non-null)
    /// `sha256` and no `"kind"` key at all.
    ///
    /// `ClipState.init(from:)` enforces "kind is nil exactly when sha256 is
    /// nil" directly (not only inside `decodePayload`), specifically so
    /// `load()`'s own direct `JSONDecoder().decode(ClipState.self, from:)`
    /// call -- which deliberately skips the sha256-hex-shape check above,
    /// see `testLoadDoesNotValidateTheStoredHashTheWayTheWireDecoderDoes`
    /// -- still enforces this one: `decodeIfPresent` returns `nil`
    /// identically whether the `"kind"` key is absent or explicitly `null`,
    /// so a non-null `sha256` with an absent `kind` key fails that
    /// equivalence exactly as a malformed wire payload would. Silently
    /// accepting it instead -- as "a hash of unknown kind" -- would feed
    /// `resolveStartupState`, and eventually the send branch (Task 11), a
    /// state with no kind to act on.
    func testAV2StoreFileIsRejectedNotLoadedAsKindless() throws {
        try Data(#"{"sha256": "\#(String(repeating: "aa", count: 32))", "ts": 100}"#.utf8).write(to: url)
        XCTAssertNil(store.load(),
                     "a v2 store (real hash, no kind key) must be rejected, not silently treated as a hash of unknown kind")
    }
}
