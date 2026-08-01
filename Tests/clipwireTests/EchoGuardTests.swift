// Tests/clipwireTests/EchoGuardTests.swift
import XCTest
@testable import clipwire

final class EchoGuardTests: XCTestCase {
    func testSuppressesExactlyWhatWeWrote() {
        var guardian = EchoGuard()
        let incoming = Data("from the peer".utf8)
        guardian.noteWrittenLocally(kind: .text, payload: incoming)
        XCTAssertFalse(guardian.shouldSend(kind: .text, payload: incoming), "our own write must not bounce back")
    }

    func testAllowsGenuineLocalChange() {
        var guardian = EchoGuard()
        guardian.noteWrittenLocally(kind: .text, payload: Data("from the peer".utf8))
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: Data("typed by the user".utf8)))
    }

    func testAllowsUserRecopyingTheSameTextAfterSomethingElse() {
        var guardian = EchoGuard()
        let text = Data("shared".utf8)
        guardian.noteWrittenLocally(kind: .text, payload: text)
        XCTAssertFalse(guardian.shouldSend(kind: .text, payload: text))
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: Data("other".utf8)))
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: text), "only the most recent write is suppressed")
    }

    func testFreshGuardSendsAnything() {
        var guardian = EchoGuard()
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: Data("anything".utf8)))
    }

    /// If the poll observes some other change before it ever observes the echo
    /// of our own write, our write's one event is already gone. A stored hash
    /// that only clears on a match would stay armed forever, waiting to eat a
    /// later, unrelated, deliberate re-copy of the same text.
    func testDeliberateRecopyStillSyncsAfterAMissedEcho() {
        var guardian = EchoGuard()
        let ourWrite = Data("our own write".utf8)
        let somethingElse = Data("a different clip the user made".utf8)
        guardian.noteWrittenLocally(kind: .text, payload: ourWrite)
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: somethingElse), "the poll missed our write and saw the user's clip")
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: ourWrite), "a deliberate re-copy of our own text must still sync")
    }

    /// Not text-by-value and images-by-hash: one rule. Kind and hash are one
    /// identity together, not two independent checks that could each pass
    /// while the other doesn't -- so identical bytes recorded under one kind
    /// must not suppress an observation of the SAME bytes under a different
    /// kind.
    func testTheSameBytesUnderADifferentKindAreNotSuppressed() {
        var guardian = EchoGuard()
        let bytes = Data("x".utf8)
        guardian.noteWrittenLocally(kind: .image, payload: bytes)
        XCTAssertTrue(guardian.shouldSend(kind: .text, payload: bytes),
                     "kind is part of identity, not decoration")
    }
}
