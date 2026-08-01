// Sources/clipwire/LocalChange.swift
import Foundation

/// The one place a clip's kind becomes a frame: `.text` goes out as a
/// `.clip` frame carrying a `ClipPayload`, `.image` as an `.imageClip` frame
/// carrying an `ImagePayload`.
///
/// One function rather than the mapping written out at each of its two call
/// sites (`handleLocalChange` below, and `handleFrame`'s `.sendMine` branch).
/// Two inline copies is how the second one came to send text only: `main.swift`
/// carried the text codec in both places, so `.sendMine` had to decline an
/// image it had otherwise verified. Two copies of one rule in one target is the
/// same drift the two "over the text limit" lines and the three-word
/// reconciliation vocabulary already guard against across the two languages.
///
/// Throws only what `ImagePayload.encode` throws -- a non-finite `ts`, which
/// it rejects on encode as well as decode. No caller can currently supply
/// one: `handleLocalChange`'s comes from `Date().timeIntervalSince1970`, and
/// `.sendMine`'s from a `ClipState` that was JSON-decoded (a format with no
/// literal for NaN or infinity) or resolved from the same clock. Left
/// throwing rather than made unfailable anyway, so the finiteness rule keeps
/// living in one place -- the codec -- for both directions.
func outgoingClipFrame(kind: ClipKind, body: Data, ts: Double) throws -> Frame {
    switch kind {
    case .text:
        // `String(decoding:as:UTF8.self)` is lossy for bytes that are not
        // valid UTF-8, which is why `SystemPasteboard`'s text read goes
        // through `string(forType:)` rather than `data(forType:)` -- see
        // `PasteboardBackend`'s doc comment. By the time a body reaches this
        // function under `.text` it has already come back from that read, so
        // there is nothing here for the substitution to damage.
        return Frame(type: .clip,
                     payload: ClipPayload(ts: ts, text: String(decoding: body, as: UTF8.self)).encode())
    case .image:
        return Frame(type: .imageClip, payload: try ImagePayload.encode(ts: ts, png: body))
    }
}

/// Everything a clipboard change this agent OBSERVED locally does: record
/// what we now hold and how old it is, then put it on the wire.
///
/// Pulled out of `wireAgent`'s `watcher.onChange` closure for the same
/// reason `handleFrame` and `announceClipState` were pulled out of
/// `runAgent()`, and it matters more here than it looks: `channel.send` has
/// no test-observable hook whatsoever -- `Channel` is `final`, its
/// `stdinPipe` is private and assigned only inside `attempt()`, so a send
/// with no live ssh process reports `onSent(false)` and leaves nothing
/// behind. While this logic lived inside that closure, the entire outbound
/// path could have been wired to the text codec for both kinds with no test
/// in either suite able to see it. A `send` spy can now verify the exact
/// frame; see `Tests/clipwireTests/HandleFrameTests.swift`.
///
/// `send` is the one-argument shape rather than `Channel.send(_:onSent:)`:
/// the `onSent` callback exists to keep `clipwire status` from reporting a
/// clip that never left the machine, which is `wireAgent`'s business and not
/// this function's. `wireAgent` closes over it.
///
/// No size guard here, deliberately: `PasteboardWatcher.pollLocked` applies
/// the per-kind limit at the moment of observation, where the skip can be
/// logged next to the read that produced it. The `.sendMine` branch needs
/// its own copy because it reads the pasteboard independently; this path
/// does not.
func handleLocalChange(
    kind: ClipKind,
    body: Data,
    observedAt: Double,
    send: (Frame) -> Void,
    clipStateStore: ClipStateStore,
    log: Log
) {
    // `observedAt` -- the moment `PasteboardWatcher` actually read it, not
    // whenever this runs -- is the timestamp both for what we persist and for
    // what we send. The save happens regardless of whether the send below
    // ever reaches the peer (no channel yet, or the write fails): the store's
    // job is "what do we hold and how old is it", independent of delivery.
    //
    // `kind` is the one the watcher observed, threaded through from the same
    // `pasteboard.read()` pair the body came from. A hardcoded `.text` here
    // would announce a PNG's digest as text on the next reconnect, and the
    // peer believes it -- `decode_clip_state` accepts both kinds, so nothing
    // rejects it on arrival.
    persistClipState(ClipState(sha256: sha256Hex(body), ts: observedAt, kind: kind),
                     to: clipStateStore, log: log)
    let frame: Frame
    do {
        frame = try outgoingClipFrame(kind: kind, body: body, ts: observedAt)
    } catch {
        // Logged rather than swallowed by `try?`. A `try?` here is the exact
        // shape of the v2 defect this branch's whole design reacts to: a
        // clip that cannot be encoded is not sent, which is right, but a
        // silent drop is how a user concludes the tool is broken with
        // nothing anywhere to look at. The line is shared byte-for-byte with
        // the `.sendMine` branch's own catch -- one sentence per condition,
        // two sites.
        log.line("could not encode a clip for the peer: \(error)")
        return
    }
    send(frame)
}
