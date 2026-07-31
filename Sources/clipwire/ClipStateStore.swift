// Sources/clipwire/ClipStateStore.swift
import Foundation

// See StatusFile.swift's StatusConstants for why this is `static let` on an
// enum rather than a bare top-level `let`.
enum ClipStateStoreConstants {
    static let defaultPath = "~/.local/state/clipwire/clip-state.json"
}

/// Persists the last known `(sha256, ts)` pair to disk, so a process that
/// starts fresh -- the PC agent on every connection (spawned new by sshd
/// each time), either side after a crash or a Mac sleep/wake cycle -- can
/// still answer "how old is what I hold" for content it never personally
/// observed. Written on every locally-observed clipboard change and on
/// every applied remote clip (both call sites are later work); read once,
/// at startup, by whoever calls `resolveStartupState` below with the
/// result.
struct ClipStateStore {
    let url: URL

    init(path: String) {
        url = URL(fileURLWithPath: expandTilde(path))
    }

    /// `nil` covers three different failure reasons identically, on
    /// purpose: no file has ever been written, the file exists but cannot
    /// be read (permissions, or -- as this suite covers directly -- not a
    /// regular file at all), and the file exists and is readable but does
    /// not decode as a `ClipState` (a write torn by a crash mid-rename, or
    /// anything else malformed). `resolveStartupState`'s "nothing stored"
    /// branch already treats all three identically, so collapsing them
    /// here rather than throwing keeps that caller from having to
    /// distinguish reasons it would handle the same way regardless.
    /// Mirrors `Status.read`'s use of `try?` across the same two failure
    /// points in StatusFile.swift.
    func load() -> ClipState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(ClipState.self, from: data)
    }

    /// Throws rather than swallowing, unlike `load()` above -- so a future
    /// caller has something to log, rather than this type logging or
    /// printing on its own. Follows `Status.write(to:)`'s temp-file-and-
    /// replace pattern in StatusFile.swift exactly, for the same reason:
    /// `replaceItemAt` is atomic, so `load()` above -- possibly running in
    /// a different process -- can never observe a half-written file.
    func save(_ state: ClipState) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let tmp = url.appendingPathExtension("tmp")
        try JSONEncoder().encode(state).write(to: tmp)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }
}

/// The judgement that makes the wake flow work. `currentHash` is the
/// clipboard's hash *right now*, at startup; `stored` is whatever
/// `ClipStateStore.load()` last returned; `now` is the caller's clock.
///
/// A timestamp can come from three places, in precedence order: a local
/// change this process watched happen, a clip received from the peer
/// (carrying the peer's own timestamp, stored unchanged), and -- the case
/// this function exists for -- content that predates this process
/// entirely. The PC agent is spawned fresh by sshd on every connection, and
/// the Mac's channel drops on every sleep/wake cycle, so "predates this
/// process" is not an edge case; it is the ordinary shape of a clip copied
/// on one side while the other slept or was disconnected.
///
/// If the hash on disk matches what the clipboard holds now, the content
/// has not changed since it was last recorded, so the *stored* timestamp is
/// the real age of that content and is returned unchanged. Returning `now`
/// here instead would make every such clip look freshly copied, winning it
/// every reconciliation and clobbering the peer systematically. If the
/// hashes differ, or nothing was ever stored, the content changed (or
/// appeared) while nothing was watching, and only `now` is honest.
///
/// A `nil` currentHash (clipboard empty or unreadable right now) always
/// wins over whatever is on disk, regardless of what was previously stored:
/// `resolveFreshness` never compares timestamps when either side's hash is
/// `nil`, so the timestamp attached here is never actually read.
func resolveStartupState(currentHash: String?, stored: ClipState?, now: Double) -> ClipState {
    guard let currentHash else {
        return ClipState(sha256: nil, ts: now)
    }
    if let stored, stored.sha256 == currentHash {
        return ClipState(sha256: currentHash, ts: stored.ts)
    }
    return ClipState(sha256: currentHash, ts: now)
}
