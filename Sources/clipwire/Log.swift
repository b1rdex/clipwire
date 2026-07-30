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

    private func rotateIfNeeded() {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attributes?[.size] as? Int) ?? 0
        guard size >= rotateAt else { return }
        let previous = url.appendingPathExtension("1")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
    }
}
