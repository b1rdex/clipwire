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
                  status: status, log: tempLog())

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
        watcher.onChange = { data in
            onChangeFireCount += 1
            wiredOnChange(data)
        }

        watcher.poll() // establishes the baseline changeCount, emits nothing

        // A clip arrives from the peer, through the wired channel.onFrame.
        // If wireAgent correctly bound noteWrittenLocally to THIS watcher,
        // handleFrame arms its suppression before writing "hello" to the
        // shared pasteboard -- which also bumps its changeCount, exactly as
        // a real incoming write bumps NSPasteboard.general's.
        channel.onFrame?(Frame(type: .clip, payload: Data("hello".utf8)))
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
}
