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

    /// If the poll observes some other change before it ever observes the echo
    /// of our own write, our write's one event is already gone. A stored hash
    /// that only clears on a match would stay armed forever, waiting to eat a
    /// later, unrelated, deliberate re-copy of the same text.
    func testDeliberateRecopyStillSyncsAfterAMissedEcho() {
        var guardian = EchoGuard()
        let ourWrite = Data("our own write".utf8)
        let somethingElse = Data("a different clip the user made".utf8)
        guardian.noteWrittenLocally(ourWrite)
        XCTAssertTrue(guardian.shouldSend(somethingElse), "the poll missed our write and saw the user's clip")
        XCTAssertTrue(guardian.shouldSend(ourWrite), "a deliberate re-copy of our own text must still sync")
    }
}
