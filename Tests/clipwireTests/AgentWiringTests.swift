// Tests/clipwireTests/AgentWiringTests.swift
//
// Final review: runAgent()'s closure wiring was the last unpinned link in
// the echo chain. `handleFrame` (HandleFrameTests.swift) and
// `PasteboardWatcher` (PasteboardTests.swift) each prove their half of the
// echo-suppression contract in isolation, but nothing proved that
// `wireAgent` (extracted from `runAgent()` in main.swift for exactly this
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
        watcher.onChange = { data, observedAt in
            onChangeFireCount += 1
            wiredOnChange(data, observedAt)
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

        channel.onStateChange?(.clipboardPending, nil)
        XCTAssertFalse(announcement.sent,
                       "a NEW connection (the ordinary sleep/wake reconnect) must re-arm the gate -- " +
                       "otherwise the announcement fires on the process's first connection ever and " +
                       "never again, which defeats the entire feature")
    }
}
