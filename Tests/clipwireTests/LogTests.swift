// Tests/clipwireTests/LogTests.swift
import XCTest
@testable import clipwire

final class LogTests: XCTestCase {
    private func tempLogPath() -> String {
        FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-log-test-\(UUID().uuidString)")
            .appendingPathComponent("test.log")
            .path
    }

    // Task 15 finding: main.swift's fatal-config-error path calls
    // `log.line("\(error)")` immediately before returning, on a path that
    // reaches `exit(0)` at the top level. `line(_:)` only enqueues the
    // write onto Log's own serial queue and returns immediately, and
    // `exit(_:)` does not wait for anything still queued -- confirmed
    // empirically (see the task report) that this reliably left the log
    // file empty across five repeated runs of the real CLI binary before
    // `flush()` existed. This test pins the same property Log's own
    // in-process unit, deterministically: after `flush()` returns, the
    // write is guaranteed complete, so reading the file synchronously
    // right after must see it -- no sleep, no polling, no timing
    // assumption either way.
    func testFlushBlocksUntilAPriorLineHasActuallyLanded() {
        let path = tempLogPath()
        let log = Log(path: path)
        log.line("hello from the test")
        log.flush()
        let contents = try? String(contentsOfFile: path, encoding: .utf8)
        XCTAssertEqual(contents?.contains("hello from the test"), true,
                       "flush() must not return before line()'s write has actually landed on disk")
    }

    func testFlushWithNothingQueuedReturnsImmediately() {
        let log = Log(path: tempLogPath())
        log.flush()   // must not hang when nothing was ever enqueued
    }
}
