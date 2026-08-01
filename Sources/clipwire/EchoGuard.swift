// Sources/clipwire/EchoGuard.swift
import CryptoKit
import Foundation

/// Suppresses the clipboard change caused by our own write, so a clip does not
/// ping-pong between the two machines forever.
///
/// Identity is `(kind, hash)`, not the hash alone: comparing the payload's
/// hash was already the rule here before this task -- what was missing was
/// kind itself. Adding it is what keeps this side's notion of "is this the
/// same content" the same SHAPE as the PC agent's `_last_seen`, which this
/// same task also turns into a `(kind, hash)` pair: two independently
/// maintained implementations of clipboard identity are exactly the kind of
/// drift between the two sides of this project that has already caused real
/// bugs twice, and matching their shape here closes that off for this
/// concept too. `kind` was a no-op in practice when it was added, since every
/// call site then armed and checked `.text`: `PasteboardWatcher`'s read path
/// was text-only, and no image ever reached this comparison. Task 13 made it
/// a live discriminator by teaching that path to emit images and
/// `handleFrame`'s `.imageClip` case to apply them, and it needed no change
/// here -- passing `.image` through was the whole of it, which is what
/// adding the field early bought.
///
/// It is load-bearing now rather than merely populated: an image applied from
/// the peer is armed as `(.image, digest)` and observed as `(.image, digest)`
/// a poll later, and a mismatch on either half sends it straight back to the
/// machine it came from -- which applies any incoming clip unconditionally.
struct EchoGuard {
    private var lastWritten: (kind: ClipKind, digest: SHA256.Digest)?

    mutating func noteWrittenLocally(kind: ClipKind, payload: Data) {
        lastWritten = (kind, SHA256.hash(data: payload))
    }

    /// True when this local clipboard value should be sent to the peer.
    /// Consumes the suppression on the very next call after
    /// `noteWrittenLocally`, whichever kind and payload are observed. Our own
    /// write produces exactly one clipboard-change event; if some other
    /// change is observed first instead, our write's event is already gone
    /// and the stored (kind, digest) is worthless — keeping it armed would
    /// silently swallow a later, unrelated, deliberate re-copy of the same
    /// content.
    mutating func shouldSend(kind: ClipKind, payload: Data) -> Bool {
        guard let expected = lastWritten else { return true }
        lastWritten = nil
        return kind != expected.kind || SHA256.hash(data: payload) != expected.digest
    }
}
