// Sources/clipwire/Commands.swift
import Foundation

func runAgent() -> Int32 {
    let log = Log(path: AgentPaths.logPath)
    let config: Config
    do {
        config = try Config.load()
    } catch {
        log.line("\(error)")
        // `line(_:)` only enqueues the write; without this it races the
        // process actually exiting and reliably loses (confirmed
        // empirically -- see Log.flush()'s doc comment), leaving nothing
        // in the log to explain a dead agent.
        log.flush()
        // Exit 0 on a config error: the plist uses KeepAlive={SuccessfulExit: false},
        // so a non-zero exit here would give an eternal restart loop.
        return 0
    }

    let status = AgentStatus(pid: ProcessInfo.processInfo.processIdentifier, url: AgentPaths.statusURL)
    status.writeInitial()

    let channel = Channel(config: config, log: log)
    // The log is what makes a dropped image visible: a TIFF that fails to
    // convert reads back as nothing, which is indistinguishable at every
    // call site from an empty pasteboard. This is the only construction site
    // that passes one; every test constructs a `SystemPasteboard` without.
    let systemPasteboard = SystemPasteboard(log: log)
    let watcher = PasteboardWatcher(
        pasteboard: systemPasteboard,
        pollInterval: Double(config.macPollIntervalMs) / 1000.0,
        log: log)
    let clipStateStore = ClipStateStore(path: ClipStateStoreConstants.defaultPath)
    let clipStateAnnouncement = ClipStateAnnouncement()

    wireAgent(channel: channel, watcher: watcher, pasteboard: systemPasteboard, status: status, log: log,
              clipStateStore: clipStateStore, clipStateAnnouncement: clipStateAnnouncement)

    watcher.start()

    let heartbeat = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    heartbeat.schedule(deadline: .now(), repeating: 5)
    heartbeat.setEventHandler {
        status.tickHeartbeat(reconnects: channel.reconnects)
    }
    heartbeat.resume()

    channel.run()   // never returns
    return 0
}

func printStatus() -> Int32 {
    switch Status.read(from: AgentPaths.statusURL) {
    case .healthy(let status):
        print("up — \(status.reconnects) reconnects")
        if let sent = status.lastSentAt { print("last sent:     \(sent)") }
        if let received = status.lastReceivedAt { print("last received: \(received)") }
        return 0
    case .unhealthy(let status, let reason):
        // `Status.read` falls back to the state's own name when the agent
        // recorded no specific reason, which would print it twice. That was
        // unreachable until `clipboard-pending` became a state the agent
        // actually reports -- it is now the normal state for the whole window
        // between a PC reboot and someone logging in to GNOME.
        if reason == status.state.rawValue {
            print(status.state.rawValue)
        } else {
            print("\(status.state.rawValue) — \(reason)")
        }
        return 1
    case .agentDead(let why):
        print("agent dead — \(why)")
        return 1
    }
}

func initConfig() -> Int32 {
    let url = Config.defaultURL
    guard !FileManager.default.fileExists(atPath: url.path) else {
        print("config already exists at \(url.path) — not overwriting")
        return 1
    }
    let example = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("config.example.json")
    do {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(contentsOf: example).write(to: url)
        print("wrote \(url.path) — edit it, then run `clipwire install`")
        return 0
    } catch {
        print("could not write config: \(error)")
        return 1
    }
}

func install() -> Int32 {
    let config: Config
    do {
        config = try Config.load()
    } catch {
        // Prints the real ConfigError (missing vs invalid, and why) rather
        // than swallowing it behind one generic line -- consistent with
        // the rest of this file's "failures are visible" intent.
        print("no usable config: \(error)")
        return 1
    }
    let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("agent/clipwire-agent.py")
    let target = config.remoteAgentPath
    let remote = "\(config.user)@\(config.host)"

    func ssh(_ command: String) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-i", expandTilde(config.identityFile),
                          "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                          "-o", "ConnectTimeout=5", remote, command]
        try? task.run()
        task.waitUntilExit()
        return task.terminationStatus
    }

    guard ssh("mkdir -p $(dirname \(target))") == 0 else {
        print("could not create the remote directory")
        return 1
    }

    let scp = Process()
    scp.executableURL = URL(fileURLWithPath: "/usr/bin/scp")
    scp.arguments = ["-i", expandTilde(config.identityFile),
                     "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     "-o", "ConnectTimeout=5",
                     source.path, "\(remote):\(target)"]
    try? scp.run()
    scp.waitUntilExit()
    guard scp.terminationStatus == 0 else {
        print("copy failed")
        return 1
    }

    guard ssh("chmod +x \(target)") == 0 else {
        print("could not set the executable bit on \(target)")
        return 1
    }
    let selftest = ssh("\(target) --selftest")
    print(selftest == 0 ? "installed and verified" : "installed, but --selftest failed")
    return selftest
}
