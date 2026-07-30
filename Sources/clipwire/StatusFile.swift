// Sources/clipwire/StatusFile.swift
import Foundation
import Darwin

// See Frame.swift for why this is `static let` on an enum rather than a bare
// top-level `let`: this target is an executableTarget, and a top-level `let`
// in whichever file the compiler treats as the implicit main file becomes a
// sequential statement of a `main()` the test target never calls, reading as
// zero-initialized memory (observed empirically in Task 1). StatusFile.swift
// is not the only file in this target, so that hazard does not currently
// reach here, but namespacing under an enum keeps every module-wide constant
// safely swift_once-guarded regardless of which file ends up main, and stays
// consistent with `FrameConstants`.
enum StatusConstants {
    static let heartbeatStaleAfter: TimeInterval = 15
}

enum ChannelState: String, Codable {
    case up
    case clipboardPending = "clipboard-pending"
    case down
}

struct Status: Codable {
    var state: ChannelState
    var reason: String?
    var heartbeat: Date
    var pid: Int32
    var lastSentAt: Date?
    var lastReceivedAt: Date?
    var reconnects: Int

    func write(to url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let tmp = url.appendingPathExtension("tmp")
        try encoder.encode(self).write(to: tmp)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }

    static func read(from url: URL, now: Date = Date(),
                     pidIsAlive: (Int32) -> Bool = { kill($0, 0) == 0 }) -> StatusReport {
        guard FileManager.default.fileExists(atPath: url.path) else {
            return .agentDead("no status file at \(url.path) — the agent has never run")
        }
        guard let data = try? Data(contentsOf: url) else {
            return .agentDead("status file at \(url.path) exists but cannot be read")
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let status = try? decoder.decode(Status.self, from: data) else {
            return .agentDead("unreadable status file at \(url.path)")
        }
        if now.timeIntervalSince(status.heartbeat) > StatusConstants.heartbeatStaleAfter {
            return .agentDead("heartbeat is stale — the agent died without updating its status")
        }
        guard pidIsAlive(status.pid) else {
            return .agentDead("pid \(status.pid) is not running")
        }
        switch status.state {
        case .up: return .healthy(status)
        case .clipboardPending, .down:
            return .unhealthy(status, status.reason ?? status.state.rawValue)
        }
    }
}

enum StatusReport {
    case healthy(Status)
    case unhealthy(Status, String)
    case agentDead(String)
}
