// Sources/clipwire/Log.swift
import Foundation

/// Appends to a file, rotating at 5 MiB and keeping one previous generation.
///
/// `Sendable` here is a real (compiler-checked), not `@unchecked`,
/// conformance: every stored property is `let` and of a `Sendable` type
/// (`URL`, `DispatchQueue`, `Int`), so the compiler can verify it structurally.
/// `line(_:)` still only ever touches file state from `queue`, its own serial
/// queue — `Sendable` just lets `Channel` (Task 14) capture a `Log` from
/// closures that themselves cross into `@Sendable` contexts (e.g. a Pipe's
/// `readabilityHandler`) without the compiler flagging the capture itself.
final class Log: Sendable {
    private let url: URL
    private let queue = DispatchQueue(label: "dev.b1rdex.clipwire.log")
    private let rotateAt = 5 * 1024 * 1024

    init(path: String) {
        url = URL(fileURLWithPath: expandTilde(path))
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    }

    func line(_ message: String) {
        queue.async { [self] in
            let stamp = ISO8601DateFormatter().string(from: Date())
            let entry = Data("\(stamp) \(message)\n".utf8)
            rotateIfNeeded()
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(entry)
                try? handle.close()
            } else {
                try? entry.write(to: url)
            }
            FileHandle.standardError.write(entry)
        }
    }

    /// Blocks until every write enqueued by an earlier `line(_:)` call has
    /// actually run. `line(_:)` only enqueues the write and returns
    /// immediately; a caller that logs a message and then calls `exit(_:)`
    /// cannot assume the write has happened, because `exit` tears the
    /// process down without waiting for anything still queued on `queue`.
    /// Confirmed empirically (Task 15): `main.swift`'s fatal-config-error
    /// path used to call `log.line("\(error)")` immediately before
    /// `return 0` (which reaches `exit(0)` at the top level), and the log
    /// file was reliably left empty across repeated runs -- a brand-new
    /// serial queue's first block needs GCD to schedule a worker thread,
    /// which loses the race against the two Swift statements between the
    /// enqueue and the process actually exiting.
    ///
    /// An empty `sync` block submitted to `queue` cannot return until every
    /// block already enqueued ahead of it has finished, because `queue` is
    /// serial (FIFO). Safe to call from any thread that is not itself
    /// already running on `queue` -- nothing in this codebase ever is,
    /// since `line(_:)` is the only thing that runs there and it never
    /// calls back into `Log`.
    func flush() {
        queue.sync {}
    }

    private func rotateIfNeeded() {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attributes?[.size] as? Int) ?? 0
        guard size >= rotateAt else { return }
        let previous = url.appendingPathExtension("1")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
    }
}
