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

    /// Dials, pumps until the channel dies, then waits and dials again. Never returns.
    func run() {
        var useFallback = false
        while true {
            let host = useFallback ? (config.fallbackIP ?? config.host) : config.host
            log.line("connecting to \(config.user)@\(host)")
            let alive = attempt(host: host)
            if alive {
                backoff.reset()
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

        while task.isRunning {
            let chunk = stdout.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while true {
                do {
                    guard let frame = try Frame.decode(from: &buffer) else { break }
                    if !established {
                        established = true
                        onStateChange?(.clipboardPending, nil)
                    }
                    onFrame?(frame)
                } catch {
                    log.line("protocol error: \(error) — dropping the channel")
                    task.terminate()
                    buffer.removeAll()
                    break
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
