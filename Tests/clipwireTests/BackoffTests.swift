// Tests/clipwireTests/BackoffTests.swift
import XCTest
@testable import clipwire

final class BackoffTests: XCTestCase {
    func testSequenceAndCap() {
        var backoff = Backoff()
        XCTAssertEqual([1, 2, 5, 15, 30, 60, 60, 60].map { TimeInterval($0) },
                       (0..<8).map { _ in backoff.next() })
    }

    func testResetReturnsToStart() {
        var backoff = Backoff()
        _ = backoff.next(); _ = backoff.next(); _ = backoff.next()
        backoff.reset()
        XCTAssertEqual(backoff.next(), 1)
    }
}
