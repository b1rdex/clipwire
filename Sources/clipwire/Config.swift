// Sources/clipwire/Config.swift
import Foundation

func expandTilde(_ path: String) -> String {
    guard path == "~" || path.hasPrefix("~/") else { return path }
    return NSHomeDirectory() + String(path.dropFirst(1))
}

enum ConfigError: Error, CustomStringConvertible {
    case missing(URL)
    case invalid(String)

    var description: String {
        switch self {
        case .missing(let url):
            return "no config at \(url.path) — run `clipwire init` to create one"
        case .invalid(let why):
            return "invalid config: \(why)"
        }
    }
}

struct Config: Codable {
    let host: String
    let fallbackIP: String?
    let user: String
    let identityFile: String
    let remoteAgentPath: String
    let macPollIntervalMs: Int
    let pcFallbackPollIntervalMs: Int
    let maxFrameBytes: Int

    enum CodingKeys: String, CodingKey {
        case host
        case fallbackIP = "fallback_ip"
        case user
        case identityFile = "identity_file"
        case remoteAgentPath = "remote_agent_path"
        case macPollIntervalMs = "mac_poll_interval_ms"
        case pcFallbackPollIntervalMs = "pc_fallback_poll_interval_ms"
        case maxFrameBytes = "max_frame_bytes"
    }

    static var defaultURL: URL {
        URL(fileURLWithPath: expandTilde("~/.config/clipwire/config.json"))
    }

    static func load(from url: URL = Config.defaultURL) throws -> Config {
        guard FileManager.default.fileExists(atPath: url.path) else {
            throw ConfigError.missing(url)
        }
        let data: Data
        do {
            data = try Data(contentsOf: url)
        } catch {
            throw ConfigError.invalid("cannot read \(url.path): \(error.localizedDescription)")
        }
        let config: Config
        do {
            config = try JSONDecoder().decode(Config.self, from: data)
        } catch {
            throw ConfigError.invalid("cannot parse \(url.path): \(error)")
        }
        try config.validate()
        return config
    }

    func validate() throws {
        if host.isEmpty { throw ConfigError.invalid("host must not be empty") }
        if user.isEmpty { throw ConfigError.invalid("user must not be empty") }
        if macPollIntervalMs <= 0 || pcFallbackPollIntervalMs <= 0 {
            throw ConfigError.invalid("poll intervals must be positive")
        }
        if maxFrameBytes <= 0 || maxFrameBytes > FrameConstants.maxPayloadBytes {
            throw ConfigError.invalid("max_frame_bytes must be between 1 and \(FrameConstants.maxPayloadBytes)")
        }
    }
}
