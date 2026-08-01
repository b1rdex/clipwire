// Sources/clipwire/Frame.swift
import Foundation

// NOTE: `clipwire` is an executableTarget with a single source file and no
// main.swift/@main. That makes this file the implicit main file, and top-level
// `let` bindings in a main file are sequential statements executed by `main()`
// — not lazily-initialized globals. The test target links this module without
// ever invoking `main()`, so top-level `let maxPayloadBytes = ...` would sit at
// zero-initialized memory (observed: reads as 0) when accessed from tests.
// Namespacing them as `static let` makes them swift_once-guarded regardless of
// entry-point status. This was scoped to Task 1: main.swift exists now, so
// Frame.swift is no longer the main file and this hazard no longer applies here.
//
// Three separate bounds, and they must stay separate even while two of them
// hold the same number. maxPayloadBytes is what Frame.decode enforces; the
// two content limits are what senders (Pasteboard.swift, main.swift) enforce
// BEFORE wrapping a body in its 8-byte timestamp (ClipPayloadConstants.
// timestampBytes). v2 used one constant for all of it, which made a
// maximum-size image unsendable while looking like it was within the limit:
// a 4 MiB image plus its timestamp does not fit a 4 MiB frame cap.
enum FrameConstants {
    static let maxPayloadBytes = 8_388_608
    static let maxTextBytes = 4_194_304
    static let maxImageBytes = 4_194_304
    static let headerBytes = 5
}

enum FrameType: UInt8 {
    case hello = 0x00
    case clip = 0x01
    case clipState = 0x02
    case imageClip = 0x03
}

enum FrameError: Error, Equatable {
    case oversized(UInt32)
    case unknownType(UInt8)
}

struct Frame: Equatable {
    let type: FrameType
    let payload: Data

    func encode() -> Data {
        var out = Data(capacity: FrameConstants.headerBytes + payload.count)
        let length = UInt32(payload.count)
        out.append(UInt8((length >> 24) & 0xFF))
        out.append(UInt8((length >> 16) & 0xFF))
        out.append(UInt8((length >> 8) & 0xFF))
        out.append(UInt8(length & 0xFF))
        out.append(type.rawValue)
        out.append(payload)
        return out
    }

    /// Decodes one frame from the front of `buffer`, consuming its bytes.
    /// Returns nil when the buffer does not yet hold a complete frame.
    static func decode(from buffer: inout Data) throws -> Frame? {
        guard buffer.count >= FrameConstants.headerBytes else { return nil }
        let bytes = [UInt8](buffer.prefix(FrameConstants.headerBytes))
        let length = (UInt32(bytes[0]) << 24) | (UInt32(bytes[1]) << 16)
            | (UInt32(bytes[2]) << 8) | UInt32(bytes[3])
        guard length <= UInt32(FrameConstants.maxPayloadBytes) else { throw FrameError.oversized(length) }
        guard let type = FrameType(rawValue: bytes[4]) else {
            throw FrameError.unknownType(bytes[4])
        }
        let total = FrameConstants.headerBytes + Int(length)
        guard buffer.count >= total else { return nil }
        let payload = Data(buffer[(buffer.startIndex + FrameConstants.headerBytes)..<(buffer.startIndex + total)])
        buffer.removeFirst(total)
        return Frame(type: type, payload: payload)
    }
}
