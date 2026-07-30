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

    /// True when this local clipboard value is a genuine user action rather
    /// than the echo of what we just wrote. Consumes the suppression, so the
    /// user re-copying the same text later still syncs.
    mutating func shouldSend(_ payload: Data) -> Bool {
        guard let expected = lastWritten, SHA256.hash(data: payload) == expected else {
            return true
        }
        lastWritten = nil
        return false
    }
}
