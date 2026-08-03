// Tests/clipwireTests/AgentWiringTests.swift
//
// Final review: runAgent()'s closure wiring was the last unpinned link in
// the echo chain. `handleFrame` (HandleFrameTests.swift) and
// `PasteboardWatcher` (PasteboardTests.swift) each prove their half of the
// echo-suppression contract in isolation, but nothing proved that
// `wireAgent` (extracted from `runAgent()` for exactly this
// purpose) actually connects them: that `channel.onFrame`'s handler is
// wired with `noteWrittenLocally: watcher.noteWrittenLocally` -- the SAME
// watcher instance whose own poll() will later observe the write. A
// mis-wired `noteWrittenLocally` (a no-op, or some other watcher) leaves
// the guard unarmed, and the watcher bounces the very clip it just
// received straight back out to the peer.
import XCTest
@testable import clipwire

final class AgentWiringTests: XCTestCase {
    private func tempStatusURL() -> URL {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-wiring-test-\(UUID().uuidString).json")
    }

    private func tempLog() -> Log {
        Log(path: FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-wiring-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path)
    }

    private func config() -> Config {
        Config(host: "pc", fallbackIP: nil, user: "me",
               identityFile: "~/.ssh/id_ed25519",
               remoteAgentPath: "~/.local/share/clipwire/clipwire-agent.py",
               macPollIntervalMs: 400)
    }

    private func tempClipStateStore() -> ClipStateStore {
        ClipStateStore(path: FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-wiring-test-\(UUID().uuidString).json").path)
    }

    func testAnIncomingClipDoesNotBounceBackThroughTheWiredWatcher() {
        // One FakePasteboard plays both roles -- read (for the watcher) and
        // write (for handleFrame) -- exactly as the single SystemPasteboard
        // does in production, so a write made through one side is the
        // change the other side's next poll observes.
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        let channel = Channel(config: config(), log: tempLog())
        let status = AgentStatus(pid: 1, url: tempStatusURL())

        wireAgent(channel: channel, watcher: watcher, pasteboard: pasteboard,
                  status: status, log: tempLog(), clipStateStore: tempClipStateStore(),
                  clipStateAnnouncement: ClipStateAnnouncement())

        // Second-round final review, Finding 3: a bare "onChange does not
        // re-fire" assertion cannot tell a correctly suppressed echo apart
        // from wireAgent never having assigned watcher.onChange at all --
        // in the latter case the count would ALSO stay 0, while Mac-to-PC
        // sync is entirely dead. Failing here, immediately and separately
        // from everything below, closes that gap directly.
        guard let wiredOnChange = watcher.onChange else {
            return XCTFail("wireAgent must assign watcher.onChange, or Mac-to-PC sync is dead")
        }

        // Observe whether the real wiring's onChange fires, without
        // replacing what it does -- it must still forward to channel.send
        // exactly as wireAgent set it up.
        var onChangeFireCount = 0
        watcher.onChange = { kind, data, observedAt in
            onChangeFireCount += 1
            wiredOnChange(kind, data, observedAt)
        }

        watcher.poll() // establishes the baseline changeCount, emits nothing

        // A clip arrives from the peer, through the wired channel.onFrame.
        // If wireAgent correctly bound noteWrittenLocally to THIS watcher,
        // handleFrame arms its suppression before writing "hello" to the
        // shared pasteboard -- which also bumps its changeCount, exactly as
        // a real incoming write bumps NSPasteboard.general's. The payload is
        // a real ClipPayload encoding (ts + text), not bare text, since v2's
        // clip frame carries its own timestamp.
        channel.onFrame?(Frame(type: .clip, payload: ClipPayload(ts: 1, text: "hello").encode()))
        XCTAssertEqual(pasteboard.text, Data("hello".utf8), "the clip must have been written")

        // The watcher's own next poll must not treat that write as a new
        // local change and echo it back to the peer.
        watcher.poll()

        XCTAssertEqual(onChangeFireCount, 0,
                       "the watcher must not bounce the very clip it just received back to " +
                       "the peer -- a mis-wired noteWrittenLocally leaves the guard unarmed " +
                       "and this fires")

        // A GENUINE local change, unrelated to the echo above, must still
        // reach onChange -- otherwise the assertion above could pass by
        // wireAgent simply never wiring onChange to fire on anything at
        // all, rather than by correctly suppressing the echo. (This proves
        // the watcher fires and the wired closure gets invoked; it does
        // not by itself prove that closure goes on to call channel.send --
        // the guard above is what proves wireAgent assigned a real,
        // non-nil closure in the first place.)
        pasteboard.set("typed")
        watcher.poll()
        XCTAssertEqual(onChangeFireCount, 1,
                       "a genuine local change must still reach onChange through the wiring -- " +
                       "the previous assertion alone cannot tell a correctly suppressed echo " +
                       "apart from onChange never firing at all")
    }

    /// Proves `wireAgent`'s `watcher.onChange` wiring does the freshness half
    /// of its job, not only the echo-forwarding half already covered above:
    /// a genuine local change must hash the new content and persist it
    /// alongside the moment it was OBSERVED (not some later moment), so
    /// `resolveStartupState`/`resolveFreshness` have something accurate to
    /// compare against on the next connection.
    func testAGenuineLocalChangeStoresItsHashAndObservationTimestamp() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        let channel = Channel(config: config(), log: tempLog())
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let clipStateStore = tempClipStateStore()

        wireAgent(channel: channel, watcher: watcher, pasteboard: pasteboard, status: status,
                  log: tempLog(), clipStateStore: clipStateStore, clipStateAnnouncement: ClipStateAnnouncement())

        watcher.poll() // baseline
        let before = Date().timeIntervalSince1970
        pasteboard.set("typed by the user")
        watcher.poll()
        let after = Date().timeIntervalSince1970

        let stored = clipStateStore.load()
        XCTAssertEqual(stored?.sha256, sha256Hex(Data("typed by the user".utf8)))
        guard let ts = stored?.ts else { return XCTFail("expected a stored timestamp") }
        XCTAssertTrue(ts >= before && ts <= after,
                      "expected \(ts) to fall within [\(before), \(after)] -- the moment of observation")
    }

    /// The outgoing-path twin of
    /// HandleFrameTests.testIncomingClipStoresTheLiteralKnownHashForAPinnedVector:
    /// comparing against `sha256Hex(Data("hi".utf8))` itself (as the test
    /// above does) cannot catch `wireAgent`'s `onChange` hashing the WRONG
    /// bytes -- it would still pass as long as it did so consistently with
    /// sha256Hex's own behavior. This pins the LITERAL, independently
    /// verified digest instead. See fixtures/hashes.json, read by both
    /// suites.
    func testAGenuineLocalChangeStoresTheLiteralKnownHashForAPinnedVector() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        let channel = Channel(config: config(), log: tempLog())
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let clipStateStore = tempClipStateStore()

        wireAgent(channel: channel, watcher: watcher, pasteboard: pasteboard, status: status,
                  log: tempLog(), clipStateStore: clipStateStore, clipStateAnnouncement: ClipStateAnnouncement())

        watcher.poll() // baseline
        pasteboard.set("hi")
        watcher.poll()

        XCTAssertEqual(clipStateStore.load()?.sha256,
                       "8f434346648f6b96df89dda901c5176b10a6d83961dd3c1ac88b59b2dc327aa4")
    }

    /// The last link of the outbound image path: `PasteboardWatcher` emits
    /// `(kind, body, observedAt)` and `handleLocalChange` turns a kind into a
    /// frame, but nothing until here proves `wireAgent` passes the OBSERVED
    /// kind between them rather than a hardcoded `.text`. A closure that
    /// dropped it would compile clean and pass both of those suites, and the
    /// damage would only appear on the wire, where `channel.send` is not
    /// observable at all -- so the store is where it is caught: a hardcoded
    /// `.text` records a PNG's digest as text, which the peer's
    /// `decode_clip_state` accepts and believes.
    func testALocalImageChangeReachesTheStoreAsAnImageThroughTheWiring() {
        let png = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        let clipStateStore = tempClipStateStore()

        wireAgent(channel: Channel(config: config(), log: tempLog()), watcher: watcher,
                  pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                  log: tempLog(), clipStateStore: clipStateStore,
                  clipStateAnnouncement: ClipStateAnnouncement())

        watcher.poll() // baseline
        pasteboard.setImage(png)
        watcher.poll()

        XCTAssertEqual(clipStateStore.load()?.kind, .image,
                       "the kind the watcher observed must survive the wiring")
        XCTAssertEqual(clipStateStore.load()?.sha256, sha256Hex(png))
    }

    /// The property that makes the whole feature work across the case it
    /// exists for: reconnects happen on every Mac sleep/wake cycle, not just
    /// reboots, and `wireAgent` is wired exactly once at process start, so
    /// whatever tracks "have we announced yet" must be reset on EVERY new
    /// connection, not just consulted once. A gate that resets would look
    /// identical to one that doesn't on a test that only checks the FIRST
    /// connection -- this drives a second `.clipboardPending` transition and
    /// checks the gate re-arms, using the SAME `ClipStateAnnouncement`
    /// instance `wireAgent` was given, so its state is directly observable
    /// without needing a live channel.send to prove anything happened.
    func testClipStateAnnouncementResetsOnEveryNewConnectionNotOnlyTheFirst() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        let channel = Channel(config: config(), log: tempLog())
        let status = AgentStatus(pid: 1, url: tempStatusURL())
        let announcement = ClipStateAnnouncement()

        wireAgent(channel: channel, watcher: watcher, pasteboard: pasteboard, status: status,
                  log: tempLog(), clipStateStore: tempClipStateStore(), clipStateAnnouncement: announcement)

        channel.onStateChange?(.clipboardPending, nil)
        channel.onFrame?(Frame(type: .hello, payload: ProtocolConstants.helloPayload))
        XCTAssertTrue(announcement.sent, "a matched hello on the first connection must claim the gate")

        XCTAssertNotNil(announcement.announced,
                        "the announced pair must be recorded, or the .clipState case has nothing " +
                        "to reconcile against when the store cannot be read back")

        channel.onStateChange?(.clipboardPending, nil)
        XCTAssertFalse(announcement.sent,
                       "a NEW connection (the ordinary sleep/wake reconnect) must re-arm the gate -- " +
                       "otherwise the announcement fires on the process's first connection ever and " +
                       "never again, which defeats the entire feature")
        XCTAssertNil(announcement.announced,
                     "and it must forget what the PREVIOUS connection announced: by now that pair " +
                     "describes an older reading of the pasteboard than the reconciliation this " +
                     "connection is about to perform for itself")
    }

    /// The local-change save site, reached the way production reaches it --
    /// through the closure `wireAgent` actually installed. The site itself
    /// moved into `handleLocalChange` with Task 13 and is pinned there too
    /// (`testALocalChangeStillSendsWhenTheStoreCannotBeSaved`), so what this
    /// test uniquely proves is the WIRING: that the closure routes through
    /// something that logs rather than swallowing, which is what it did
    /// inline before.
    ///
    /// Every one of the PC agent's own `save_clip_state` calls already logs
    /// `could not persist clip state: %r`; the Swift ones were all bare
    /// `try?`. The asymmetry matters because the silent side is the one whose
    /// disk failure is the precondition for a store-goes-stale clobber.
    func testALocalChangeLogsAFailedSave() throws {
        let logPath = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-wiring-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log").path
        let log = Log(path: logPath)
        let blockingFile = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-wiring-test-blocker-\(UUID().uuidString)")
        try Data("occupying this name".utf8).write(to: blockingFile)
        // A plain file where the store needs a directory, so `save()`'s own
        // first step throws for real instead of being mocked.
        let store = ClipStateStore(path: blockingFile.appendingPathComponent("clip-state.json").path)
        XCTAssertThrowsError(try store.save(ClipState(sha256: "aa", ts: 1, kind: .text)),
                             "test setup must actually force a save failure, or this test proves nothing")

        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        wireAgent(channel: Channel(config: config(), log: tempLog()), watcher: watcher,
                  pasteboard: pasteboard, status: AgentStatus(pid: 1, url: tempStatusURL()),
                  log: log, clipStateStore: store, clipStateAnnouncement: ClipStateAnnouncement())

        // Drives the closure `wireAgent` actually installed, rather than a
        // stand-in -- the point is that THIS wiring logs, not that some
        // equivalent code would.
        watcher.onChange?(.text, Data("a local copy".utf8), 5)

        log.flush()
        let contents = (try? String(contentsOfFile: logPath, encoding: .utf8)) ?? ""
        let messages = contents.split(separator: "\n").map {
            String($0.drop(while: { $0 != " " }).dropFirst())
        }
        XCTAssertEqual(messages.filter { $0.hasPrefix("could not persist clip state: ") }.count, 1,
                       "an observed local change whose state cannot be persisted must not be silent")
    }
}
