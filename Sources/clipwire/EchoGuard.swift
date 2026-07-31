// Sources/clipwire/EchoGuard.swift
import CryptoKit
import Foundation

/// Suppresses the clipboard change caused by our own write, so a clip does not
/// ping-pong between the two machines forever.
struct EchoGuard {
    private var lastWritten: SHA256.Digest?

    mutating func noteWrittenLocally(_ payload: Data) {
        lastWritten = SHA256.hash(data: payload)
    }

    /// True when this local clipboard value should be sent to the peer.
    /// Consumes the suppression on the very next call after
    /// `noteWrittenLocally`, whichever payload is observed. Our own write
    /// produces exactly one clipboard-change event; if some other change is
    /// observed first instead, our write's event is already gone and the
    /// stored hash is worthless — keeping it armed would silently swallow a
    /// later, unrelated, deliberate re-copy of the same text.
    mutating func shouldSend(_ payload: Data) -> Bool {
        guard let expected = lastWritten else { return true }
        lastWritten = nil
        return SHA256.hash(data: payload) != expected
    }
}
