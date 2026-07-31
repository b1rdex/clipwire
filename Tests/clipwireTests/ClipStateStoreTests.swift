// Tests/clipwireTests/ClipStateStoreTests.swift
import XCTest
@testable import clipwire

final class ClipStateStoreTests: XCTestCase {
    private var url: URL!
    private var store: ClipStateStore!

    override func setUp() {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-clip-state-\(UUID().uuidString).json")
        store = ClipStateStore(path: url.path)
    }

    override func tearDown() {
        try? FileManager.default.removeItem(at: url)
    }

    // MARK: - load/save round trip

    func testRoundTrip() throws {
        let state = ClipState(sha256: "deadbeefcafe", ts: 1785400000.5)
        try store.save(state)
        XCTAssertEqual(store.load(), state)
    }

    func testSecondSaveOverwritesTheFirst() throws {
        try store.save(ClipState(sha256: "aa", ts: 1))
        try store.save(ClipState(sha256: "bb", ts: 2))
        XCTAssertEqual(store.load(), ClipState(sha256: "bb", ts: 2))
    }

    func testNilHashRoundTrips() throws {
        let state = ClipState(sha256: nil, ts: 0)
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
        try store.save(ClipState(sha256: "aa", ts: 1))
        let tmp = url.appendingPathExtension("tmp")
        XCTAssertFalse(FileManager.default.fileExists(atPath: tmp.path),
                       "the temp file used for the atomic replace must not linger")
    }

    func testSaveCreatesIntermediateDirectories() throws {
        let nested = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-clip-state-\(UUID().uuidString)")
            .appendingPathComponent("nested/clip-state.json")
        let nestedStore = ClipStateStore(path: nested.path)
        try nestedStore.save(ClipState(sha256: "aa", ts: 1))
        XCTAssertEqual(nestedStore.load(), ClipState(sha256: "aa", ts: 1))
        try? FileManager.default.removeItem(at: nested.deletingLastPathComponent().deletingLastPathComponent())
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
    func testStoredHashMatchesCurrentReturnsStoredTimestampNotNow() {
        let stored = ClipState(sha256: "aa", ts: 100)
        let result = resolveStartupState(currentHash: "aa", stored: stored, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "aa", ts: 100))
    }

    func testStoredHashDiffersReturnsNow() {
        let stored = ClipState(sha256: "aa", ts: 100)
        let result = resolveStartupState(currentHash: "bb", stored: stored, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "bb", ts: 999))
    }

    func testNothingStoredReturnsNow() {
        let result = resolveStartupState(currentHash: "aa", stored: nil, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "aa", ts: 999))
    }

    /// A `nil` current hash always wins over whatever is on disk, and it
    /// takes precedence even when something IS stored: `resolveFreshness`
    /// never compares timestamps when either side's hash is `nil`, so `ts`
    /// is unread downstream here -- this pins current behaviour (`now`)
    /// rather than asserting a hard requirement on its exact value.
    func testCurrentHashNilReturnsNilHashRegardlessOfStored() {
        let stored = ClipState(sha256: "aa", ts: 100)
        let result = resolveStartupState(currentHash: nil, stored: stored, now: 999)
        XCTAssertNil(result.sha256)
        XCTAssertEqual(result.ts, 999)
    }

    func testCurrentHashNilAndNothingStoredReturnsNilHash() {
        let result = resolveStartupState(currentHash: nil, stored: nil, now: 999)
        XCTAssertNil(result.sha256)
    }
}
