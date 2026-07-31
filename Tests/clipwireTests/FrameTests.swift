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

    func testFrameTypeRawValuesArePinned() {
        // The golden fixtures (Task 3) round-trip whichever raw byte a case
        // carries — FrameType(rawValue:) followed by .rawValue is an identity
        // function regardless of which case owns which byte, so a consistent
        // relabelling of hello/clip would stay green there. Pin the actual
        // wire-format assignment explicitly here instead.
        XCTAssertEqual(FrameType.hello.rawValue, 0x00, "hello must be wire type 0x00")
        XCTAssertEqual(FrameType.clip.rawValue, 0x01, "clip must be wire type 0x01")
        // Same gap, same fix, for the type this task registers.
        XCTAssertEqual(FrameType.clipState.rawValue, 0x02, "clipState must be wire type 0x02")
    }

    func testClipStateRoundTrip() throws {
        // Mirrors testRoundTrip for .clip: proves the envelope carries the
        // new type correctly. The payload here is arbitrary opaque bytes --
        // this task registers the frame type, not the clip-state JSON codec,
        // which lands later.
        let original = Frame(type: .clipState, payload: Data("state".utf8))
        var buffer = original.encode()
        let decoded = try Frame.decode(from: &buffer)
        XCTAssertEqual(decoded?.type, .clipState)
        XCTAssertEqual(decoded?.payload, Data("state".utf8))
        XCTAssertTrue(buffer.isEmpty, "decode must consume exactly one frame")
    }

    func testProtocolVersionIsBumpedToV2() {
        // ProtocolConstants lives in main.swift, not this file -- but
        // @testable import gives this test target access regardless, and
        // this is the file the task brief designates for the assertion.
        XCTAssertEqual(ProtocolConstants.version, 2)
    }

    func testHelloPayloadContainsSentAtAsANumber() throws {
        // decodeHello's own return tuple is deliberately left unchanged by
        // this task (nothing consumes sent_at yet), so this parses the raw
        // JSON directly rather than going through decodeHello.
        let json = try JSONSerialization.jsonObject(with: ProtocolConstants.helloPayload) as? [String: Any]
        XCTAssertNotNil(json?["sent_at"] as? Double,
                         "hello payload must carry sent_at as a JSON number, not a string")
    }
}
