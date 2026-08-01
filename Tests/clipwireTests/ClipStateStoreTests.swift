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
            resolveStartupState(currentHash: sha256Hex(Data("current".utf8)), currentKind: .text,
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
            // Only `concurrentStore` crosses into the closure: `self` is a
            // non-Sendable `XCTestCase`, and capturing it inside a `@Sendable`
            // closure is a warning this project does not ship.
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
    /// its real recorded kind too, not just its timestamp -- and the STORED
    /// kind, not the one just read. `currentKind` here is deliberately the
    /// opposite of `stored`'s, so returning either one is distinguishable:
    /// unchanged content did not change what kind of content it is, and the
    /// stored value is the one this side already announced to the peer,
    /// possibly on an earlier connection.
    func testStoredHashMatchesCurrentReturnsStoredTimestampNotNow() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .image)
        let result = resolveStartupState(currentHash: "aa", currentKind: .text,
                                         stored: stored, now: 999)
        XCTAssertEqual(result, stored)
    }

    /// *** The kind-threading deliverable. *** Content that changed while
    /// nothing was watching gets `now` for its age AND the kind of the read
    /// that just observed it -- never a hardcoded guess.
    ///
    /// `currentKind: .image` against a stored `.text` is what makes the
    /// three possible implementations distinguishable, and passing `.text`
    /// would prove none of it: `.image` is returned only by threading the
    /// parameter through, `.text` would mean either the old hardcoding or a
    /// value copied from `stored`. Before Task 8 this branch hardcoded
    /// `.text`, which was harmless only because the pasteboard read could
    /// not report an image at all; the moment it could, that hardcoding
    /// would have labelled a PNG's hash `.text` in the persistent store and
    /// on the wire, with no compiler error anywhere to point at it (this
    /// function's signature was unchanged) -- so the fix is the parameter
    /// itself, which makes dropping the kind a compile error at every call
    /// site rather than a silent wrong answer at one.
    func testStoredHashDiffersReturnsNowAndTheKindThatWasActuallyRead() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .text)
        let result = resolveStartupState(currentHash: "bb", currentKind: .image,
                                         stored: stored, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "bb", ts: 999, kind: .image),
                       "the changed-content branch must report the kind it was given, " +
                       "not the stored kind and not a hardcoded .text")
    }

    /// Same branch, reached the other way: nothing was ever stored, so the
    /// content appeared while nothing was watching. `.image` again, for the
    /// same reason -- a hardcoding here would be invisible against `.text`.
    func testNothingStoredReturnsNowAndTheKindThatWasActuallyRead() {
        let result = resolveStartupState(currentHash: "aa", currentKind: .image,
                                         stored: nil, now: 999)
        XCTAssertEqual(result, ClipState(sha256: "aa", ts: 999, kind: .image))
    }

    /// A `nil` current hash always wins over whatever is on disk, and it
    /// takes precedence even when something IS stored: `resolveFreshness`
    /// never compares timestamps when either side's hash is `nil`, so `ts`
    /// is unread downstream here -- this pins current behaviour (`now`)
    /// rather than asserting a hard requirement on its exact value.
    ///
    /// `currentKind` is `.image` and must NOT survive: a hash-less state
    /// carrying a kind violates the nil-iff-nil rule `ClipState.init(from:)`
    /// enforces on the wire, and `ClipState`'s memberwise initializer does
    /// not enforce it, so nothing downstream would catch a leak here before
    /// the peer's decoder rejected the frame.
    func testCurrentHashNilReturnsNilHashRegardlessOfStored() {
        let stored = ClipState(sha256: "aa", ts: 100, kind: .text)
        let result = resolveStartupState(currentHash: nil, currentKind: .image,
                                         stored: stored, now: 999)
        XCTAssertNil(result.sha256)
        XCTAssertEqual(result.ts, 999)
        XCTAssertNil(result.kind, "a nil hash must carry a nil kind, whatever the read reported")
    }

    func testCurrentHashNilAndNothingStoredReturnsNilHash() {
        let result = resolveStartupState(currentHash: nil, currentKind: nil, stored: nil, now: 999)
        XCTAssertNil(result.sha256)
    }

    // MARK: - resolveCurrentClipState — the hash and the kind come from ONE read

    /// The other half of the deliverable, and the frame the compiler does
    /// NOT protect: `resolveStartupState`'s new parameter can be satisfied
    /// by anything of the right type, including a fresh guess. What makes it
    /// honest is that this function destructures a single
    /// `pasteboard.read()` pair and derives both values from it, so a hash
    /// and a kind can never be paired up from two different readings of the
    /// clipboard.
    ///
    /// An image-only pasteboard against a STORED `.text` state with a
    /// different hash: `.image` can only come from the read, `.text` would
    /// mean the kind was dropped somewhere between the read and the result.
    func testResolveCurrentClipStateReportsTheKindOfTheBytesItHashed() {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x01])
        let pasteboard = FakePasteboard()
        pasteboard.setImage(png)
        let stored = ClipState(sha256: String(repeating: "ab", count: 32), ts: 100, kind: .text)

        let result = resolveCurrentClipState(pasteboard: pasteboard, stored: stored, now: 999, log: nil)

        XCTAssertEqual(result, ClipState(sha256: sha256Hex(png), ts: 999, kind: .image),
                       "the hash and the kind must both come from the one read that produced them")
    }

    /// The same function on the text path, so the test above cannot pass by
    /// simply always reporting `.image`.
    func testResolveCurrentClipStateReportsTextForATextPasteboard() {
        let pasteboard = FakePasteboard()
        pasteboard.set("hello")

        let result = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999, log: nil)

        XCTAssertEqual(result, ClipState(sha256: sha256Hex(Data("hello".utf8)), ts: 999, kind: .text))
    }

    /// An unreadable pasteboard is hashless and kindless -- never hashed,
    /// matching the wire contract that `sha256` is null for exactly this
    /// clipboard state.
    func testResolveCurrentClipStateReportsNothingForAnEmptyPasteboard() {
        let result = resolveCurrentClipState(pasteboard: FakePasteboard(), stored: nil, now: 999, log: nil)
        XCTAssertNil(result.sha256)
        XCTAssertNil(result.kind)
    }

    // MARK: - The announce path's own size guard

    /// The announce path had no size cap on either machine, and the guards
    /// it lacked are the two the SENDERS already apply.
    ///
    /// Traced end to end: `PasteboardWatcher.pollLocked` skips an oversized
    /// body with a log and returns BEFORE `persistClipState`, so the store
    /// keeps its older entry -- but `resolveCurrentClipState` hashed the
    /// oversized body with no cap, `resolveStartupState` saw a hash
    /// differing from the store and stamped `now`, and `announceClipState`
    /// persisted and announced it. That announcement wins reconciliation
    /// against anything the peer copied earlier, and then `.sendMine`
    /// refuses the very content it won with, at its own size guard. The peer
    /// has by that point resolved `waitForPeer` and suppressed its own push,
    /// so its perfectly sendable clip never arrives -- the wake flow
    /// protocol v2 exists to serve, broken by content that cannot travel.
    ///
    /// A nil hash is the remedy `resolveFreshness` already understands: the
    /// peer wins and delivers. The PC agent's `resolve_current_clip_state`
    /// carries the identical guard and the byte-identical lines.
    private func loggedLines(_ body: (Log) -> Void) -> [String] {
        let path = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-announce-limit-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: path)
        body(log)
        log.flush()
        let contents = (try? String(contentsOfFile: path, encoding: .utf8)) ?? ""
        return contents.split(separator: "\n").map {
            String($0.drop(while: { $0 != " " }).dropFirst())
        }
    }

    func testAnOversizedImageResolvesANilHashAndKind() {
        let oversized = Data(repeating: 0x89, count: FrameConstants.maxImageBytes + 1)
        let pasteboard = FakePasteboard()
        pasteboard.setImage(oversized)
        var result: ClipState?

        let lines = loggedLines { log in
            result = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999, log: log)
        }

        XCTAssertEqual(result, ClipState(sha256: nil, ts: 999, kind: nil),
                       "content this side can never send must be announced as nothing, "
                       + "not hashed and stamped `now`")
        XCTAssertEqual(lines, ["not announcing an image of \(oversized.count) bytes: over the image limit"],
                       "a silent skip is how a user concludes the tool is broken")
    }

    /// The boundary the three separated caps exist to permit. Written in the
    /// text guard's shape this would refuse exactly the maximum-size
    /// screenshot every send site accepts, and the two would disagree about
    /// one image.
    func testAnImageAtExactlyTheLimitIsStillAnnounced() {
        let exact = Data(repeating: 0x89, count: FrameConstants.maxImageBytes)
        let pasteboard = FakePasteboard()
        pasteboard.setImage(exact)
        var result: ClipState?

        let lines = loggedLines { log in
            result = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999, log: log)
        }

        XCTAssertEqual(result, ClipState(sha256: sha256Hex(exact), ts: 999, kind: .image))
        XCTAssertEqual(lines, [], "nothing was skipped, so nothing is worth a line")
    }

    /// Text at exactly `maxTextBytes` is already over: it is wrapped in an
    /// 8-byte timestamp before it reaches the wire, so the senders refuse it
    /// and this guard must refuse the same byte count.
    func testOversizedTextResolvesANilHashAndKind() {
        let pasteboard = FakePasteboard()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxTextBytes))
        var result: ClipState?

        let lines = loggedLines { log in
            result = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999, log: log)
        }

        XCTAssertEqual(result, ClipState(sha256: nil, ts: 999, kind: nil))
        XCTAssertEqual(lines,
                       ["not announcing a clip of \(FrameConstants.maxTextBytes) bytes: over the text limit"])
    }

    func testTextAtExactlyTheSendableBoundaryIsStillAnnounced() {
        let exact = String(repeating: "x",
                           count: FrameConstants.maxTextBytes - ClipPayloadConstants.timestampBytes)
        let pasteboard = FakePasteboard()
        pasteboard.set(exact)
        var result: ClipState?

        let lines = loggedLines { log in
            result = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999, log: log)
        }

        XCTAssertEqual(result, ClipState(sha256: sha256Hex(Data(exact.utf8)), ts: 999, kind: .text))
        XCTAssertEqual(lines, [])
    }

    /// The whole point, stated as the decision both sides actually run.
    /// Before the guard, `mine` carried a real hash stamped `now` and beat
    /// the peer's older-but-sendable clip; the peer then waited for a frame
    /// `.sendMine` had already refused to build.
    func testThePeerWinsInsteadOfBeingLockedOut() {
        let pasteboard = FakePasteboard()
        pasteboard.setImage(Data(repeating: 0x89, count: FrameConstants.maxImageBytes + 1))
        var mine = ClipState(sha256: nil, ts: 0, kind: nil)

        _ = loggedLines { log in
            mine = resolveCurrentClipState(pasteboard: pasteboard, stored: nil, now: 999_999, log: log)
        }

        let peer = ClipState(sha256: String(repeating: "aa", count: 32), ts: 100, kind: .text)
        XCTAssertEqual(resolveFreshness(mine: mine, peer: peer), .waitForPeer,
                       "a clip we cannot send must never win against one the peer can")
    }

    /// `announceClipState` persists whatever it resolved, and that must be
    /// the nil state here rather than the older entry `pollLocked`'s own
    /// skip left behind. A store still describing content the pasteboard no
    /// longer offers is the "store goes stale" disease this whole design
    /// closes: the next connection finds it, sees a hash the pasteboard does
    /// not hold, and stamps `now` on it.
    ///
    /// The reconciliation line stays silent, because a nil hash never
    /// reaches a timestamp comparison -- there is no judgement to report.
    func testAnOversizedPasteboardOverwritesTheStoreWithNothing() throws {
        try store.save(ClipState(sha256: String(repeating: "bb", count: 32), ts: 111, kind: .text))
        let pasteboard = FakePasteboard()
        pasteboard.setImage(Data(repeating: 0x89, count: FrameConstants.maxImageBytes + 1))
        var sent: [Frame] = []

        let lines = loggedLines { log in
            announceClipState(send: { sent.append($0) }, pasteboard: pasteboard,
                              clipStateStore: store, log: log, now: 999_999)
        }

        XCTAssertEqual(store.load(), ClipState(sha256: nil, ts: 999_999, kind: nil))
        XCTAssertEqual(try ClipState.decodePayload(XCTUnwrap(sent.first).payload),
                       ClipState(sha256: nil, ts: 999_999, kind: nil))
        XCTAssertFalse(lines.contains("clipboard changed while apart"))
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

    // MARK: - the file on disk

    /// The top-level keys of whatever is on disk right now.
    private func keysOnDisk() throws -> Set<String> {
        let data = try Data(contentsOf: url)
        let object = try JSONSerialization.jsonObject(with: data) as? [String: Any]
        return Set((object ?? [:]).keys)
    }

    /// *** Acceptance item 7 of protocol v3. *** The owner's
    /// `~/.local/state/clipwire/clip-state.json` is a bare `ClipState`, and
    /// every version of this store has to keep reading one. Written literally
    /// rather than round-tripped, because a round trip passes under a NESTED
    /// encoding too -- and under a nested one this file would fail to decode,
    /// `load()` would report "nothing stored" (its documented answer to a torn
    /// file), and the next announcement would stamp `now` on content that is
    /// actually old and win a reconciliation it should have lost. That is the
    /// clobber the persistent store exists to prevent, arriving through an
    /// upgrade.
    ///
    /// v3.1 wrapped the state in a `StoredClipState` carrying a second hash
    /// and had to encode it flat for exactly this reason; v3.2 deleted the
    /// wrapper with the density fix it served, so these bytes are once again
    /// simply what the store writes. The literal file is the guard that
    /// survived both moves.
    func testAStoreFileWrittenAsABareClipStateStillLoads() throws {
        let hash = String(repeating: "ab", count: 32)
        try Data(#"{"sha256":"\#(hash)","ts":1785400000.5,"kind":"text"}"#.utf8).write(to: url)

        XCTAssertEqual(store.load(), ClipState(sha256: hash, ts: 1785400000.5, kind: .text))
    }

    /// A file written by v3.1's two-hash store still loads, with its extra key
    /// ignored -- and no such file exists anywhere, which is the point. The
    /// density fix never fired on a real machine, so nothing ever wrote a
    /// `localSha256`; the removal needs no migration step, and this test is
    /// what says so in code rather than in a commit message.
    func testAV31StoreFileWithItsSecondHashStillLoadsAndIgnoresIt() throws {
        let hash = String(repeating: "ab", count: 32)
        try Data(#"""
        {"sha256":"\#(hash)","ts":1785400000.5,"kind":"image","localSha256":"\#(String(repeating: "cd", count: 32))"}
        """#.utf8).write(to: url)

        XCTAssertEqual(store.load(), ClipState(sha256: hash, ts: 1785400000.5, kind: .image),
                       "the extra key is one the keyed container never asks for")
    }

    /// The same property from the writing side: a record writes the three wire
    /// keys and nothing else, so a file this version writes is a file every
    /// earlier reader accepts.
    func testAnOrdinaryRecordWritesExactlyTheThreeOldKeys() throws {
        try store.save(ClipState(sha256: "aa", ts: 1, kind: .text))
        XCTAssertEqual(try keysOnDisk(), ["sha256", "ts", "kind"])
    }
}
