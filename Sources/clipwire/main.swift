// Sources/clipwire/main.swift
import Foundation

// Namespaced rather than bare top-level `let`: main.swift is the file
// Swift treats as the literal entry point, so top-level statements here
// are correct and required (see the bottom of this file) -- but a bare
// top-level `let` is still a script-local binding, not a genuine shared
// constant, and every other constant in this target (FrameConstants,
// StatusConstants, ChannelConstants) is namespaced the same way for
// consistency and so nothing about its initialization depends on which
// file happens to be main.
enum AgentPaths {
    static let statusURL = URL(fileURLWithPath: expandTilde("~/.local/state/clipwire/status.json"))
    static let logPath = "~/.local/state/clipwire/clipwire.log"
}

/// Wires the channel, the pasteboard watcher, and status reporting together.
/// Pulled out of `runAgent()` so the last unpinned link in the echo chain --
/// that the closures actually bind `watcher.noteWrittenLocally` into the
/// frame handler, not something forgotten or a no-op -- is directly
/// exercisable in a test, with no live ssh or real pasteboard needed. See
/// `Tests/clipwireTests/AgentWiringTests.swift`.
///
/// `watcher.onChange` only calls `status.recordSent()` once `channel.send`
/// reports the frame actually reached the pipe (`onSent(true)`) -- a send
/// attempted before any channel is established (`stdinPipe` still nil) must
/// not make `clipwire status` claim a clip that never left the machine.
func wireAgent(
    channel: Channel, watcher: PasteboardWatcher, pasteboard: PasteboardReading & PasteboardWriting,
    status: AgentStatus, log: Log, clipStateStore: ClipStateStore, clipStateAnnouncement: ClipStateAnnouncement
) {
    // The kind the watcher observed is passed straight through, never
    // re-derived: it comes from the same `pasteboard.read()` pair the body
    // did (see `PasteboardWatcher.onChange`). Dropping it here would compile
    // clean and pass both the watcher's and `handleLocalChange`'s own suites,
    // and the damage would surface only on the wire, where `channel.send` is
    // not observable at all -- so `AgentWiringTests` catches it through the
    // store instead.
    //
    // `status.recordSent()` stays here rather than moving into
    // `handleLocalChange`: it is only called once `channel.send` reports the
    // frame actually reached the pipe (`onSent(true)`), because a send
    // attempted before any channel is established (`stdinPipe` still nil)
    // must not make `clipwire status` claim a clip that never left the
    // machine. That is this wiring's business, not the handler's.
    watcher.onChange = { kind, payload, observedAt in
        handleLocalChange(
            kind: kind, body: payload, observedAt: observedAt,
            send: { frame in
                channel.send(frame, onSent: { sent in
                    if sent { status.recordSent() }
                })
            },
            clipStateStore: clipStateStore, log: log)
    }

    channel.onFrame = { frame in
        handleFrame(frame, send: channel.send, noteWrittenLocally: watcher.noteWrittenLocally,
                    pasteboard: pasteboard, status: status, log: log,
                    clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)
    }

    channel.onStateChange = { state, reason in
        status.applyChannelState(state, reason)
        // Channel raises `.clipboardPending` exactly once per established
        // connection attempt (see `Channel.attempt()`'s `established`
        // guard) -- the reset point that lets the one-shot announcement
        // re-arm on every reconnect rather than firing only on the
        // process's first connection ever.
        if state == .clipboardPending {
            clipStateAnnouncement.reset()
        }
    }
}

switch Array(CommandLine.arguments.dropFirst()).first {
case "run", nil: exit(runAgent())
case "status":   exit(printStatus())
case "init":     exit(initConfig())
case "install":  exit(install())
default:
    print("usage: clipwire [run|status|init|install]")
    exit(2)
}
