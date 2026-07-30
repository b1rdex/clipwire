// Sources/clipwire/Channel.swift
import Foundation

func sshArguments(for config: Config, host: String) -> [String] {
    [
        "-i", expandTilde(config.identityFile),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "\(config.user)@\(host)",
        config.remoteAgentPath,
    ]
}

enum ChannelConstants {
    /// A connection must stay up at least this long before its eventual drop
    /// is allowed to reset the reconnect backoff. Below this floor, a remote
    /// that completes its handshake (one frame is enough to count as
    /// "established") and then crash-loops would reset the ladder every
    /// cycle and get hammered at roughly 1-2s intervals forever, instead of
    /// backing off — see Task 14 fix round 1, finding 3.
    static let minimumUptimeForBackoffReset: TimeInterval = 30
}

/// Owns the ssh process and the framing on its pipes. All writes go through
/// `send`, which serialises them — two concurrent writers would interleave
/// frames and corrupt the stream.
///
/// `@unchecked Sendable`: `send(_:)` only ever touches `stdinPipe` from
/// `writeQueue`, a single serial queue, so pipe writes cannot interleave —
/// that is the actual safety property this type relies on, and the compiler
/// cannot see it structurally because `Process`/`Pipe` and the `onFrame`/
/// `onStateChange` closures are not themselves `Sendable`. `process` and
/// `stdinPipe` are otherwise written only from whichever thread calls `run()`
/// (never two threads at once, since `run()` is one sequential infinite
/// loop) and read from `writeQueue`; that cross-queue handoff relies on GCD's
/// own enqueue ordering rather than an explicit lock, the same convention
/// this codebase already uses for `PasteboardWatcher.timer` in
/// Pasteboard.swift.
final class Channel: @unchecked Sendable {
    var onFrame: ((Frame) -> Void)?
    var onStateChange: ((ChannelState, String?) -> Void)?

    private let config: Config
    private let log: Log
    private let writeQueue = DispatchQueue(label: "dev.b1rdex.clipwire.write")
    private var process: Process?
    private var stdinPipe: Pipe?
    private var backoff = Backoff()
    // Set (from the run-thread only, alongside `established` in `attempt()`)
    // the moment the first frame decodes; read back in `run()` to decide
    // whether the drop that just happened earned a backoff reset. Never
    // touched by `writeQueue` — simpler than `stdinPipe`, not just the same.
    private var establishedAt: Date?
    private(set) var reconnects = 0

    init(config: Config, log: Log) {
        self.config = config
        self.log = log
    }

    func send(_ frame: Frame) {
        writeQueue.async { [self] in
            guard let pipe = stdinPipe else { return }
            do {
                try pipe.fileHandleForWriting.write(contentsOf: frame.encode())
            } catch {
                log.line("write failed: \(error)")
            }
        }
    }

    /// SIGPIPE's default disposition terminates the process the instant a
    /// write lands on a pipe with no reader on the other end — exactly what
    /// `send()` can hit when it races `ssh` dying (`stdinPipe` is only niled
    /// out at the end of `attempt()`, after `waitUntilExit()` returns).
    /// Confirmed empirically (Task 14 fix round 1, finding 1) that this
    /// kills the whole agent before `send()`'s own `do/catch` ever runs.
    /// Ignoring it turns that same write into a normal, catchable `EPIPE`
    /// that `send()` already handles. This is a process-global disposition,
    /// not per-`Channel` state — calling it from `run()` is a pragmatic home
    /// for this task; the next task, which owns `main.swift`, may want to
    /// hoist it to the program entry point instead, so it also covers any
    /// other code path that ever writes to a pipe or socket, not just this
    /// one.
    static func ignoreSIGPIPE() {
        signal(SIGPIPE, SIG_IGN)
    }

    /// Whether a connection that just ended earned a backoff reset — pulled
    /// out as a pure, static, directly-testable function (rather than folded
    /// inline into `run()`) so the 30s floor can be pinned with injected
    /// `Date` values instead of an actual 30-second sleep in a test.
    static func shouldResetBackoff(
        establishedAt: Date?, now: Date,
        minimumUptime: TimeInterval = ChannelConstants.minimumUptimeForBackoffReset
    ) -> Bool {
        guard let establishedAt else { return false }
        return now.timeIntervalSince(establishedAt) >= minimumUptime
    }

    /// Dials, pumps until the channel dies, then waits and dials again. Never returns.
    func run() {
        Channel.ignoreSIGPIPE()
        var useFallback = false
        while true {
            let host = useFallback ? (config.fallbackIP ?? config.host) : config.host
            log.line("connecting to \(config.user)@\(host)")
            let alive = attempt(host: host)
            if alive {
                if Channel.shouldResetBackoff(establishedAt: establishedAt, now: Date()) {
                    backoff.reset()
                }
                onStateChange?(.down, "channel closed")
            } else {
                useFallback = config.fallbackIP != nil && !useFallback
            }
            reconnects += 1
            let delay = backoff.next()
            log.line("reconnecting in \(Int(delay))s")
            Thread.sleep(forTimeInterval: delay)
        }
    }

    /// Returns true when the channel was established before it dropped.
    private func attempt(host: String) -> Bool {
        // Cleared unconditionally, before anything else, including before
        // the early `return false` below if `task.run()` throws — so no
        // path through this function can ever leave a previous attempt's
        // timestamp behind for `run()` to misread as this attempt's uptime.
        establishedAt = nil

        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = sshArguments(for: config, host: host)
        task.standardInput = stdin
        task.standardOutput = stdout
        task.standardError = stderr

        // Captures `log` (a `Sendable` value) rather than `self` (a `Channel`,
        // which is not `Sendable`) — this closure only ever needed the
        // logger, and doing it this way sidesteps a Swift 6 strict-
        // concurrency compile error on capturing `self` in a `Pipe`
        // readability handler's `@Sendable` closure. It also means stderr
        // lines still get logged even if `Channel` itself were ever torn
        // down before the handler fires, instead of silently going missing.
        let log = self.log
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") where !line.isEmpty {
                log.line("remote: \(line)")
            }
        }

        do { try task.run() } catch {
            log.line("failed to start ssh: \(error)")
            onStateChange?(.down, "cannot start ssh")
            return false
        }

        self.process = task
        self.stdinPipe = stdin
        var established = false
        var buffer = Data()

        // Labeled so a decode error can drop straight out of both loops at
        // once (see the `catch` below) — Task 14 fix round 1, finding 2. A
        // plain `break` there only exited the inner decode loop; the outer
        // loop, whose `while task.isRunning` still reads true for a beat
        // after `terminate()` (confirmed empirically: SIGTERM is
        // asynchronous), would call `availableData` again and feed whatever
        // arrived next into the just-cleared buffer at an arbitrary offset —
        // resynchronising against a corrupt stream instead of dropping it.
        outer: while task.isRunning {
            let chunk = stdout.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while true {
                do {
                    guard let frame = try Frame.decode(from: &buffer) else { break }
                    if !established {
                        established = true
                        establishedAt = Date()
                        onStateChange?(.clipboardPending, nil)
                    }
                    onFrame?(frame)
                } catch {
                    log.line("protocol error: \(error) — dropping the channel")
                    task.terminate()
                    buffer.removeAll()
                    break outer
                }
            }
        }

        task.waitUntilExit()
        stderr.fileHandleForReading.readabilityHandler = nil
        self.stdinPipe = nil
        self.process = nil
        log.line("ssh exited with status \(task.terminationStatus)")
        return established
    }
}
