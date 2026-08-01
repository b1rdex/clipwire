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
/// concept too. Every call site today only ever arms and checks `.text` --
/// `PasteboardWatcher`'s own read path is still text-only (see its class doc
/// comment) -- so `kind` is currently a no-op in practice, not yet a live
/// discriminator; it is here so a later task that starts syncing local image
/// changes only has to pass `.image` through, not touch this comparison.
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
