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
    /// The command `attempt()` spawns. These default to exactly the production
    /// values, so `Commands.swift`'s `Channel(config:log:)` — the only
    /// construction site outside tests — keeps spawning `/usr/bin/ssh` with
    /// `sshArguments(for:host:)`, and nothing in production passes or depends
    /// on either of them.
    ///
    /// They exist for v3.1's pairing harness, which points this at
    /// `python3 agent/clipwire-agent.py` and runs the Swift and Python halves
    /// of this project against each other — something they had never once
    /// done, while every defect this project ever shipped was found by running
    /// it rather than by a test. Substituting the *command* and not the
    /// transport is the whole point: it keeps the real spawn, the real pipes,
    /// real EOF and real SIGPIPE, which is where this channel's actual defect
    /// history lives (see `ignoreSIGPIPE()` below, and `attempt()`'s labeled
    /// `outer:` loop).
    ///
    /// Three alternatives were rejected on the record, so a later tidying pass
    /// need not re-litigate them:
    ///
    /// - **An environment variable.** It would put "exec an arbitrary command"
    ///   into a tool that lives beside the owner's ssh keys, in a public
    ///   repository — a permanent surface to document and defend, bought for a
    ///   testing convenience. Nothing here reads the environment; the test
    ///   target injects through this initializer instead.
    /// - **A mock, or a protocol over `Process`/`Pipe`.** That substitutes a
    ///   drawing of the transport for the transport, and the transport is
    ///   precisely the part with the defect history.
    /// - **Storing already-built argv.** `host` varies per attempt — `run()`
    ///   flips to `config.fallbackIP` after a dial that never established — so
    ///   argv has to be a function of the host, evaluated inside `attempt()`,
    ///   not a value fixed at init.
    ///
    /// Internal rather than `private` only so a test can pin the defaults:
    /// once the command stopped being a literal at its call site, nothing else
    /// guaranteed that production still spawns ssh. `run()` never returns, and
    /// the pairing harness — the only caller of `attempt(host:)` outside
    /// `run()`, and the reason that method is no longer private — always
    /// injects, so neither of them observes what the defaults actually are.
    let executablePath: String
    let makeArguments: (Config, String) -> [String]
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

    init(config: Config, log: Log,
         executablePath: String = "/usr/bin/ssh",
         arguments: @escaping (Config, String) -> [String] = sshArguments(for:host:)) {
        self.config = config
        self.log = log
        self.executablePath = executablePath
        self.makeArguments = arguments
    }

    func send(_ frame: Frame) {
        send(frame, onSent: nil)
    }

    /// Same single writer as `send(_:)`, but reports back whether the frame
    /// actually reached the pipe -- `false` when no channel is established
    /// (`stdinPipe` is nil) or the write itself throws, `true` only once
    /// `write(contentsOf:)` has returned successfully. `onSent` runs on
    /// `writeQueue`, asynchronously, the same as the write itself.
    ///
    /// Exists because a caller (`runAgent()`'s `watcher.onChange`) used to
    /// call the plain `send(_:)` and then unconditionally record the clip
    /// as sent for `clipwire status` purposes, even on a connection that
    /// was never established -- reporting a clip that never left the
    /// machine as delivered. A two-overload split (rather than a single
    /// method with a defaulted parameter) keeps `send: channel.send`
    /// type-checking unchanged at its existing call site in `runAgent()`
    /// (`handleFrame`'s `send` parameter is `(Frame) -> Void`; a method
    /// referenced as a bare value ignores default arguments and would
    /// otherwise widen to `(Frame, ((Bool) -> Void)?) -> Void`).
    func send(_ frame: Frame, onSent: (@Sendable (Bool) -> Void)?) {
        writeQueue.async { [self] in
            guard let pipe = stdinPipe else {
                onSent?(false)
                return
            }
            do {
                try pipe.fileHandleForWriting.write(contentsOf: frame.encode())
                onSent?(true)
            } catch {
                log.line("write failed: \(error)")
                onSent?(false)
            }
        }
    }

    /// Closes this side's write end, which the far side sees as EOF on its
    /// stdin — the same thing a dropped ssh gives the agent, and the agent's
    /// own ordinary way out (`Agent.run()` logs "stdin closed, exiting" and
    /// returns 0).
    ///
    /// Nothing in production calls this, and nothing should: `run()`
    /// reconnects forever and the ssh process only ever dies on its own. It
    /// exists for v3.1's pairing harness, which spawns a real agent per
    /// connection — as sshd does, one process per connection — and therefore
    /// has to be able to END one. Without it every harness test would leave a
    /// python3 (and the gdbus child it forked) alive for the rest of the test
    /// binary's life, still polling a fake clipboard whose temp directory had
    /// been deleted underneath it.
    ///
    /// Goes through `writeQueue`, the one queue `stdinPipe` is ever touched
    /// from, so it cannot close the handle out from under an in-flight
    /// `send`; and `sync`, so a caller may rely on the close having happened
    /// by the time this returns.
    func hangUp() {
        writeQueue.sync { [self] in
            try? stdinPipe?.fileHandleForWriting.close()
            stdinPipe = nil
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
            // This loop is the main thread of a plain Swift executable, and
            // nothing here ever drains an autorelease pool — no AppKit, no
            // run loop, and `run()` by design never returns. Without this,
            // every `Process` and `Pipe` an attempt creates stays alive until
            // the agent exits, which for this agent means days. Measured at
            // ~5 KB per reconnect before this pool existed.
            let alive = autoreleasepool { attempt(host: host) }
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

    /// One connection: spawn the command, pump frames until it dies, and
    /// report whether it was ever established before it dropped.
    ///
    /// Internal rather than `private` for v3.1's pairing harness, which drives
    /// connections one at a time instead of through `run()`. `run()` is an
    /// infinite reconnect loop that never returns, so a test calling it would
    /// leave a thread respawning agents for the rest of the binary's life —
    /// and it offers no way to say "now reconnect", which is precisely the
    /// event freshness reconciliation exists for and the one the harness has
    /// to be able to stage. Production still reaches this only through
    /// `run()`, which is the only caller in `Sources/`.
    func attempt(host: String) -> Bool {
        // Cleared unconditionally, before anything else, including before
        // the early `return false` below if `task.run()` throws — so no
        // path through this function can ever leave a previous attempt's
        // timestamp behind for `run()` to misread as this attempt's uptime.
        establishedAt = nil

        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()

        // Whether `task.run()` got far enough to hand the child ends to a
        // real child process. It decides which descriptors the `defer` below
        // may touch, and getting it wrong is worse than the leak it replaces
        // — see there.
        var spawned = false

        // Three `Pipe()`s per connection is six descriptors, and until this
        // `defer` existed not one of them was ever closed: the only `close()`
        // in this file lives in `hangUp()`, which production never calls.
        // `Pipe`'s handles carry `closeOnDealloc`, so the intent was that ARC
        // would do it — it did not, and the shipped agent proved it over 3
        // days and 1168 reconnects, ending with 2556 open pipes and its
        // descriptor table full solid (fds 0…2559 of a 2560 ceiling). Past
        // saturation `Pipe()` cannot allocate, `Process.run()` dup2's fd −1,
        // and every reconnect fails with `NSPOSIXErrorDomain Code=9` forever:
        // the agent cannot recover on its own, it has to be restarted.
        //
        // Deliberately a `defer` in this synchronous scope rather than a
        // wrapper type with a `deinit`. The attempt already has an owner —
        // this function's scope — and lifetime-based release is precisely the
        // mechanism that just failed; swapping one implicit `deinit` for
        // another would reproduce the bug with more ceremony.
        //
        // Only the three PARENT ends are unconditionally ours. After a
        // successful spawn `Process` has already closed the parent's copies
        // of the child ends, and the `FileHandle` objects go on holding those
        // stale numbers — closing them again would close whatever unrelated
        // descriptor has since reused the number, in the worst case a live
        // pipe belonging to the next attempt. Verified directly rather than
        // assumed: after `run()` succeeds, `fcntl(F_GETFD)` on all three
        // child ends returns EBADF while all three parent ends stay valid;
        // after `run()` throws, all six are still open. Hence `!spawned`.
        //
        // stderr's read end is missing here on purpose: it is closed by its
        // own readability handler on EOF (see below), because closing it from
        // this thread can crash the process.
        defer {
            // Through `writeQueue`, the one queue `stdinPipe` is ever touched
            // from, for the same reason `hangUp()` does it: a concurrent
            // `send()` must not have the handle closed out from under its
            // `write(contentsOf:)`. Niling `stdinPipe` inside the same block
            // is what makes that safe in the other direction too — a `send`
            // enqueued after this close finds nil and reports `false`,
            // instead of writing into a closed (or, worse, recycled)
            // descriptor. No deadlock: the run thread is never `writeQueue`
            // itself, and an in-flight write cannot block forever because
            // this runs after `waitUntilExit()`, so the reader is already
            // gone and the kernel fails the write with EPIPE — which
            // `ignoreSIGPIPE()` plus `send()`'s `catch` already handle.
            // Closing twice is fine, and the pairing harness does exactly
            // that: it calls `hangUp()` to end a connection, which closes
            // this same handle and nils `stdinPipe`, and then this `defer`
            // closes it again on the way out. Unlike the child ends above,
            // that is safe, because it is the same `FileHandle` object doing
            // both closes — `Pipe` caches its handles (verified: two property
            // accesses are identical objects) so the second `close()` finds
            // the handle already closed and does nothing. Checked against the
            // exact hazard rather than assumed: with the freed number
            // deliberately recycled by another `open()` in between, the
            // second close leaves that descriptor untouched. The child ends
            // are the opposite case only because `Process` closes them
            // behind the handle's back, leaving it holding a number it no
            // longer owns and no way to know it.
            writeQueue.sync { [self] in
                try? stdin.fileHandleForWriting.close()
                stdinPipe = nil
            }
            try? stdout.fileHandleForReading.close()
            if !spawned {
                try? stdin.fileHandleForReading.close()
                try? stdout.fileHandleForWriting.close()
                // Also the EOF that retires the readability handler below:
                // with no child to close its fd 2, this is the last writer,
                // so nothing else would ever wake that source.
                try? stderr.fileHandleForWriting.close()
            }
        }

        let task = Process()
        task.executableURL = URL(fileURLWithPath: executablePath)
        task.arguments = makeArguments(config, host)
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
        // Retires itself on EOF, from inside its own last invocation, and
        // that placement is load-bearing. The obvious alternative — the run
        // thread setting `readabilityHandler = nil` and closing the handle
        // after `waitUntilExit()` — crashes the agent: cancelling a dispatch
        // source is asynchronous, so an invocation already in flight goes on
        // to call `availableData` on the descriptor the run thread just
        // closed, and `FileHandle` answers that with an ObjC
        // `NSFileHandleOperationException`, which Swift cannot catch. The
        // window is microseconds wide and this loop reconnects a few hundred
        // times a day. The same race has a second, quieter outcome: a late
        // invocation reading a descriptor number already recycled by the NEXT
        // connection's stderr, stealing its lines into this one's log.
        //
        // Closing from the final invocation has neither problem — the source
        // is serial, so nothing else is running inside it, and once the
        // handler has cleared itself it cannot fire again. EOF is guaranteed
        // on both paths: after a successful spawn the child's death closes
        // the last writer (`Process` having already closed the parent's copy),
        // and on the throw path the `defer` above closes it explicitly.
        //
        // The empty read is also the only stop condition this source has —
        // before, an EOF that nobody acted on left it re-arming and firing on
        // a dead descriptor for the rest of the attempt.
        stderr.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            if data.isEmpty {
                handle.readabilityHandler = nil
                try? handle.close()
                return
            }
            guard let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") where !line.isEmpty {
                log.line("remote: \(line)")
            }
        }

        do { try task.run() } catch {
            log.line("failed to start ssh: \(error)")
            onStateChange?(.down, "cannot start ssh")
            return false
        }

        spawned = true
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
            // The pool in `run()` drains once per reconnect, which is no help
            // to a connection that stays up for hours: this loop never leaves
            // `attempt()`, so without a pool of its own every `availableData`
            // adds an `NSData` that nothing will release until the channel
            // finally drops. Draining per read is safe — `chunk` is a Swift
            // `Data`, which owns its bytes past the pool's scope.
            let chunk = autoreleasepool { stdout.fileHandleForReading.availableData }
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
        // Both the handler teardown and `stdinPipe = nil` used to happen
        // here; they now live in this function's `defer` — the handler
        // retires itself on EOF, and the pipe is closed and niled together
        // under `writeQueue`. See the two comments above for why neither
        // belongs on this thread.
        self.process = nil
        log.line("ssh exited with status \(task.terminationStatus)")
        return established
    }
}
