// Tests/clipwireTests/EchoGuardTests.swift
import XCTest
@testable import clipwire

final class EchoGuardTests: XCTestCase {
    func testSuppressesExactlyWhatWeWrote() {
        var guardian = EchoGuard()
        let incoming = Data("from the peer".utf8)
        guardian.noteWrittenLocally(incoming)
        XCTAssertFalse(guardian.shouldSend(incoming), "our own write must not bounce back")
    }

    func testAllowsGenuineLocalChange() {
        var guardian = EchoGuard()
        guardian.noteWrittenLocally(Data("from the peer".utf8))
        XCTAssertTrue(guardian.shouldSend(Data("typed by the user".utf8)))
    }

    func testAllowsUserRecopyingTheSameTextAfterSomethingElse() {
        var guardian = EchoGuard()
        let text = Data("shared".utf8)
        guardian.noteWrittenLocally(text)
        XCTAssertFalse(guardian.shouldSend(text))
        XCTAssertTrue(guardian.shouldSend(Data("other".utf8)))
        XCTAssertTrue(guardian.shouldSend(text), "only the most recent write is suppressed")
    }

    func testFreshGuardSendsAnything() {
        var guardian = EchoGuard()
        XCTAssertTrue(guardian.shouldSend(Data("anything".utf8)))
    }
}
