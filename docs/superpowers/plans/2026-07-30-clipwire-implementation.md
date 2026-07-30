# ClipWire Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One clipboard shared between a Mac and an Ubuntu/Wayland PC over a single SSH channel, surviving the PC's daily reboot with no manual action.

**Architecture:** The Mac agent (Swift, launchd) dials `ssh user@host <remote-agent-path>`; `sshd` spawns a single-file stdlib-Python agent on the PC. Length-prefixed frames flow both ways over that process's stdin/stdout. No server, no sessions, no listening ports — authentication and encryption come from SSH, so a reboot invalidates nothing.

**Tech Stack:** Swift 6.1 + SwiftPM + XCTest (macOS side), Python 3.11+ stdlib + `unittest` (PC side), GitHub Actions (CI). No third-party dependencies anywhere.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-07-30-clipwire-design.md`. Every behaviour here traces to it.
- **No third-party dependencies.** Not in the Swift package, not in the Python agent, not in the Python tests. The agent is deployed as one file with nothing alongside it; tests that need packages would stop resembling the target machine.
- **Python floor: 3.11.** Ubuntu 25.10 ships 3.13; CI runs on `ubuntu-latest`. Do not use 3.12+ syntax.
- **Swift floor: macOS 13.** `NSPasteboard.changeCount` long predates this; do not raise the floor.
- **No bare top-level `let` in the Swift target.** Namespace shared constants as `static let` on an enum, as `FrameConstants` and `StatusConstants` do. History: with a single file in the `executableTarget` the compiler treated it as the implicit main file and its top-level bindings silently read as zero under test (found in Task 1); once a second file arrived there was no main file at all and the target stopped linking (found in Task 6). A placeholder `Sources/clipwire/main.swift` now closes the second hazard, and the convention stays because it is immune to both.
- **Verification counts only from a clean build.** Run `rm -rf .build` before `swift build` / `swift test` and paste that output as evidence. Incremental state masked the link failure above across two whole tasks, during which every local "all green" was meaningless. After pushing, the controller checks the actual CI run rather than trusting a local result.
- **Frame format:** `[u32 big-endian payload length][u8 type][payload]`. The length counts **payload bytes only** — the 5-byte header is not included.
- **Frame types:** `0x00` hello, `0x01` clip (`text/plain; charset=utf-8`). No others in v1.
- **Protocol version: 1.** Carried in the hello payload.
- **Max payload: 4 MiB** (`4194304`).
- **Stream discipline:** the PC agent writes **only frames to stdout**. Every log line, warning and traceback goes to stderr. One stray `print()` desynchronises the protocol.
- **Single-writer discipline:** on both sides, all frame writes go through one serial path. Two frame sources (incoming reader, local watcher) writing concurrently corrupt the stream.
- **Empty clips are never synced**, in either direction.
- **Naming:** files, identifiers, comments, commit messages and docs are in English. The bundle identifier is `dev.b1rdex.clipwire`.

## File Structure

| Path | Responsibility |
|---|---|
| `Package.swift` | SwiftPM manifest: one executable target `clipwire`, one test target. |
| `Sources/clipwire/Frame.swift` | Frame encode/decode. Pure, no I/O. |
| `Sources/clipwire/Config.swift` | Load and validate `config.json`. |
| `Sources/clipwire/StatusFile.swift` | Write/read `status.json` with heartbeat and pid. |
| `Sources/clipwire/Backoff.swift` | Reconnect delay sequence. Pure. |
| `Sources/clipwire/EchoGuard.swift` | Last-written-hash suppression. Pure. |
| `Sources/clipwire/Pasteboard.swift` | `NSPasteboard` watcher via `changeCount`. |
| `Sources/clipwire/Channel.swift` | `ssh` process supervisor, serial frame writer, remote stderr pump. |
| `Sources/clipwire/Log.swift` | Rotating log file. |
| `Sources/clipwire/main.swift` | CLI dispatch: `run`, `status`, `init`, `install`. |
| `Tests/clipwireTests/*.swift` | XCTest suites, one per pure unit plus fixture conformance. |
| `agent/clipwire-agent.py` | The entire PC side: codec, lifecycle, clipboard I/O, watcher. Deployed as-is. |
| `agent/tests/test_*.py` | `unittest` suites. Never deployed. |
| `fixtures/frames.json` | Golden byte vectors both codecs must agree on. |
| `.github/workflows/ci.yml` | Two jobs: `swift` on macOS, `python` on Ubuntu. |
| `config.example.json` | Committed example. The real config lives outside the repo. |

Files that change together live together: the PC agent is deliberately one file because it is deployed as one file, and splitting it would create a packaging problem for no benefit.

---

### Task 1: Frame codec in Swift

**Files:**
- Create: `Package.swift`
- Create: `Sources/clipwire/Frame.swift`
- Test: `Tests/clipwireTests/FrameTests.swift`

**Interfaces:**
- Consumes: nothing.
- Produces: `enum FrameType: UInt8 { case hello = 0x00, clip = 0x01 }`; `struct Frame { let type: FrameType; let payload: Data }`; `Frame.encode() -> Data`; `static Frame.decode(from: inout Data) throws -> Frame?` returning `nil` when more bytes are needed; `enum FrameError: Error { case oversized(UInt32), unknownType(UInt8) }`; `enum FrameConstants { static let maxPayloadBytes = 4_194_304; static let headerBytes = 5 }` — namespaced, never bare top-level `let`, for the reason given in Step 4.

- [ ] **Step 1: Write the failing test**

```swift
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
        // The type byte is deliberately INVALID (0x7F). With a valid type byte
        // this assertion would hold whether the length or the type is checked
        // first, so it could not detect a swapped guard order — and a swapped
        // order is exactly how the two implementations would diverge on a
        // corrupt stream.
        var buffer = Data([0xFF, 0xFF, 0xFF, 0xFF, 0x7F])
        XCTAssertThrowsError(try Frame.decode(from: &buffer)) { error in
            guard case FrameError.oversized = error else {
                return XCTFail("expected .oversized, got \(error)")
            }
        }
    }

    func testBoundaryAtExactlyMaxPayload() throws {
        // A header declaring exactly the cap is legal but incomplete: nil, not a throw.
        var atCap = Data([0x00, 0x40, 0x00, 0x00, 0x01])
        XCTAssertEqual(UInt32(FrameConstants.maxPayloadBytes), 0x0040_0000)
        XCTAssertNil(try Frame.decode(from: &atCap))

        var overCap = Data([0x00, 0x40, 0x00, 0x01, 0x01])
        XCTAssertThrowsError(try Frame.decode(from: &overCap)) { error in
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
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `swift test`
Expected: compile failure — no `Frame`, no `Package.swift`.

- [ ] **Step 3: Write the package manifest**

```swift
// Package.swift
// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "clipwire",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(name: "clipwire", path: "Sources/clipwire"),
        .testTarget(name: "clipwireTests", dependencies: ["clipwire"], path: "Tests/clipwireTests"),
    ]
)
```

- [ ] **Step 4: Write the minimal implementation**

```swift
// Sources/clipwire/Frame.swift
import Foundation

// Namespaced deliberately. `clipwire` is an executableTarget, and until
// main.swift exists the compiler treats a lone file as the implicit main
// file: top-level `let` become statements of a main() that never runs when
// the module is linked into the test target, so the constants read as zero.
// Static members of an enum are swift_once-guarded regardless.
enum FrameConstants {
    static let maxPayloadBytes = 4_194_304
    static let headerBytes = 5
}

enum FrameType: UInt8 {
    case hello = 0x00
    case clip = 0x01
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
```

Note the ordering: the length is validated **before** the type byte, so a corrupt stream that produces an absurd length is reported as `oversized` rather than as a spurious unknown type.

- [ ] **Step 5: Run the tests**

Run: `swift test`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add Package.swift Sources/clipwire/Frame.swift Tests/clipwireTests/FrameTests.swift
git commit -m "Add frame codec (Swift)"
```

---

### Task 2: Frame codec in Python

**Files:**
- Create: `agent/clipwire-agent.py`
- Test: `agent/tests/test_frame.py`

**Interfaces:**
- Consumes: the wire format from Task 1 — identical bytes.
- Produces: `MAX_PAYLOAD_BYTES = 4194304`; `HEADER_BYTES = 5`; `TYPE_HELLO = 0x00`; `TYPE_CLIP = 0x01`; `PROTOCOL_VERSION = 1`; `encode_frame(frame_type: int, payload: bytes) -> bytes`; `decode_frame(buffer: bytearray) -> tuple[int, bytes] | None` consuming from the front; `class FrameError(Exception)`, `class OversizedFrame(FrameError)`, `class UnknownFrameType(FrameError)`.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_frame.py
import unittest

from agent_under_test import (
    OversizedFrame,
    TYPE_CLIP,
    UnknownFrameType,
    decode_frame,
    encode_frame,
)


class TestFrame(unittest.TestCase):
    def test_header_layout(self):
        self.assertEqual(
            encode_frame(TYPE_CLIP, b"hi"), b"\x00\x00\x00\x02\x01hi"
        )

    def test_round_trip(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, "привет".encode()))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, "привет".encode()))
        self.assertEqual(len(buffer), 0)

    def test_partial_frame_returns_none(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b"hi"))[:-1]
        self.assertIsNone(decode_frame(buffer))
        self.assertEqual(len(buffer), 6, "an incomplete frame must not be consumed")

    def test_two_frames_in_one_buffer(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b"a"))
        buffer += encode_frame(TYPE_CLIP, b"b")
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b"a"))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b"b"))
        self.assertIsNone(decode_frame(buffer))

    def test_oversized_length_raises(self):
        # Invalid type byte (0x7f) on purpose: with a valid one this passes
        # whether length or type is checked first, so it could not catch a
        # swapped guard order.
        with self.assertRaises(OversizedFrame):
            decode_frame(bytearray(b"\xff\xff\xff\xff\x7f"))

    def test_boundary_at_exactly_max_payload(self):
        # Exactly the cap is legal but incomplete: None, not an exception.
        self.assertIsNone(decode_frame(bytearray(b"\x00\x40\x00\x00\x01")))
        with self.assertRaises(OversizedFrame):
            decode_frame(bytearray(b"\x00\x40\x00\x01\x01"))

    def test_unknown_type_raises(self):
        with self.assertRaises(UnknownFrameType):
            decode_frame(bytearray(b"\x00\x00\x00\x00\x7f"))

    def test_empty_payload_is_valid(self):
        buffer = bytearray(encode_frame(TYPE_CLIP, b""))
        self.assertEqual(decode_frame(buffer), (TYPE_CLIP, b""))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Add the import shim so tests can load a hyphenated filename**

The agent file is `clipwire-agent.py` because that is what lands on the PC, but a hyphen is not importable. Create the shim once:

```python
# agent/tests/agent_under_test.py
"""Imports clipwire-agent.py under a legal module name."""
import importlib.util
import pathlib
import sys

_path = pathlib.Path(__file__).resolve().parent.parent / "clipwire-agent.py"
_spec = importlib.util.spec_from_file_location("clipwire_agent", _path)
_module = importlib.util.module_from_spec(_spec)
sys.modules["clipwire_agent"] = _module
_spec.loader.exec_module(_module)

globals().update({k: v for k, v in vars(_module).items() if not k.startswith("__")})
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: FAIL — `clipwire-agent.py` does not exist.

- [ ] **Step 4: Write the minimal implementation**

```python
#!/usr/bin/env python3
"""clipwire PC-side agent. Spawned by sshd; speaks frames on stdin/stdout.

Only frames go to stdout. Everything else goes to stderr — a stray print()
on stdout desynchronises the protocol.
"""

MAX_PAYLOAD_BYTES = 4194304
HEADER_BYTES = 5
TYPE_HELLO = 0x00
TYPE_CLIP = 0x01
PROTOCOL_VERSION = 1
_KNOWN_TYPES = (TYPE_HELLO, TYPE_CLIP)


class FrameError(Exception):
    pass


class OversizedFrame(FrameError):
    pass


class UnknownFrameType(FrameError):
    pass


def encode_frame(frame_type, payload):
    return len(payload).to_bytes(4, "big") + bytes([frame_type]) + payload


def decode_frame(buffer):
    """Consume one frame from the front of `buffer`.

    Returns (type, payload), or None when the buffer holds no complete frame.
    """
    if len(buffer) < HEADER_BYTES:
        return None
    length = int.from_bytes(buffer[:4], "big")
    if length > MAX_PAYLOAD_BYTES:
        raise OversizedFrame(length)
    frame_type = buffer[4]
    if frame_type not in _KNOWN_TYPES:
        raise UnknownFrameType(frame_type)
    total = HEADER_BYTES + length
    if len(buffer) < total:
        return None
    payload = bytes(buffer[HEADER_BYTES:total])
    del buffer[:total]
    return frame_type, payload
```

- [ ] **Step 5: Run the tests**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: PASS, 7 tests.

- [ ] **Step 6: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_frame.py agent/tests/agent_under_test.py
git commit -m "Add frame codec (Python agent)"
```

---

### Task 3: Golden fixtures both codecs must agree on

Two implementations of one wire format drift. A committed golden file makes drift fail on whichever side moved, with no coordination between CI runners.

**Files:**
- Create: `fixtures/frames.json`
- Test: `Tests/clipwireTests/FixtureTests.swift`
- Test: `agent/tests/test_fixtures.py`

**Interfaces:**
- Consumes: `Frame` (Task 1), `encode_frame`/`decode_frame` (Task 2).
- Produces: `fixtures/frames.json` — a list of `{"name", "type", "payload_hex", "frame_hex"}`.

- [ ] **Step 1: Write the fixture file**

Hex is used for both fields so the file stays byte-exact and language-neutral.

```json
{
  "comment": "Golden frame vectors. Both codecs must encode payload_hex to frame_hex and decode frame_hex back. Do not edit by hand without recomputing.",
  "cases": [
    {"name": "empty-clip",  "type": 1, "payload_hex": "",                         "frame_hex": "0000000001"},
    {"name": "ascii-clip",  "type": 1, "payload_hex": "6869",                     "frame_hex": "00000002016869"},
    {"name": "utf8-clip",   "type": 1, "payload_hex": "d0bfd180d0b8d0b2d0b5d182", "frame_hex": "0000000c01d0bfd180d0b8d0b2d0b5d182"},
    {"name": "emoji-clip",  "type": 1, "payload_hex": "f09f94a5",                 "frame_hex": "0000000401f09f94a5"},
    {"name": "newline-clip","type": 1, "payload_hex": "610a62",                   "frame_hex": "0000000301610a62"},
    {"name": "nul-clip",    "type": 1, "payload_hex": "610062",                   "frame_hex": "0000000301610062"},
    {"name": "hello",       "type": 0, "payload_hex": "7b2270726f746f636f6c223a317d", "frame_hex": "0000000e007b2270726f746f636f6c223a317d"}
  ]
}
```

The `hello` case uses the minimal payload `{"protocol":1}`. Real hello payloads also carry an `agent` field, and JSON key order is not guaranteed across languages — so **hello is fixture-tested at the frame layer only**. Its JSON body is validated by parsing, never by byte comparison. Encoding a full hello payload into a golden vector would be a test that fails for reasons unrelated to the protocol.

- [ ] **Step 2: Write the failing Swift conformance test**

```swift
// Tests/clipwireTests/FixtureTests.swift
import XCTest
@testable import clipwire

final class FixtureTests: XCTestCase {
    struct Case: Decodable {
        let name: String
        let type: UInt8
        let payload_hex: String
        let frame_hex: String
    }
    struct Fixtures: Decodable { let cases: [Case] }

    func loadFixtures() throws -> [Case] {
        // Tests/clipwireTests/ -> repo root -> fixtures/frames.json
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let data = try Data(contentsOf: root.appendingPathComponent("fixtures/frames.json"))
        return try JSONDecoder().decode(Fixtures.self, from: data).cases
    }

    func testEncodeMatchesGolden() throws {
        for c in try loadFixtures() {
            let frame = Frame(type: FrameType(rawValue: c.type)!, payload: hex(c.payload_hex))
            XCTAssertEqual(frame.encode(), hex(c.frame_hex), "encode mismatch for \(c.name)")
        }
    }

    func testDecodeMatchesGolden() throws {
        for c in try loadFixtures() {
            var buffer = hex(c.frame_hex)
            let frame = try Frame.decode(from: &buffer)
            XCTAssertEqual(frame?.type.rawValue, c.type, "type mismatch for \(c.name)")
            XCTAssertEqual(frame?.payload, hex(c.payload_hex), "payload mismatch for \(c.name)")
            XCTAssertTrue(buffer.isEmpty, "leftover bytes for \(c.name)")
        }
    }

    private func hex(_ s: String) -> Data {
        var out = Data()
        var i = s.startIndex
        while i < s.endIndex {
            let j = s.index(i, offsetBy: 2)
            out.append(UInt8(s[i..<j], radix: 16)!)
            i = j
        }
        return out
    }
}
```

- [ ] **Step 3: Write the failing Python conformance test**

```python
# agent/tests/test_fixtures.py
import json
import pathlib
import unittest

from agent_under_test import decode_frame, encode_frame

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "frames.json"


class TestFixtures(unittest.TestCase):
    def setUp(self):
        self.cases = json.loads(FIXTURES.read_text())["cases"]
        self.assertTrue(self.cases, "fixture file must not be empty")

    def test_encode_matches_golden(self):
        for c in self.cases:
            with self.subTest(c["name"]):
                encoded = encode_frame(c["type"], bytes.fromhex(c["payload_hex"]))
                self.assertEqual(encoded.hex(), c["frame_hex"])

    def test_decode_matches_golden(self):
        for c in self.cases:
            with self.subTest(c["name"]):
                buffer = bytearray(bytes.fromhex(c["frame_hex"]))
                frame_type, payload = decode_frame(buffer)
                self.assertEqual(frame_type, c["type"])
                self.assertEqual(payload.hex(), c["payload_hex"])
                self.assertEqual(len(buffer), 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 4: Run both suites**

Run: `swift test` and `cd agent && python3 -m unittest discover -s tests -v`
Expected: both PASS. If a `frame_hex` was mistyped, both sides fail identically — that is the file being wrong, not the codecs disagreeing.

- [ ] **Step 5: Commit**

```bash
git add fixtures/frames.json Tests/clipwireTests/FixtureTests.swift agent/tests/test_fixtures.py
git commit -m "Add golden frame fixtures and cross-language conformance tests"
```

---

### Task 4: CI

Without this the test-driven flow rests on nothing but discipline.

**Files:**
- Create: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: the Swift and Python suites from Tasks 1–3.
- Produces: nothing consumed by later tasks.

- [ ] **Step 1: Write the workflow**

```yaml
name: CI

on:
  push:
  pull_request:

jobs:
  swift:
    runs-on: macos-latest
    steps:
      - uses: actions/checkout@v4
      - run: swift --version
      - run: swift build
      - run: swift test

  python:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: python3 --version
      - name: Byte-compile the agent
        run: python3 -m compileall -q agent/clipwire-agent.py
      - name: Unit tests
        # No -t: agent/tests has no __init__.py, and unittest's discover
        # requires an importable start dir whenever it differs from the top
        # level. Verified: with -t agent this fails with
        # "Start directory is not importable".
        run: python3 -m unittest discover -s agent/tests -v
```

Task 12 appends a self-test step to this job. It is deliberately absent here: adding a step that fails until a later task lands would leave CI red across several commits, which trains everyone to ignore it.

- [ ] **Step 2: Verify locally what CI will run**

Run: `swift test && python3 -m unittest discover -s agent/tests -v`
Expected: both PASS.

- [ ] **Step 3: Commit and confirm the run is green**

```bash
git add .github/workflows/ci.yml
git commit -m "Add CI: Swift tests on macOS, agent tests on Ubuntu"
git push
gh run watch
```

CI cannot cover the real clipboard, the SSH channel, launchd, or the Local Network gate. Green CI means the pure logic holds — it is not evidence that sync works.

---

### Task 5: Configuration

**Files:**
- Create: `Sources/clipwire/Config.swift`
- Create: `config.example.json`
- Test: `Tests/clipwireTests/ConfigTests.swift`

**Interfaces:**
- Produces: `struct Config: Codable` with `host: String`, `fallbackIP: String?`, `user: String`, `identityFile: String`, `remoteAgentPath: String`, `macPollIntervalMs: Int`, `pcFallbackPollIntervalMs: Int`, `maxFrameBytes: Int`; `static Config.load(from: URL) throws -> Config`; `static Config.defaultURL: URL`; `enum ConfigError: Error { case missing(URL), invalid(String) }`; `func expandTilde(_ path: String) -> String`.

- [ ] **Step 1: Write the failing test**

```swift
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
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `swift test --filter ConfigTests`
Expected: compile failure — no `Config`.

- [ ] **Step 3: Implement**

```swift
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
        let config = try JSONDecoder().decode(Config.self, from: Data(contentsOf: url))
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
```

Missing config is `.missing`, not `.invalid`: the agent must never fall back to a built-in default and connect somewhere unintended.

- [ ] **Step 4: Write the example config**

```json
{
  "host": "your-pc-hostname",
  "fallback_ip": "192.168.1.10",
  "user": "your-username",
  "identity_file": "~/.ssh/id_ed25519",
  "remote_agent_path": "~/.local/share/clipwire/clipwire-agent.py",
  "mac_poll_interval_ms": 400,
  "pc_fallback_poll_interval_ms": 1000,
  "max_frame_bytes": 4194304
}
```

- [ ] **Step 5: Run the tests**

Run: `swift test --filter ConfigTests`
Expected: PASS, 5 tests.

- [ ] **Step 6: Commit**

```bash
git add Sources/clipwire/Config.swift Tests/clipwireTests/ConfigTests.swift config.example.json
git commit -m "Add configuration loading and validation"
```

---

### Task 6: Status file with liveness

A status file that reports "up" because its writer crashed is the invisible failure this project exists to avoid.

**Files:**
- Create: `Sources/clipwire/StatusFile.swift`
- Test: `Tests/clipwireTests/StatusTests.swift`

**Interfaces:**
- Produces: `enum ChannelState: String, Codable { case up, clipboardPending = "clipboard-pending", down }`; `struct Status: Codable` with `state`, `reason: String?`, `heartbeat: Date`, `pid: Int32`, `lastSentAt: Date?`, `lastReceivedAt: Date?`, `reconnects: Int`; `Status.write(to: URL) throws`; `static Status.read(from: URL, now: Date, pidIsAlive: (Int32) -> Bool) -> StatusReport`; `enum StatusReport { case healthy(Status), unhealthy(Status, String), agentDead(String) }`; `let heartbeatStaleAfter: TimeInterval = 15`.

- [ ] **Step 1: Write the failing test**

```swift
// Tests/clipwireTests/StatusTests.swift
import XCTest
@testable import clipwire

final class StatusTests: XCTestCase {
    private var url: URL!

    override func setUp() {
        url = FileManager.default.temporaryDirectory
            .appendingPathComponent("clipwire-status-\(UUID().uuidString).json")
    }

    func testRoundTrip() throws {
        let now = Date()
        try Status(state: .up, reason: nil, heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 3).write(to: url)
        guard case .healthy(let status) = Status.read(from: url, now: now, pidIsAlive: { _ in true }) else {
            return XCTFail("fresh heartbeat with a live pid must be healthy")
        }
        XCTAssertEqual(status.reconnects, 3)
    }

    func testStaleHeartbeatIsAgentDead() throws {
        let written = Date()
        try Status(state: .up, reason: nil, heartbeat: written, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 0).write(to: url)
        let later = written.addingTimeInterval(heartbeatStaleAfter + 1)
        guard case .agentDead = Status.read(from: url, now: later, pidIsAlive: { _ in true }) else {
            return XCTFail("a stale heartbeat must report agentDead regardless of state")
        }
    }

    func testDeadPidIsAgentDead() throws {
        let now = Date()
        try Status(state: .up, reason: nil, heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 0).write(to: url)
        guard case .agentDead = Status.read(from: url, now: now, pidIsAlive: { _ in false }) else {
            return XCTFail("a dead pid must report agentDead even with a fresh heartbeat")
        }
    }

    func testMissingFileIsAgentDead() {
        let absent = URL(fileURLWithPath: "/nonexistent/clipwire/status.json")
        guard case .agentDead = Status.read(from: absent, now: Date(), pidIsAlive: { _ in true }) else {
            return XCTFail("no status file means nothing is running")
        }
    }

    func testDownStateIsUnhealthyNotDead() throws {
        let now = Date()
        try Status(state: .down, reason: "peer unreachable", heartbeat: now, pid: 42,
                   lastSentAt: nil, lastReceivedAt: nil, reconnects: 7).write(to: url)
        guard case .unhealthy(_, let reason) = Status.read(from: url, now: now, pidIsAlive: { _ in true }) else {
            return XCTFail("a live agent with a down channel is unhealthy, not dead")
        }
        XCTAssertEqual(reason, "peer unreachable")
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `swift test --filter StatusTests`
Expected: compile failure.

- [ ] **Step 3: Implement**

```swift
// Sources/clipwire/StatusFile.swift
import Foundation

let heartbeatStaleAfter: TimeInterval = 15

enum ChannelState: String, Codable {
    case up
    case clipboardPending = "clipboard-pending"
    case down
}

struct Status: Codable {
    var state: ChannelState
    var reason: String?
    var heartbeat: Date
    var pid: Int32
    var lastSentAt: Date?
    var lastReceivedAt: Date?
    var reconnects: Int

    func write(to url: URL) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let tmp = url.appendingPathExtension("tmp")
        try encoder.encode(self).write(to: tmp)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }

    static func read(from url: URL, now: Date = Date(),
                     pidIsAlive: (Int32) -> Bool = { kill($0, 0) == 0 }) -> StatusReport {
        guard let data = try? Data(contentsOf: url) else {
            return .agentDead("no status file at \(url.path) — the agent has never run")
        }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        guard let status = try? decoder.decode(Status.self, from: data) else {
            return .agentDead("unreadable status file at \(url.path)")
        }
        if now.timeIntervalSince(status.heartbeat) > heartbeatStaleAfter {
            return .agentDead("heartbeat is stale — the agent died without updating its status")
        }
        guard pidIsAlive(status.pid) else {
            return .agentDead("pid \(status.pid) is not running")
        }
        switch status.state {
        case .up: return .healthy(status)
        case .clipboardPending, .down:
            return .unhealthy(status, status.reason ?? status.state.rawValue)
        }
    }
}

enum StatusReport {
    case healthy(Status)
    case unhealthy(Status, String)
    case agentDead(String)
}
```

Writes are atomic via a temp file and `replaceItemAt`, so `clipwire status` never reads a half-written file.

- [ ] **Step 4: Run the tests**

Run: `swift test --filter StatusTests`
Expected: PASS, 5 tests.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/StatusFile.swift Tests/clipwireTests/StatusTests.swift
git commit -m "Add status file with heartbeat and pid liveness"
```

---

### Task 7: Reconnect backoff and echo suppression

Both are pure logic, and both are things that go subtly wrong and are miserable to debug in a running system.

**Files:**
- Create: `Sources/clipwire/Backoff.swift`
- Create: `Sources/clipwire/EchoGuard.swift`
- Test: `Tests/clipwireTests/BackoffTests.swift`
- Test: `Tests/clipwireTests/EchoGuardTests.swift`

**Interfaces:**
- Produces: `struct Backoff { mutating func next() -> TimeInterval; mutating func reset() }` yielding 1, 2, 5, 15, 30, 60, 60, … seconds; `struct EchoGuard { mutating func noteWrittenLocally(_ payload: Data); mutating func shouldSend(_ payload: Data) -> Bool }`.

- [ ] **Step 1: Write the failing tests**

```swift
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
```

```swift
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `swift test --filter BackoffTests && swift test --filter EchoGuardTests`
Expected: compile failure.

- [ ] **Step 3: Implement**

```swift
// Sources/clipwire/Backoff.swift
import Foundation

struct Backoff {
    private static let ladder: [TimeInterval] = [1, 2, 5, 15, 30, 60]
    private var index = 0

    mutating func next() -> TimeInterval {
        let value = Backoff.ladder[min(index, Backoff.ladder.count - 1)]
        index += 1
        return value
    }

    mutating func reset() { index = 0 }
}
```

```swift
// Sources/clipwire/EchoGuard.swift
import CryptoKit
import Foundation

/// Suppresses the clipboard change caused by our own write, so a clip does not
/// ping-pong between the two machines forever.
struct EchoGuard {
    private var lastWritten: SHA256.Digest?

    mutating func noteWrittenLocally(_ payload: Data) {
        lastWritten = SHA256.hash(data: payload)
    }

    /// True when this local clipboard value is a genuine user action rather
    /// than the echo of what we just wrote. Consumes the suppression, so the
    /// user re-copying the same text later still syncs.
    mutating func shouldSend(_ payload: Data) -> Bool {
        guard let expected = lastWritten, SHA256.hash(data: payload) == expected else {
            return true
        }
        lastWritten = nil
        return false
    }
}
```

`shouldSend` clearing the stored hash is deliberate: suppression applies to the single change our write provokes, not forever. Without it, deliberately re-copying the same text would silently never sync.

- [ ] **Step 4: Run the tests**

Run: `swift test`
Expected: PASS, all suites.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/Backoff.swift Sources/clipwire/EchoGuard.swift Tests/clipwireTests/BackoffTests.swift Tests/clipwireTests/EchoGuardTests.swift
git commit -m "Add reconnect backoff and echo suppression"
```

---

### Task 8: PC agent lifecycle

The pre-login window is the most fragile part of the design and the least likely to be exercised by accident. It gets its own tests before any clipboard code exists.

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `encode_frame`, `decode_frame`, `TYPE_HELLO`, `TYPE_CLIP`, `PROTOCOL_VERSION` (Task 2).
- Produces: `class Agent` with `__init__(self, stdin, stdout, clipboard)`, `send_hello() -> None`, `hello_payload() -> bytes`, `send(frame_type, payload) -> None`, `on_frame(frame_type, payload) -> None`, `clipboard_became_ready() -> None`, `clipboard_lost() -> None`, `pending_clip` attribute, `phase` attribute (`"clipboard-pending"` or `"ready"`); the clipboard duck-type it depends on: `ready() -> bool`, `read() -> bytes | None`, `write(data: bytes) -> None`.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_lifecycle.py
import io
import unittest

from agent_under_test import (
    Agent,
    PROTOCOL_VERSION,
    TYPE_CLIP,
    TYPE_HELLO,
    decode_frame,
)


class FakeClipboard:
    def __init__(self, ready=False):
        self._ready = ready
        self.written = []

    def ready(self):
        return self._ready

    def become_ready(self):
        self._ready = True

    def read(self):
        return None

    def write(self, data):
        self.written.append(data)


class TestLifecycle(unittest.TestCase):
    def build(self, ready=False):
        clipboard = FakeClipboard(ready=ready)
        out = io.BytesIO()
        return Agent(stdin=io.BytesIO(), stdout=out, clipboard=clipboard), clipboard, out

    def test_hello_is_sent_before_clipboard_is_ready(self):
        agent, _, out = self.build(ready=False)
        agent.send_hello()
        buffer = bytearray(out.getvalue())
        frame_type, payload = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_HELLO)
        self.assertEqual(json_of(payload)["protocol"], PROTOCOL_VERSION)

    def test_starts_in_clipboard_pending(self):
        agent, _, _ = self.build(ready=False)
        self.assertEqual(agent.phase, "clipboard-pending")

    def test_clip_arriving_while_pending_is_not_written(self):
        agent, clipboard, _ = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, b"early")
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.pending_clip, b"early")

    def test_only_the_newest_pending_clip_survives(self):
        agent, clipboard, _ = self.build(ready=False)
        agent.on_frame(TYPE_CLIP, b"first")
        agent.on_frame(TYPE_CLIP, b"second")
        agent.on_frame(TYPE_CLIP, b"third")
        clipboard.become_ready()
        agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [b"third"])

    def test_becoming_ready_without_a_pending_clip_writes_nothing(self):
        agent, clipboard, _ = self.build(ready=False)
        clipboard.become_ready()
        agent.clipboard_became_ready()
        self.assertEqual(clipboard.written, [])
        self.assertEqual(agent.phase, "ready")

    def test_clip_when_ready_is_written_immediately(self):
        agent, clipboard, _ = self.build(ready=True)
        agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, b"now")
        self.assertEqual(clipboard.written, [b"now"])
        self.assertIsNone(agent.pending_clip)

    def test_empty_clip_is_never_written(self):
        agent, clipboard, _ = self.build(ready=True)
        agent.clipboard_became_ready()
        agent.on_frame(TYPE_CLIP, b"")
        self.assertEqual(clipboard.written, [])

    def test_losing_the_clipboard_returns_to_pending(self):
        agent, clipboard, _ = self.build(ready=True)
        agent.clipboard_became_ready()
        agent.clipboard_lost()
        self.assertEqual(agent.phase, "clipboard-pending")
        agent.on_frame(TYPE_CLIP, b"during outage")
        self.assertEqual(clipboard.written, [], "must not write while the session is gone")


def json_of(payload):
    import json
    return json.loads(payload.decode())


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: FAIL — `Agent` does not exist.

- [ ] **Step 3: Implement**

Append to `agent/clipwire-agent.py`:

```python
import json
import sys
import threading

AGENT_VERSION = "0.1.0"
PHASE_PENDING = "clipboard-pending"
PHASE_READY = "ready"


def log(message):
    """Diagnostics go to stderr. stdout carries frames and nothing else."""
    print(message, file=sys.stderr, flush=True)


class Agent:
    def __init__(self, stdin, stdout, clipboard):
        self.stdin = stdin
        self.stdout = stdout
        self.clipboard = clipboard
        self.phase = PHASE_PENDING
        self.pending_clip = None
        self._write_lock = threading.Lock()

    # --- outbound -------------------------------------------------------

    def hello_payload(self):
        return json.dumps(
            {"protocol": PROTOCOL_VERSION, "agent": AGENT_VERSION}
        ).encode()

    def send_hello(self):
        self.send(TYPE_HELLO, self.hello_payload())

    def send(self, frame_type, payload):
        """The single writer. Every frame leaves through here."""
        with self._write_lock:
            self.stdout.write(encode_frame(frame_type, payload))
            flush = getattr(self.stdout, "flush", None)
            if flush:
                flush()

    # --- inbound --------------------------------------------------------

    def on_frame(self, frame_type, payload):
        if frame_type == TYPE_HELLO:
            self._on_hello(payload)
        elif frame_type == TYPE_CLIP:
            self._on_clip(payload)

    def _on_hello(self, payload):
        try:
            peer = json.loads(payload.decode())
        except (UnicodeDecodeError, ValueError):
            raise FrameError("malformed hello payload")
        if peer.get("protocol") != PROTOCOL_VERSION:
            raise FrameError(
                "protocol mismatch: peer speaks %r, this agent speaks %d — "
                "run `clipwire install`" % (peer.get("protocol"), PROTOCOL_VERSION)
            )

    def _on_clip(self, payload):
        if not payload:
            return
        if self.phase != PHASE_READY:
            # Keep only the newest. Replaying a backlog into clipboard history
            # once the session appears is noise, not a feature.
            self.pending_clip = payload
            return
        self.clipboard.write(payload)

    # --- phase transitions ----------------------------------------------

    def clipboard_became_ready(self):
        self.phase = PHASE_READY
        if self.pending_clip is not None:
            self.clipboard.write(self.pending_clip)
            self.pending_clip = None

    def clipboard_lost(self):
        self.phase = PHASE_PENDING
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: PASS, 8 lifecycle tests plus the earlier suites.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_lifecycle.py
git commit -m "Add agent lifecycle: hello-first, pending phase, newest-clip-wins"
```

---

### Task 9: PC agent main loop with EOF exit

Without this, every Mac reconnect during the pre-login window spawns another agent while the previous ones linger, and once the session starts several agents race to write the clipboard.

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_mainloop.py`

**Interfaces:**
- Consumes: `Agent` (Task 8).
- Produces: `Agent.run() -> int` — the main loop, returning a process exit code; `main(argv) -> int`; `class NeverReadyClipboard` — the test double selected by `CLIPWIRE_FAKE_CLIPBOARD=never-ready`.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_mainloop.py
import os
import subprocess
import sys
import unittest
import pathlib

from agent_under_test import TYPE_HELLO, decode_frame, encode_frame

AGENT = pathlib.Path(__file__).resolve().parents[1] / "clipwire-agent.py"


class TestMainLoop(unittest.TestCase):
    def test_agent_exits_when_stdin_closes_before_clipboard_is_ready(self):
        """The pre-login window: no Wayland session, peer hangs up."""
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        process.stdin.close()
        self.assertEqual(process.wait(timeout=10), 0,
                         "a sleeping agent must still notice stdin EOF")

    def test_agent_sends_hello_immediately_even_when_not_ready(self):
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        header = process.stdout.read(5)
        length = int.from_bytes(header[:4], "big")
        payload = process.stdout.read(length)
        buffer = bytearray(header + payload)
        frame_type, _ = decode_frame(buffer)
        self.assertEqual(frame_type, TYPE_HELLO)
        process.stdin.close()
        process.wait(timeout=10)

    def test_agent_exits_on_protocol_mismatch(self):
        env = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD="never-ready")
        process = subprocess.Popen(
            [sys.executable, str(AGENT)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env,
        )
        process.stdin.write(encode_frame(TYPE_HELLO, b'{"protocol":999}'))
        process.stdin.flush()
        self.assertNotEqual(process.wait(timeout=10), 0,
                            "a version mismatch must fail loudly, not flap silently")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s agent/tests -p test_mainloop.py -v` (from the repository root — a dotted `tests.test_mainloop` path needs an `__init__.py` that deliberately does not exist)
Expected: FAIL — the agent has no `__main__` entry point.

- [ ] **Step 3: Implement**

Add `run` as a **method on the `Agent` class** defined in Task 8 — not as an attribute
assigned after the fact. This is one file; monkey-patching inside it makes the class
impossible to read top to bottom.

```python
import select

READ_CHUNK = 65536
CLIPBOARD_RECHECK_SECONDS = 1.0


class NeverReadyClipboard:
    """Test double selected by CLIPWIRE_FAKE_CLIPBOARD, so the main loop can be
    exercised on a machine with no Wayland session — including CI."""

    def ready(self):
        return False

    def read(self):
        return None

    def write(self, data):
        pass


```

Then add `run` **to the `Agent` class defined in Task 8** — indented as a method of
`Agent`, not of the test double above. Getting this wrong makes `run` a method of
`NeverReadyClipboard`, which fails only at runtime and is easy to miss:

```python

    def run(self):
        self.send_hello()
        buffer = bytearray()
        while True:
            # select() with a timeout in EVERY phase. An agent that sleeps in a
            # "wait for the Wayland socket" loop never reads stdin, never sees
            # EOF, and lingers after the channel drops — so each Mac reconnect
            # before login would leave another agent behind, and they would all
            # race to write the clipboard once the session appears.
            readable, _, _ = select.select([self.stdin], [], [], CLIPBOARD_RECHECK_SECONDS)

            if readable:
                chunk = os.read(self.stdin.fileno(), READ_CHUNK)
                if not chunk:
                    log("stdin closed, exiting")
                    return 0
                buffer += chunk
                while True:
                    frame = decode_frame(buffer)
                    if frame is None:
                        break
                    self.on_frame(*frame)

            was_ready = self.phase == PHASE_READY
            is_ready = self.clipboard.ready()
            if is_ready and not was_ready:
                log("clipboard is available")
                self.clipboard_became_ready()
            elif was_ready and not is_ready:
                log("clipboard went away, waiting for it to come back")
                self.clipboard_lost()

```

Finally, still at module level:

```python

def _select_clipboard():
    if os.environ.get("CLIPWIRE_FAKE_CLIPBOARD") == "never-ready":
        return NeverReadyClipboard()
    return WaylandClipboard()


def main(argv):
    if "--selftest" in argv:
        return selftest()
    agent = Agent(
        stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, clipboard=_select_clipboard()
    )
    try:
        return agent.run()
    except FrameError as error:
        log("protocol error: %s" % error)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

Add `import os` to the imports at the top of the file.

`WaylandClipboard` and `selftest` land in Tasks 10 and 12; until then the fake clipboard path is what the tests exercise. Define both as stubs now so the module imports cleanly:

```python
class WaylandClipboard:
    def ready(self):
        return False

    def read(self):
        return None

    def write(self, data):
        pass


def selftest():
    return 0
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_mainloop.py
git commit -m "Add agent main loop with stdin EOF exit in all phases"
```

---

### Task 10: Wayland clipboard I/O

**Files:**
- Modify: `agent/clipwire-agent.py` — replace the `WaylandClipboard` stub
- Test: `agent/tests/test_clipboard.py`

**Interfaces:**
- Produces: `class WaylandClipboard` with `ready()`, `read()`, `write(data)`; `runtime_dir() -> str`; `wayland_socket_path() -> str`; `clipboard_env() -> dict`; `SUBPROCESS_TIMEOUT = 3`.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_clipboard.py
import os
import unittest

from agent_under_test import clipboard_env, runtime_dir, wayland_socket_path


class TestEnvironment(unittest.TestCase):
    def test_uses_xdg_runtime_dir_when_present(self):
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/4242")
        self.assertEqual(runtime_dir(env), "/run/user/4242")

    def test_falls_back_to_uid_when_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        self.assertEqual(runtime_dir(env), "/run/user/%d" % os.getuid())

    def test_builds_wayland_display_and_bus_address(self):
        env = clipboard_env({"XDG_RUNTIME_DIR": "/run/user/4242"})
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/4242/bus"
        )
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")

    def test_socket_path(self):
        self.assertEqual(
            wayland_socket_path({"XDG_RUNTIME_DIR": "/run/user/4242"}),
            "/run/user/4242/wayland-0",
        )


if __name__ == "__main__":
    unittest.main()
```

An SSH session gets `XDG_RUNTIME_DIR` from `pam_systemd` but `WAYLAND_DISPLAY` is empty and there is no bus address — the agent constructs both itself. That is what these tests pin down.

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s agent/tests -p test_clipboard.py -v` (from the repository root — a dotted `tests.test_clipboard` path needs an `__init__.py` that deliberately does not exist)
Expected: FAIL — the helpers do not exist.

- [ ] **Step 3: Implement**

Replace the `WaylandClipboard` stub in `agent/clipwire-agent.py`:

```python
import subprocess

SUBPROCESS_TIMEOUT = 3


def runtime_dir(env=None):
    env = os.environ if env is None else env
    return env.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()


def wayland_socket_path(env=None):
    return os.path.join(runtime_dir(env), "wayland-0")


def clipboard_env(env=None):
    base = dict(os.environ if env is None else env)
    directory = runtime_dir(base)
    base["XDG_RUNTIME_DIR"] = directory
    base["WAYLAND_DISPLAY"] = "wayland-0"
    base["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=%s/bus" % directory
    return base


class WaylandClipboard:
    def ready(self):
        return os.path.exists(wayland_socket_path())

    def read(self):
        """Current clipboard text, or None when empty or not text.

        A non-zero exit from wl-paste means an empty or non-text selection.
        That is a normal state, not an error.
        """
        try:
            result = subprocess.run(
                ["wl-paste", "-n", "--type", "text/plain;charset=utf-8"],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT, env=clipboard_env(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as error:
            log("wl-paste failed: %r" % error)
            return None
        if result.returncode != 0:
            return None
        return result.stdout or None

    def write(self, data):
        """wl-copy does not exit — it stays resident as the selection owner.

        It must be spawned detached with its pipes closed. Waiting on it, or
        holding its fds, hangs the agent.
        """
        try:
            process = subprocess.Popen(
                ["wl-copy", "--type", "text/plain;charset=utf-8"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, env=clipboard_env(),
            )
        except FileNotFoundError:
            log("wl-copy is not installed")
            return
        try:
            process.stdin.write(data)
        except BrokenPipeError:
            log("wl-copy closed its pipe early")
        finally:
            process.stdin.close()  # hand off ownership; never wait()
```

- [ ] **Step 4: Run the tests**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: PASS. These tests run anywhere — they exercise environment construction, not a live compositor.

- [ ] **Step 5: Verify against the real machine (manual, not CI)**

```bash
ssh <user>@<host> 'XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 \
  python3 -c "
import sys; sys.path.insert(0, \".\")
exec(open(\"clipwire-agent.py\").read())
c = WaylandClipboard()
print(\"ready:\", c.ready())
c.write(b\"clipwire round trip\")
import time; time.sleep(0.3)
print(\"read back:\", c.read())
"'
```

Expected: `ready: True` and the text read back. This is the only way to prove the clipboard path works; CI cannot.

- [ ] **Step 6: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_clipboard.py
git commit -m "Add Wayland clipboard I/O with constructed session environment"
```

---

### Task 11: Clipboard watcher — GPaste with polling fallback

`wl-paste --watch` does not work here: wl-clipboard 2.2.1 needs the wlroots data-control protocol, and Mutter does not implement it.

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_watcher.py`

**Interfaces:**
- Consumes: `WaylandClipboard` (Task 10), `Agent.send` (Task 8).
- Produces: `parse_gpaste_line(line: str) -> bool` — true when the line is a clipboard-affecting Update signal; `class GPasteWatcher` with `start(on_change)`, `stop()`, `available() -> bool`; `class PollingWatcher` with the same interface.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_watcher.py
import unittest

from agent_under_test import parse_gpaste_line


class TestGPasteSignalParsing(unittest.TestCase):
    def test_accepts_the_real_captured_signal(self):
        """This is a verbatim line captured from the target machine.

        GPaste 45.3 reports target 'ALL' and a uint64 index — not 'CLIPBOARD'
        and not uint32. Filtering on 'CLIPBOARD' rejects every real signal.
        """
        line = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                "('REPLACE', 'ALL', uint64 0)")
        self.assertTrue(parse_gpaste_line(line))

    def test_accepts_any_target_including_ones_not_seen_yet(self):
        """Targets are not filtered: the content comparison is the real gate.

        GPaste's primary-to-history setting is off on the target machine, so
        primary selections emit nothing today — but that is a user-flippable
        setting, and a watcher that depends on it would break silently when it
        is flipped. Accepting every Update and letting the content comparison
        decide is immune to that.
        """
        for target in ("'ALL'", "'CLIPBOARD'", "'PRIMARY'"):
            line = ("/org/gnome/GPaste: org.gnome.GPaste2.Update "
                    "('REPLACE', %s, uint64 0)" % target)
            self.assertTrue(parse_gpaste_line(line), target)

    def test_ignores_unrelated_signals(self):
        self.assertFalse(parse_gpaste_line(
            "/org/gnome/GPaste: org.gnome.GPaste2.ShowHistory ()"))
        self.assertFalse(parse_gpaste_line(""))
        self.assertFalse(parse_gpaste_line("Monitoring signals..."))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s agent/tests -p test_watcher.py -v` (from the repository root — a dotted `tests.test_watcher` path needs an `__init__.py` that deliberately does not exist)
Expected: FAIL.

- [ ] **Step 3: Implement**

Append to `agent/clipwire-agent.py`:

```python
import threading
import time

GPASTE_OBJECT_PATH = "/org/gnome/GPaste"
# The BUS name is org.gnome.GPaste; org.gnome.GPaste2 is the INTERFACE name on
# that object. Verified on the target machine: --dest org.gnome.GPaste2 has no
# owner, so probing it would make available() always false and silently leave
# the watcher on the polling fallback forever.
GPASTE_BUS_NAME = "org.gnome.GPaste"


def parse_gpaste_line(line):
    """True when a gdbus monitor line is a GPaste Update signal.

    Verified against GPaste 45.3 on the target machine. The signal is
    Update(s action, s target, t index) and a real line looks like:

        /org/gnome/GPaste: org.gnome.GPaste2.Update ('REPLACE', 'ALL', uint64 0)

    The target is 'ALL', not 'CLIPBOARD'. Do not filter on the target: the
    observed value would reject every real signal, and the set of values
    depends on GPaste's own settings. Treat the signal as "something may have
    changed" and let the content comparison in Agent._local_change decide —
    that is correct whatever GPaste reports, and it also absorbs duplicate
    signals, which GPaste does emit.
    """
    return "Update" in line and GPASTE_OBJECT_PATH in line


class GPasteWatcher:
    """Event-driven. Python has no stdlib DBus binding, so this shells out to
    gdbus monitor and parses its output line by line."""

    def __init__(self):
        self._process = None
        self._thread = None
        self._stop = threading.Event()

    def available(self):
        try:
            result = subprocess.run(
                ["gdbus", "introspect", "--session", "--dest", GPASTE_BUS_NAME,
                 "--object-path", GPASTE_OBJECT_PATH],
                capture_output=True, timeout=SUBPROCESS_TIMEOUT, env=clipboard_env(),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        return result.returncode == 0

    def start(self, on_change):
        self._process = subprocess.Popen(
            ["gdbus", "monitor", "--session", "--dest", GPASTE_BUS_NAME,
             "--object-path", GPASTE_OBJECT_PATH],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, env=clipboard_env(),
        )

        def pump():
            for line in self._process.stdout:
                if self._stop.is_set():
                    return
                if parse_gpaste_line(line):
                    on_change()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._process:
            self._process.terminate()


class PollingWatcher:
    """Degraded mode: forks wl-paste and reads the whole clipboard each time,
    so it runs at a slower interval than the Mac's in-process poll."""

    def __init__(self, clipboard, interval_seconds):
        self.clipboard = clipboard
        self.interval = interval_seconds
        self._stop = threading.Event()
        self._thread = None

    def available(self):
        return True

    def start(self, on_change):
        def pump():
            previous = self.clipboard.read()
            while not self._stop.wait(self.interval):
                current = self.clipboard.read()
                if current != previous:
                    previous = current
                    on_change()

        self._thread = threading.Thread(target=pump, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()


def make_watcher(clipboard, fallback_interval_seconds=1.0):
    watcher = GPasteWatcher()
    if watcher.available():
        log("watching the clipboard through GPaste")
        return watcher
    log("GPaste unavailable, falling back to polling every %.1fs" % fallback_interval_seconds)
    return PollingWatcher(clipboard, fallback_interval_seconds)
```

Now **replace** the `clipboard_became_ready` and `clipboard_lost` methods written in Task 8
with these, and add `_local_change`. The watcher starts only once a session exists and
stops when it goes away — a `gdbus monitor` against a dead session is not useful.

```python
    def clipboard_became_ready(self):
        self.phase = PHASE_READY
        if self.pending_clip is not None:
            self._write_clip(self.pending_clip)
            self.pending_clip = None
        if self._watcher is None:
            self._watcher = make_watcher(self.clipboard)
            self._watcher.start(self._local_change)

    def clipboard_lost(self):
        self.phase = PHASE_PENDING
        if self._watcher is not None:
            self._watcher.stop()
            self._watcher = None

    def _write_clip(self, payload):
        """Single place where we touch the local clipboard, so echo
        bookkeeping cannot be forgotten on one of the paths."""
        self._last_written = payload
        self.clipboard.write(payload)

    def _local_change(self):
        text = self.clipboard.read()
        if not text:
            return
        # Consume the suppression on the FIRST observed change, whatever it is —
        # not only on a match. Our write produces exactly one change event; if we
        # observe a different one instead, ours is already gone, and a lingering
        # hash would silently swallow the user's later deliberate copy of the
        # same text. Mirrors EchoGuard.shouldSend on the Swift side, where the
        # match-only variant was found to be a real defect.
        expected, self._last_written = self._last_written, None
        if text == expected:
            return
        if len(text) > MAX_PAYLOAD_BYTES:
            log("skipping a clip of %d bytes: over the frame cap" % len(text))
            return
        self.send(TYPE_CLIP, text)
```

Pin the same scenario the Swift suite pins, so the two sides cannot drift on it: after
the agent writes a clip locally, a poll that observes *different* content must still let
a later deliberate re-copy of the original text through. Under the match-only variant
that second copy is swallowed.

Two more edits to `Agent`: add `self._watcher = None` and `self._last_written = None` to
`__init__`, and change `_on_clip` to call `self._write_clip(payload)` instead of
`self.clipboard.write(payload)`. Routing every write through one method is what keeps the
echo suppression correct on both the immediate and the pending-clip path — the
pending path is the one that is easy to forget, and Task 8's tests do not catch it.

Clearing `_last_written` after one match is deliberate: suppression applies to the single
change our write provokes, not forever. Without it, deliberately re-copying the same text
would silently never sync.

- [ ] **Step 4: Run the tests**

Run: `cd agent && python3 -m unittest discover -s tests -v`
Expected: PASS. The lifecycle tests from Task 8 must still pass — if they broke, the echo bookkeeping was wired in the wrong place.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_watcher.py
git commit -m "Add clipboard watcher: GPaste subscription with polling fallback"
```

---

### Task 12: Agent self-test

`clipwire install` needs a way to verify the deployed file actually works, on a machine that may not have a graphical session yet.

**Files:**
- Modify: `agent/clipwire-agent.py` — replace the `selftest` stub
- Modify: `.github/workflows/ci.yml` — enable the self-test step

**Interfaces:**
- Produces: `selftest() -> int` — 0 when healthy, non-zero otherwise.

- [ ] **Step 1: Implement**

```python
def selftest():
    """Verify the deployed agent without needing a Wayland session.

    Checks what install can check remotely; reports the rest as information.
    """
    ok = True

    if sys.version_info < (3, 11):
        log("FAIL python %s is older than 3.11" % ".".join(map(str, sys.version_info[:3])))
        ok = False
    else:
        log("ok   python %s" % ".".join(map(str, sys.version_info[:3])))

    probe = encode_frame(TYPE_CLIP, "clipwire selftest ✓".encode())
    buffer = bytearray(probe)
    decoded = decode_frame(buffer)
    if decoded != (TYPE_CLIP, "clipwire selftest ✓".encode()) or buffer:
        log("FAIL codec round trip")
        ok = False
    else:
        log("ok   codec round trip")

    for tool in ("wl-copy", "wl-paste"):
        found = subprocess.run(["which", tool], capture_output=True).returncode == 0
        log("%s %s" % ("ok  " if found else "FAIL", tool))
        ok = ok and found

    log("info wayland session: %s" % ("present" if os.path.exists(wayland_socket_path()) else "absent (fine before login)"))
    log("info gpaste: %s" % ("available" if GPasteWatcher().available() else "unavailable, will poll"))

    return 0 if ok else 1
```

A missing Wayland session is information, not a failure: `install` normally runs over SSH where there is no session, and the agent is designed to wait for one.

- [ ] **Step 2: Run it**

Run: `python3 agent/clipwire-agent.py --selftest; echo "exit=$?"`
Expected on a Mac: `FAIL wl-copy` / `FAIL wl-paste`, `exit=1`. On the target PC: all `ok`, `exit=0`.

- [ ] **Step 3: Enable the CI step**

The `Agent self-test` step from Task 4 will now fail on `ubuntu-latest`, which has no wl-clipboard. Install it in the workflow:

```yaml
      - name: Install clipboard tools
        run: sudo apt-get update && sudo apt-get install -y wl-clipboard
      - name: Agent self-test
        run: python3 agent/clipwire-agent.py --selftest
```

- [ ] **Step 4: Commit**

```bash
git add agent/clipwire-agent.py .github/workflows/ci.yml
git commit -m "Add agent self-test and wire it into CI"
git push && gh run watch
```

---

### Task 13: Mac pasteboard watcher

**Files:**
- Create: `Sources/clipwire/Pasteboard.swift`
- Test: `Tests/clipwireTests/PasteboardTests.swift`

**Interfaces:**
- Consumes: `EchoGuard` (Task 7).
- Produces: `protocol PasteboardReading { var changeCount: Int { get }; func readText() -> Data? }`; `final class SystemPasteboard: PasteboardReading`; `final class PasteboardWatcher` with `init(pasteboard:pollInterval:)`, `var onChange: ((Data) -> Void)?`, `func poll()`, `func start()`, `func stop()`, `func noteWrittenLocally(_ payload: Data)`.

- [ ] **Step 1: Write the failing test**

```swift
// Tests/clipwireTests/PasteboardTests.swift
import XCTest
@testable import clipwire

final class FakePasteboard: PasteboardReading {
    var changeCount = 0
    var text: Data?

    func set(_ value: String) {
        text = Data(value.utf8)
        changeCount += 1
    }

    func setNonText() {
        text = nil
        changeCount += 1
    }

    func readText() -> Data? { text }
}

final class PasteboardTests: XCTestCase {
    func testEmitsOnChange() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }

        watcher.poll()            // establishes the baseline, emits nothing
        pasteboard.set("hello")
        watcher.poll()
        XCTAssertEqual(seen, [Data("hello".utf8)])
    }

    func testUnchangedCountEmitsNothing() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        pasteboard.set("hello")
        watcher.poll()
        watcher.poll()
        watcher.poll()
        XCTAssertEqual(seen.count, 1, "changeCount unchanged means no work")
    }

    func testNonTextIsSkippedButChangeCountIsConsumed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.setNonText()   // e.g. an image
        watcher.poll()
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
        pasteboard.set("after the image")
        watcher.poll()
        XCTAssertEqual(seen, [Data("after the image".utf8)],
                       "the image must not have wedged the watcher")
    }

    func testEmptyClipIsNotEmitted() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.set("")
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }

    func testOurOwnWriteIsNotEchoed() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()

        watcher.noteWrittenLocally(Data("from the peer".utf8))
        pasteboard.set("from the peer")     // the write we just made
        watcher.poll()
        XCTAssertTrue(seen.isEmpty, "our own write must not bounce back")

        pasteboard.set("typed by the user")
        watcher.poll()
        XCTAssertEqual(seen, [Data("typed by the user".utf8)])
    }

    func testOversizedClipIsSkipped() {
        let pasteboard = FakePasteboard()
        let watcher = PasteboardWatcher(pasteboard: pasteboard, pollInterval: 0.4)
        var seen: [Data] = []
        watcher.onChange = { seen.append($0) }
        watcher.poll()
        pasteboard.set(String(repeating: "x", count: FrameConstants.maxPayloadBytes + 1))
        watcher.poll()
        XCTAssertTrue(seen.isEmpty)
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `swift test --filter PasteboardTests`
Expected: compile failure.

- [ ] **Step 3: Implement**

```swift
// Sources/clipwire/Pasteboard.swift
import AppKit
import Foundation

protocol PasteboardReading {
    var changeCount: Int { get }
    func readText() -> Data?
}

final class SystemPasteboard: PasteboardReading {
    private let pasteboard = NSPasteboard.general
    var changeCount: Int { pasteboard.changeCount }

    func readText() -> Data? {
        guard let string = pasteboard.string(forType: .string) else { return nil }
        return Data(string.utf8)
    }
}

/// Polls NSPasteboard.changeCount. The comparison is an integer read in-process,
/// so a sub-second interval costs nothing — unlike the PC side, which has to
/// fork a process and read the whole clipboard.
final class PasteboardWatcher {
    var onChange: ((Data) -> Void)?

    private let pasteboard: PasteboardReading
    private let pollInterval: TimeInterval
    private var lastChangeCount: Int
    private var echo = EchoGuard()
    private var timer: DispatchSourceTimer?

    init(pasteboard: PasteboardReading, pollInterval: TimeInterval) {
        self.pasteboard = pasteboard
        self.pollInterval = pollInterval
        self.lastChangeCount = pasteboard.changeCount
    }

    func noteWrittenLocally(_ payload: Data) {
        echo.noteWrittenLocally(payload)
    }

    func poll() {
        let current = pasteboard.changeCount
        guard current != lastChangeCount else { return }
        // Record the new count before any early return, so non-text content
        // cannot wedge the watcher into rescanning the same item forever.
        lastChangeCount = current

        guard let text = pasteboard.readText(), !text.isEmpty else { return }
        guard text.count <= FrameConstants.maxPayloadBytes else { return }
        guard echo.shouldSend(text) else { return }
        onChange?(text)
    }

    func start() {
        let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
        timer.schedule(deadline: .now() + pollInterval, repeating: pollInterval)
        timer.setEventHandler { [weak self] in self?.poll() }
        timer.resume()
        self.timer = timer
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `swift test`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/Pasteboard.swift Tests/clipwireTests/PasteboardTests.swift
git commit -m "Add macOS pasteboard watcher with changeCount polling"
```

---

### Task 14: SSH channel supervisor

**Files:**
- Create: `Sources/clipwire/Channel.swift`
- Create: `Sources/clipwire/Log.swift`
- Test: `Tests/clipwireTests/ChannelTests.swift`

**Interfaces:**
- Consumes: `Config` (Task 5), `Frame`/`FrameError` (Task 1), `Backoff` (Task 7), `ChannelState` (Task 6), `expandTilde` (Task 5).
- Produces: `func sshArguments(for config: Config, host: String) -> [String]`; `final class Channel` with `init(config:log:)`, `var onFrame: ((Frame) -> Void)?`, `var onStateChange: ((ChannelState, String?) -> Void)?`, `func send(_ frame: Frame)`, `func run()`; `final class Log` with `init(path:)`, `func line(_ message: String)`.

- [ ] **Step 1: Write the failing test**

```swift
// Tests/clipwireTests/ChannelTests.swift
import XCTest
@testable import clipwire

final class ChannelTests: XCTestCase {
    private func config(fallback: String? = nil) -> Config {
        Config(host: "pc", fallbackIP: fallback, user: "me",
               identityFile: "~/.ssh/id_ed25519",
               remoteAgentPath: "~/.local/share/clipwire/clipwire-agent.py",
               macPollIntervalMs: 400, pcFallbackPollIntervalMs: 1000,
               maxFrameBytes: FrameConstants.maxPayloadBytes)
    }

    func testArgumentsPinEverythingExplicitly() {
        let args = sshArguments(for: config(), host: "pc")
        XCTAssertTrue(args.contains("-o"))
        XCTAssertTrue(args.contains("IdentitiesOnly=yes"),
                      "must not fall back to the default identity list")
        XCTAssertTrue(args.contains("BatchMode=yes"),
                      "must never block on an interactive prompt under launchd")
        XCTAssertTrue(args.contains("ServerAliveInterval=5"))
        XCTAssertTrue(args.contains("ServerAliveCountMax=2"))
        XCTAssertTrue(args.contains("ConnectTimeout=5"))
        XCTAssertTrue(args.contains("me@pc"))
        XCTAssertEqual(args.last, "~/.local/share/clipwire/clipwire-agent.py")
    }

    func testIdentityPathIsExpanded() {
        let args = sshArguments(for: config(), host: "pc")
        guard let index = args.firstIndex(of: "-i") else { return XCTFail("no -i") }
        XCTAssertFalse(args[index + 1].hasPrefix("~"), "ssh gets no shell to expand ~")
    }

    func testFallbackIPIsUsedAsAnAlternateHost() {
        let args = sshArguments(for: config(fallback: "192.168.1.10"), host: "192.168.1.10")
        XCTAssertTrue(args.contains("me@192.168.1.10"))
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `swift test --filter ChannelTests`
Expected: compile failure.

- [ ] **Step 3: Implement the log**

```swift
// Sources/clipwire/Log.swift
import Foundation

/// Appends to a file, rotating at 5 MiB and keeping one previous generation.
final class Log {
    private let url: URL
    private let queue = DispatchQueue(label: "dev.b1rdex.clipwire.log")
    private let rotateAt = 5 * 1024 * 1024

    init(path: String) {
        url = URL(fileURLWithPath: expandTilde(path))
        try? FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
    }

    func line(_ message: String) {
        queue.async { [self] in
            let stamp = ISO8601DateFormatter().string(from: Date())
            let entry = Data("\(stamp) \(message)\n".utf8)
            rotateIfNeeded()
            if let handle = try? FileHandle(forWritingTo: url) {
                handle.seekToEndOfFile()
                handle.write(entry)
                try? handle.close()
            } else {
                try? entry.write(to: url)
            }
            FileHandle.standardError.write(entry)
        }
    }

    private func rotateIfNeeded() {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        let size = (attributes?[.size] as? Int) ?? 0
        guard size >= rotateAt else { return }
        let previous = url.appendingPathExtension("1")
        try? FileManager.default.removeItem(at: previous)
        try? FileManager.default.moveItem(at: url, to: previous)
    }
}
```

- [ ] **Step 4: Implement the channel**

```swift
// Sources/clipwire/Channel.swift
import Foundation

func sshArguments(for config: Config, host: String) -> [String] {
    [
        "-i", expandTilde(config.identityFile),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        "\(config.user)@\(host)",
        config.remoteAgentPath,
    ]
}

/// Owns the ssh process and the framing on its pipes. All writes go through
/// `send`, which serialises them — two concurrent writers would interleave
/// frames and corrupt the stream.
final class Channel {
    var onFrame: ((Frame) -> Void)?
    var onStateChange: ((ChannelState, String?) -> Void)?

    private let config: Config
    private let log: Log
    private let writeQueue = DispatchQueue(label: "dev.b1rdex.clipwire.write")
    private var process: Process?
    private var stdinPipe: Pipe?
    private var backoff = Backoff()
    private(set) var reconnects = 0

    init(config: Config, log: Log) {
        self.config = config
        self.log = log
    }

    func send(_ frame: Frame) {
        writeQueue.async { [self] in
            guard let pipe = stdinPipe else { return }
            do {
                try pipe.fileHandleForWriting.write(contentsOf: frame.encode())
            } catch {
                log.line("write failed: \(error)")
            }
        }
    }

    /// Dials, pumps until the channel dies, then waits and dials again. Never returns.
    func run() {
        var useFallback = false
        while true {
            let host = useFallback ? (config.fallbackIP ?? config.host) : config.host
            log.line("connecting to \(config.user)@\(host)")
            let alive = attempt(host: host)
            if alive {
                backoff.reset()
                onStateChange?(.down, "channel closed")
            } else {
                useFallback = config.fallbackIP != nil && !useFallback
            }
            reconnects += 1
            let delay = backoff.next()
            log.line("reconnecting in \(Int(delay))s")
            Thread.sleep(forTimeInterval: delay)
        }
    }

    /// Returns true when the channel was established before it dropped.
    private func attempt(host: String) -> Bool {
        let stdin = Pipe(), stdout = Pipe(), stderr = Pipe()
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = sshArguments(for: config, host: host)
        task.standardInput = stdin
        task.standardOutput = stdout
        task.standardError = stderr

        stderr.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else { return }
            for line in text.split(separator: "\n") where !line.isEmpty {
                self?.log.line("remote: \(line)")
            }
        }

        do { try task.run() } catch {
            log.line("failed to start ssh: \(error)")
            onStateChange?(.down, "cannot start ssh")
            return false
        }

        self.process = task
        self.stdinPipe = stdin
        var established = false
        var buffer = Data()

        while task.isRunning {
            let chunk = stdout.fileHandleForReading.availableData
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while true {
                do {
                    guard let frame = try Frame.decode(from: &buffer) else { break }
                    if !established {
                        established = true
                        onStateChange?(.clipboardPending, nil)
                    }
                    onFrame?(frame)
                } catch {
                    log.line("protocol error: \(error) — dropping the channel")
                    task.terminate()
                    buffer.removeAll()
                    break
                }
            }
        }

        task.waitUntilExit()
        stderr.fileHandleForReading.readabilityHandler = nil
        self.stdinPipe = nil
        self.process = nil
        log.line("ssh exited with status \(task.terminationStatus)")
        return established
    }
}
```

- [ ] **Step 5: Run the tests**

Run: `swift test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add Sources/clipwire/Channel.swift Sources/clipwire/Log.swift Tests/clipwireTests/ChannelTests.swift
git commit -m "Add SSH channel supervisor with serial writes and backoff"
```

---

### Task 15: CLI and end-to-end wiring

**Files:**
- Replace: `Sources/clipwire/main.swift` — a placeholder already exists (it writes one line to stderr and exits 2), added early because the executable target could not link without an entry point. Replace its whole body; keep nothing.
- Test: manual acceptance, below

**Interfaces:**
- Consumes: everything above.
- Produces: the `clipwire` executable with subcommands `run`, `status`, `init`, `install`.

- [ ] **Step 1: Implement**

```swift
// Sources/clipwire/main.swift
import Foundation

let statusURL = URL(fileURLWithPath: expandTilde("~/.local/state/clipwire/status.json"))
let logPath = "~/.local/state/clipwire/clipwire.log"

func runAgent() -> Int32 {
    let log = Log(path: logPath)
    let config: Config
    do {
        config = try Config.load()
    } catch {
        log.line("\(error)")
        // Exit 0 on a config error: the plist uses KeepAlive={SuccessfulExit: false},
        // so a non-zero exit here would give an eternal restart loop.
        return 0
    }

    var status = Status(state: .down, reason: "starting", heartbeat: Date(),
                        pid: ProcessInfo.processInfo.processIdentifier,
                        lastSentAt: nil, lastReceivedAt: nil, reconnects: 0)
    try? status.write(to: statusURL)

    let channel = Channel(config: config, log: log)
    let watcher = PasteboardWatcher(
        pasteboard: SystemPasteboard(),
        pollInterval: Double(config.macPollIntervalMs) / 1000.0)

    watcher.onChange = { payload in
        channel.send(Frame(type: .clip, payload: payload))
        status.lastSentAt = Date()
    }

    channel.onFrame = { frame in
        switch frame.type {
        case .hello:
            status.state = .up
            status.reason = nil
            log.line("peer said hello")
        case .clip:
            guard !frame.payload.isEmpty,
                  let text = String(data: frame.payload, encoding: .utf8) else { return }
            watcher.noteWrittenLocally(frame.payload)
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            status.lastReceivedAt = Date()
        }
    }

    channel.onStateChange = { state, reason in
        status.state = state
        status.reason = reason
    }

    channel.send(Frame(type: .hello,
                       payload: Data(#"{"protocol":1,"agent":"0.1.0"}"#.utf8)))
    watcher.start()

    let heartbeat = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    heartbeat.schedule(deadline: .now(), repeating: 5)
    heartbeat.setEventHandler {
        status.heartbeat = Date()
        status.reconnects = channel.reconnects
        try? status.write(to: statusURL)
    }
    heartbeat.resume()

    channel.run()   // never returns
    return 0
}

func printStatus() -> Int32 {
    switch Status.read(from: statusURL) {
    case .healthy(let status):
        print("up — \(status.reconnects) reconnects")
        if let sent = status.lastSentAt { print("last sent:     \(sent)") }
        if let received = status.lastReceivedAt { print("last received: \(received)") }
        return 0
    case .unhealthy(let status, let reason):
        print("\(status.state.rawValue) — \(reason)")
        return 1
    case .agentDead(let why):
        print("agent dead — \(why)")
        return 1
    }
}

func initConfig() -> Int32 {
    let url = Config.defaultURL
    guard !FileManager.default.fileExists(atPath: url.path) else {
        print("config already exists at \(url.path) — not overwriting")
        return 1
    }
    let example = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("config.example.json")
    do {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(contentsOf: example).write(to: url)
        print("wrote \(url.path) — edit it, then run `clipwire install`")
        return 0
    } catch {
        print("could not write config: \(error)")
        return 1
    }
}

func install() -> Int32 {
    guard let config = try? Config.load() else {
        print("no usable config — run `clipwire init` first")
        return 1
    }
    let source = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent("agent/clipwire-agent.py")
    let target = config.remoteAgentPath
    let remote = "\(config.user)@\(config.host)"

    func ssh(_ command: String) -> Int32 {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/usr/bin/ssh")
        task.arguments = ["-i", expandTilde(config.identityFile),
                          "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", remote, command]
        try? task.run()
        task.waitUntilExit()
        return task.terminationStatus
    }

    guard ssh("mkdir -p $(dirname \(target))") == 0 else {
        print("could not create the remote directory")
        return 1
    }

    let scp = Process()
    scp.executableURL = URL(fileURLWithPath: "/usr/bin/scp")
    scp.arguments = ["-i", expandTilde(config.identityFile),
                     "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes",
                     source.path, "\(remote):\(target)"]
    try? scp.run()
    scp.waitUntilExit()
    guard scp.terminationStatus == 0 else {
        print("copy failed")
        return 1
    }

    guard ssh("chmod +x \(target)") == 0 else { return 1 }
    let selftest = ssh("\(target) --selftest")
    print(selftest == 0 ? "installed and verified" : "installed, but --selftest failed")
    return selftest
}

let arguments = Array(CommandLine.arguments.dropFirst())
switch arguments.first {
case "run", nil: exit(runAgent())
case "status":   exit(printStatus())
case "init":     exit(initConfig())
case "install":  exit(install())
default:
    print("usage: clipwire [run|status|init|install]")
    exit(2)
}
```

- [ ] **Step 2: Build and check the CLI surface**

Run: `swift build && .build/debug/clipwire status`
Expected: `agent dead — no status file at …` and exit 1. That is correct: nothing is running yet.

- [ ] **Step 3: Install the launchd agent**

Create `~/Library/LaunchAgents/dev.b1rdex.clipwire.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.b1rdex.clipwire</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOUR_USER/.local/bin/clipwire</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
</dict>
</plist>
```

```bash
swift build -c release
mkdir -p ~/.local/bin && cp .build/release/clipwire ~/.local/bin/clipwire
launchctl load ~/Library/LaunchAgents/dev.b1rdex.clipwire.plist
```

- [ ] **Step 4: Acceptance test — this is what the project is for**

```bash
clipwire status                                   # expect: up
# 1. Copy text on the Mac, paste on the PC.
# 2. Copy text on the PC, paste on the Mac.
# 3. Reboot the PC. Log in. Wait ~30s.
clipwire status                                   # expect: up, reconnects incremented
# 4. Copy in both directions again — with no manual action taken.
```

Steps 3 and 4 are the acceptance criterion. Sync working before a reboot proves nothing; the daily reboot is exactly what killed the previous tool.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/main.swift
git commit -m "Add CLI: run, status, init, install"
git push
```

---

## Self-Review

**Spec coverage.** Every section of the design maps to a task: architecture and protocol → Tasks 1–3; CI → Tasks 4 and 12; configuration → Task 5; observability → Tasks 6 and 15; echo suppression and backoff → Task 7; lifecycle, EOF exit and single-writer discipline → Tasks 8, 9 and 14; clipboard I/O and the constructed session environment → Task 10; GPaste watcher with polling fallback → Task 11; installation and self-test → Tasks 12 and 15; the failure-handling table → distributed across Tasks 9 (EOF, protocol mismatch), 10 (timeouts, non-zero `wl-paste`), 11 (GPaste absent), 13 (non-text, empty, oversized) and 15 (fatal config error, `KeepAlive`).

**Deferred deliberately.** Wake-from-sleep re-dialling is out of v1 per the spec — `ServerAlive` recovers within ~15 s.

**Not covered by any automated test, by nature:** the real clipboard, the SSH channel, launchd, and the macOS Local Network gate. Task 10 Step 5 and Task 15 Step 4 are manual for that reason, and the design document says so plainly.
