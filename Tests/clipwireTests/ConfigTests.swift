// Tests/clipwireTests/ConfigTests.swift
import XCTest
@testable import clipwire

final class ConfigTests: XCTestCase {
    private func write(_ json: String) throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-test-\(UUID().uuidString).json")
        try json.write(to: url, atomically: true, encoding: .utf8)
        return url
    }

    func testLoadsValidConfig() throws {
        let url = try write("""
        {"host":"pc","user":"me","identity_file":"~/.ssh/id_ed25519",
         "remote_agent_path":"~/.local/share/clipwire/clipwire-agent.py",
         "mac_poll_interval_ms":400,"pc_fallback_poll_interval_ms":1000,
         "max_frame_bytes":4194304}
        """)
        let config = try Config.load(from: url)
        XCTAssertEqual(config.host, "pc")
        XCTAssertEqual(config.macPollIntervalMs, 400)
        XCTAssertNil(config.fallbackIP)
    }

    func testMissingFileThrowsMissingNotInvalid() throws {
        let url = URL(fileURLWithPath: "/nonexistent/clipwire/config.json")
        XCTAssertThrowsError(try Config.load(from: url)) { error in
            guard case ConfigError.missing = error else {
                return XCTFail("expected .missing, got \(error)")
            }
        }
    }

    func testRejectsFrameCapAboveProtocolMaximum() throws {
        let url = try write("""
        {"host":"pc","user":"me","identity_file":"k","remote_agent_path":"a",
         "mac_poll_interval_ms":400,"pc_fallback_poll_interval_ms":1000,
         "max_frame_bytes":99999999}
        """)
        XCTAssertThrowsError(try Config.load(from: url))
    }

    func testRejectsNonPositivePollInterval() throws {
        let url = try write("""
        {"host":"pc","user":"me","identity_file":"k","remote_agent_path":"a",
         "mac_poll_interval_ms":0,"pc_fallback_poll_interval_ms":1000,
         "max_frame_bytes":4194304}
        """)
        XCTAssertThrowsError(try Config.load(from: url))
    }

    func testExpandsTilde() {
        let home = NSHomeDirectory()
        XCTAssertEqual(expandTilde("~/x"), home + "/x")
        XCTAssertEqual(expandTilde("/abs/x"), "/abs/x")
        XCTAssertEqual(expandTilde("~"), home, "a bare ~ must expand to the home directory itself")
    }

    // Not in the brief. The brief's own self-review checklist asks: "does an
    // unreadable or malformed one produce something a user can act on?" The
    // verbatim Step 3 `load()` answers no — a malformed file lets a raw
    // JSONDecoder error (a Swift DecodingError reflection dump) propagate
    // uncaught, instead of the ConfigError the rest of this type is built
    // around. Pinning that gap here before closing it, the same way Task 1
    // pinned the FrameConstants zero-init hazard before fixing it.
    func testMalformedJSONThrowsInvalidNotRawDecodingError() throws {
        let url = try write("this is not json")
        XCTAssertThrowsError(try Config.load(from: url)) { error in
            guard case ConfigError.invalid = error else {
                return XCTFail("expected .invalid, got \(error)")
            }
        }
    }
}
