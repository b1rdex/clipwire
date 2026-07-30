// Tests/clipwireTests/FrameTests.swift
import XCTest
@testable import clipwire

final class FrameTests: XCTestCase {
    func testRoundTrip() throws {
        let original = Frame(type: .clip, payload: Data("hi".utf8))
        var buffer = original.encode()
        let decoded = try Frame.decode(from: &buffer)
        XCTAssertEqual(decoded?.type, .clip)
        XCTAssertEqual(decoded?.payload, Data("hi".utf8))
        XCTAssertTrue(buffer.isEmpty, "decode must consume exactly one frame")
    }

    func testHeaderLayout() {
        let encoded = Frame(type: .clip, payload: Data("hi".utf8)).encode()
        XCTAssertEqual([UInt8](encoded), [0x00, 0x00, 0x00, 0x02, 0x01, 0x68, 0x69])
    }

    func testPartialFrameReturnsNil() throws {
        var buffer = Frame(type: .clip, payload: Data("hi".utf8)).encode()
        buffer.removeLast()
        XCTAssertNil(try Frame.decode(from: &buffer))
        XCTAssertEqual(buffer.count, 6, "an incomplete frame must not be consumed")
    }

    func testTwoFramesInOneBuffer() throws {
        var buffer = Frame(type: .clip, payload: Data("a".utf8)).encode()
        buffer.append(Frame(type: .clip, payload: Data("b".utf8)).encode())
        XCTAssertEqual(try Frame.decode(from: &buffer)?.payload, Data("a".utf8))
        XCTAssertEqual(try Frame.decode(from: &buffer)?.payload, Data("b".utf8))
        XCTAssertNil(try Frame.decode(from: &buffer))
    }

    func testOversizedLengthThrows() {
        var buffer = Data([0xFF, 0xFF, 0xFF, 0xFF, 0x7F])
        XCTAssertThrowsError(try Frame.decode(from: &buffer)) { error in
            guard case FrameError.oversized = error else {
                return XCTFail("expected .oversized, got \(error)")
            }
        }
    }

    func testUnknownTypeThrows() {
        var buffer = Data([0x00, 0x00, 0x00, 0x00, 0x7F])
        XCTAssertThrowsError(try Frame.decode(from: &buffer)) { error in
            guard case FrameError.unknownType(0x7F) = error else {
                return XCTFail("expected .unknownType(0x7F), got \(error)")
            }
        }
    }

    func testEmptyPayloadIsValid() throws {
        var buffer = Frame(type: .clip, payload: Data()).encode()
        XCTAssertEqual(try Frame.decode(from: &buffer)?.payload, Data())
    }

    func testMaxPayloadBoundaryIncomplete() throws {
        // Frame declaring exactly MAX_PAYLOAD_BYTES (4_194_304) is incomplete, not oversized
        var buffer = Data([0x00, 0x40, 0x00, 0x00, 0x00])
        XCTAssertNil(try Frame.decode(from: &buffer))
    }

    func testMaxPayloadBoundaryExceeded() {
        // Frame declaring MAX_PAYLOAD_BYTES + 1 (4_194_305) is oversized
        var buffer = Data([0x00, 0x40, 0x00, 0x01, 0x00])
        XCTAssertThrowsError(try Frame.decode(from: &buffer)) { error in
            guard case FrameError.oversized = error else {
                return XCTFail("expected .oversized, got \(error)")
            }
        }
    }
}
