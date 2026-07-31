# ClipWire protocol v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile the two clipboards at connect time so the fresher one wins, fixing the wake flow that v1 breaks by design.

**Architecture:** Clip frames gain their own timestamp. A new `0x02` clip-state frame, sent once when each side's clipboard becomes readable, carries `(sha256, ts)`; a pure resolution rule decides which side sends. Timestamps for content that predates the agent come from a persistent store on each machine.

**Tech Stack:** Unchanged — Swift 6 + SwiftPM + XCTest on the Mac, Python 3.11+ stdlib + `unittest` on the PC, GitHub Actions.

## Global Constraints

- **Design spec:** `docs/superpowers/specs/2026-07-31-protocol-v2-freshness-design.md`, which amends `2026-07-30-clipwire-design.md`. Both are batya-reviewed; do not relitigate their decisions.
- **No third-party dependencies.** Not in the Swift package, not in the agent, not in either test suite.
- **Python floor 3.11.** Production runs 3.13.
- **Swift floor macOS 13.** No bare top-level `let` in the Swift target — namespace constants as `static let` on an enum.
- **Frame envelope is unchanged:** `[u32 BE payload length][u8 type][payload]`, length counting payload only.
- **Clip payload (type `0x01`) becomes** `[f64 big-endian ts][utf-8 text bytes]`. This is a breaking change; the golden vectors change with it.
- **New frame type `0x02` clip-state**, payload JSON `{"sha256": <hex string or null>, "ts": <float>}`.
- **`PROTOCOL_VERSION = 2`.** `hello` gains `sent_at`.
- **Max payload stays 4 MiB**, now including the 8-byte timestamp prefix.
- **stdout carries frames and nothing else** on the PC side; diagnostics to stderr.
- **Verification counts only from a clean build:** `rm -rf .build && swift build && swift test`. Use `rtk proxy` if the shell hook compacts output.
- **No test may hang**, require a live peer, a live compositor, or a live GPaste.
- **Never append below the `__main__` guard** in `agent/clipwire-agent.py` — `sys.exit(main(...))` means anything after it never exists in a real run while tests still see it.
- English throughout.

## File Structure

| Path | Change |
|---|---|
| `Sources/clipwire/ClipPayload.swift` | **New.** Encodes/decodes the `ts`+text clip payload. Pure. |
| `Sources/clipwire/Freshness.swift` | **New.** The resolution rule and the clip-state payload codec. Pure. |
| `Sources/clipwire/ClipStateStore.swift` | **New.** Persistent `(sha256, ts)` across agent restarts. |
| `Sources/clipwire/Frame.swift` | `PROTOCOL_VERSION`, `FrameType.clipState`. |
| `Sources/clipwire/Pasteboard.swift` | Watcher reports the observation timestamp alongside the payload. |
| `Sources/clipwire/main.swift` | Sends clip-state when ready; handles incoming clip-state and timestamped clips; skew logging. |
| `agent/clipwire-agent.py` | All of the above on the PC side, in the one deployed file. |
| `fixtures/frames.json` | v2 vectors: timestamped clips, clip-state frames. |
| `fixtures/freshness.json` | **New.** The resolution decision table, shared by both suites. |
| `Tests/clipwireTests/*`, `agent/tests/*` | Suites for each of the above. |
| `.github/workflows/ci.yml` | Python 3.13 in the matrix. |
| `README.md` | GNOME-extension check, secrets-in-history note. |

---

### Task 1: Clip payload codec (Swift)

**Files:**
- Create: `Sources/clipwire/ClipPayload.swift`
- Test: `Tests/clipwireTests/ClipPayloadTests.swift`

**Interfaces:**
- Consumes: nothing. The 4 MiB cap is enforced by the frame envelope layer, not here — this codec is a pure payload format.
- Produces: `struct ClipPayload { let ts: Double; let text: String }`; `func encode() -> Data`; `static func decode(_ data: Data) throws -> ClipPayload`; `enum ClipPayloadError: Error { case tooShort(Int), invalidUTF8 }`; `ClipPayloadConstants.timestampBytes = 8`.

- [ ] **Step 1: Write the failing test**

```swift
// Tests/clipwireTests/ClipPayloadTests.swift
import XCTest
@testable import clipwire

final class ClipPayloadTests: XCTestCase {
    func testRoundTrip() throws {
        let original = ClipPayload(ts: 1785400000.5, text: "привет 🔥")
        let decoded = try ClipPayload.decode(original.encode())
        XCTAssertEqual(decoded.ts, original.ts)
        XCTAssertEqual(decoded.text, original.text)
    }

    func testLayoutIsTimestampThenUTF8() {
        let encoded = ClipPayload(ts: 1.0, text: "hi").encode()
        XCTAssertEqual(encoded.count, 8 + 2)
        // 1.0 as IEEE-754 big-endian is 3F F0 00 00 00 00 00 00
        XCTAssertEqual([UInt8](encoded.prefix(8)),
                       [0x3F, 0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        XCTAssertEqual([UInt8](encoded.suffix(2)), [0x68, 0x69])
    }

    func testEmptyTextIsRepresentable() throws {
        let decoded = try ClipPayload.decode(ClipPayload(ts: 5, text: "").encode())
        XCTAssertEqual(decoded.text, "")
        XCTAssertEqual(decoded.ts, 5)
    }

    func testTooShortThrows() {
        XCTAssertThrowsError(try ClipPayload.decode(Data([0x00, 0x01, 0x02]))) { error in
            guard case ClipPayloadError.tooShort(3) = error else {
                return XCTFail("expected .tooShort(3), got \(error)")
            }
        }
    }

    func testInvalidUTF8Throws() {
        var data = Data(repeating: 0, count: 8)
        data.append(contentsOf: [0xFF, 0xFE])
        XCTAssertThrowsError(try ClipPayload.decode(data)) { error in
            guard case ClipPayloadError.invalidUTF8 = error else {
                return XCTFail("expected .invalidUTF8, got \(error)")
            }
        }
    }

    func testTimestampSurvivesSubsecondPrecision() throws {
        let ts = 1785400000.123456
        XCTAssertEqual(try ClipPayload.decode(ClipPayload(ts: ts, text: "x").encode()).ts, ts)
    }
}
```

- [ ] **Step 2: Run to verify it fails**

Run: `swift test --filter ClipPayloadTests`
Expected: compile failure — no `ClipPayload`.

- [ ] **Step 3: Implement**

```swift
// Sources/clipwire/ClipPayload.swift
import Foundation

enum ClipPayloadConstants {
    static let timestampBytes = 8
}

enum ClipPayloadError: Error, Equatable {
    case tooShort(Int)
    case invalidUTF8
}

/// The payload of a clip frame: when the text was copied, then the text.
///
/// The timestamp travels with the clip because the receiving side must record
/// the *peer's* timestamp for content it applies. Without it, applied content
/// would look freshly copied here and bounce straight back on the next
/// reconciliation.
struct ClipPayload: Equatable {
    let ts: Double
    let text: String

    func encode() -> Data {
        var out = Data(capacity: ClipPayloadConstants.timestampBytes + text.utf8.count)
        var bits = ts.bitPattern.bigEndian
        withUnsafeBytes(of: &bits) { out.append(contentsOf: $0) }
        out.append(contentsOf: Array(text.utf8))
        return out
    }

    static func decode(_ data: Data) throws -> ClipPayload {
        guard data.count >= ClipPayloadConstants.timestampBytes else {
            throw ClipPayloadError.tooShort(data.count)
        }
        let head = data.prefix(ClipPayloadConstants.timestampBytes)
        var bits: UInt64 = 0
        for byte in head { bits = (bits << 8) | UInt64(byte) }
        let body = data.dropFirst(ClipPayloadConstants.timestampBytes)
        guard let text = String(data: Data(body), encoding: .utf8) else {
            throw ClipPayloadError.invalidUTF8
        }
        return ClipPayload(ts: Double(bitPattern: bits), text: text)
    }
}
```

- [ ] **Step 4: Run the tests**

Run: `swift test`
Expected: PASS, existing suites unaffected.

- [ ] **Step 5: Commit**

```bash
git add Sources/clipwire/ClipPayload.swift Tests/clipwireTests/ClipPayloadTests.swift
git commit -m "Add clip payload codec carrying the copy timestamp (Swift)"
```

---

### Task 2: Clip payload codec (Python)

**Files:**
- Modify: `agent/clipwire-agent.py` — insert **above** the `__main__` guard
- Test: `agent/tests/test_clip_payload.py`

**Interfaces:**
- Produces: `TIMESTAMP_BYTES = 8`; `encode_clip_payload(ts: float, text: bytes) -> bytes`; `decode_clip_payload(payload: bytes) -> tuple[float, bytes]`; `class ClipPayloadError(FrameError)`.

Note the asymmetry with Swift, and keep it: the Python side works in **bytes** for the text, because that is what `wl-paste` returns and what `wl-copy` consumes. Decoding to `str` and re-encoding would introduce exactly the representation drift the design warns about.

- [ ] **Step 1: Write the failing test**

```python
# agent/tests/test_clip_payload.py
import struct
import unittest

from agent_under_test import (
    ClipPayloadError,
    decode_clip_payload,
    encode_clip_payload,
)


class TestClipPayload(unittest.TestCase):
    def test_round_trip(self):
        blob = encode_clip_payload(1785400000.5, "привет 🔥".encode())
        ts, text = decode_clip_payload(blob)
        self.assertEqual(ts, 1785400000.5)
        self.assertEqual(text, "привет 🔥".encode())

    def test_layout_is_timestamp_then_bytes(self):
        blob = encode_clip_payload(1.0, b"hi")
        self.assertEqual(blob[:8], struct.pack(">d", 1.0))
        self.assertEqual(blob[:8], bytes([0x3F, 0xF0, 0, 0, 0, 0, 0, 0]))
        self.assertEqual(blob[8:], b"hi")

    def test_empty_text_is_representable(self):
        self.assertEqual(decode_clip_payload(encode_clip_payload(5.0, b"")), (5.0, b""))

    def test_too_short_raises(self):
        with self.assertRaises(ClipPayloadError):
            decode_clip_payload(b"\x00\x01\x02")

    def test_arbitrary_bytes_survive(self):
        """Text is carried as bytes; nothing may normalise or re-encode it."""
        raw = b"e\xcc\x81\r\n\x00tail"          # NFD e-acute, CRLF, an embedded NUL
        self.assertEqual(decode_clip_payload(encode_clip_payload(1.0, raw))[1], raw)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m unittest discover -s agent/tests -p test_clip_payload.py -v`
Expected: ImportError — the names do not exist.

- [ ] **Step 3: Implement**

Insert beside the frame codec, well above the `__main__` guard:

```python
import struct

TIMESTAMP_BYTES = 8


class ClipPayloadError(FrameError):
    pass


def encode_clip_payload(ts, text):
    """[f64 big-endian ts][text bytes]. Text stays bytes end to end."""
    return struct.pack(">d", ts) + text


def decode_clip_payload(payload):
    if len(payload) < TIMESTAMP_BYTES:
        raise ClipPayloadError("clip payload shorter than its timestamp: %d bytes" % len(payload))
    (ts,) = struct.unpack(">d", payload[:TIMESTAMP_BYTES])
    return ts, bytes(payload[TIMESTAMP_BYTES:])
```

- [ ] **Step 4: Run the suites**

Run: `python3 -m unittest discover -s agent/tests -v` and `swift test`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_clip_payload.py
git commit -m "Add clip payload codec carrying the copy timestamp (Python agent)"
```

---

### Task 3: v2 golden vectors

**Files:**
- Modify: `fixtures/frames.json`
- Modify: `Tests/clipwireTests/FixtureTests.swift`, `agent/tests/test_fixtures.py`

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: fixture cases whose `payload_hex` for type `1` now begins with the 8-byte timestamp, plus type `2` cases.

- [ ] **Step 1: Update the fixture file**

Keep the existing structure. Replace the type-1 cases so their payloads carry a timestamp, and add two type-2 cases. Use `ts = 1.0` throughout so the prefix is the recognisable `3ff0000000000000`.

```json
{
  "comment": "Golden frame vectors, protocol v2. Type 1 payloads are [f64 BE ts][utf-8]; type 2 is clip-state JSON. Do not edit by hand without recomputing.",
  "cases": [
    {"name": "empty-clip",   "type": 1, "payload_hex": "3ff0000000000000",                         "frame_hex": "00000008013ff0000000000000"},
    {"name": "ascii-clip",   "type": 1, "payload_hex": "3ff00000000000006869",                     "frame_hex": "0000000a013ff00000000000006869"},
    {"name": "utf8-clip",    "type": 1, "payload_hex": "3ff0000000000000d0bfd180d0b8d0b2d0b5d182", "frame_hex": "00000014013ff0000000000000d0bfd180d0b8d0b2d0b5d182"},
    {"name": "emoji-clip",   "type": 1, "payload_hex": "3ff0000000000000f09f94a5",                 "frame_hex": "0000000c013ff0000000000000f09f94a5"},
    {"name": "nfd-clip",     "type": 1, "payload_hex": "3ff000000000000065cc81",                   "frame_hex": "0000000b013ff000000000000065cc81"},
    {"name": "crlf-clip",    "type": 1, "payload_hex": "3ff0000000000000610d0a62",                 "frame_hex": "0000000c013ff0000000000000610d0a62"},
    {"name": "hello",        "type": 0, "payload_hex": "7b2270726f746f636f6c223a327d",             "frame_hex": "0000000e007b2270726f746f636f6c223a327d"}
  ]
}
```

No clip-state case appears here: type `0x02` is not registered until Task 4, so a vector
for it would crash the Swift envelope test on an unknown raw value and raise
`UnknownFrameType` in Python. Task 4 adds it, **decode-only** — the same treatment `hello`
already gets, because JSON key order and float formatting differ between the two languages
and a byte-exact vector would fail for reasons unrelated to the protocol.

The `nfd-clip` and `crlf-clip` cases are deliberate: they pin that the codec passes a decomposed character and a CRLF through untouched, which is the byte-level half of the representation-fidelity concern.

- [ ] **Step 2: Verify the hex by computation before trusting it**

For each case, derive the frame bytes independently from the payload and the format rule. `1.0` as an IEEE-754 big-endian double is `3ff0000000000000`; the frame length counts the payload including that prefix. If any case disagrees, stop and report rather than adjusting a test to fit a wrong fixture.

- [ ] **Step 3: Extend both conformance suites**

The existing tests already iterate every case and check encode and decode at the frame layer; they need no structural change. Add, in each language, a case asserting that a type-1 fixture's payload also decodes through the **clip payload** codec to the expected `(ts, text)` — otherwise the fixtures pin the envelope while the new inner layout drifts freely.

- [ ] **Step 4: Run both suites**

Run: `swift test` and `python3 -m unittest discover -s agent/tests -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add fixtures/frames.json Tests/clipwireTests/FixtureTests.swift agent/tests/test_fixtures.py
git commit -m "Update golden vectors for protocol v2 clip payloads and clip-state"
```

---

### Task 4: Protocol v2 constants and the clip-state frame type

**Files:**
- Modify: `Sources/clipwire/Frame.swift`, `agent/clipwire-agent.py`
- Test: `Tests/clipwireTests/FrameTests.swift`, `agent/tests/test_frame.py`

**Interfaces:**
- Produces: `FrameType.clipState = 0x02` and `TYPE_CLIP_STATE = 0x02`; `PROTOCOL_VERSION` becomes `2` on both sides; `hello` payloads gain `sent_at`.

- [ ] **Step 1: Write the failing tests**

Add the clip-state vector to `fixtures/frames.json` here, where the type finally exists, and
assert it **decode-only** in both suites: given
`{"sha256": null, "ts": 1.0}` encoded as a type-2 frame, each side must decode it to type 2
and a payload that parses to those values. Do not compare encoded bytes — key order and float
formatting differ between the languages, and `hello` is already treated this way for the same
reason.

Then, in each language, assert: the new type's raw value is `0x02`; a frame of that type round-trips; `PROTOCOL_VERSION == 2`; and a hello payload contains `sent_at` as a number. Pin the raw values directly — `FrameType.clipState.rawValue == 0x02` and `TYPE_CLIP_STATE == 0x02` — for the same reason the other two are pinned: the fixtures route raw bytes through and cannot detect a relabelling.

- [ ] **Step 2: Run to verify they fail**

Run: `swift test` and `python3 -m unittest discover -s agent/tests -v`

- [ ] **Step 3: Implement**

Add the case to `FrameType` and the constant to the Python known-types tuple. Bump both `PROTOCOL_VERSION` constants to 2. Add `sent_at` to both `hello` payload builders, set to the sender's current clock.

Leave the mismatch handling exactly as it is: it already closes the channel and surfaces the reason in `status`, which is what makes a half-updated deployment fail loudly instead of misbehaving quietly.

- [ ] **Step 4: Run both suites**

- [ ] **Step 5: Commit**

```bash
git commit -am "Bump protocol to v2: clip-state frame type and hello sent_at"
```

---

### Task 5: Freshness resolution rule (Swift)

**Files:**
- Create: `Sources/clipwire/Freshness.swift`
- Create: `fixtures/freshness.json`
- Test: `Tests/clipwireTests/FreshnessTests.swift`

**Interfaces:**
- Produces: `struct ClipState: Codable, Equatable { let sha256: String?; let ts: Double }`; `ClipState.encodePayload() -> Data` and `static decodePayload(_:) throws -> ClipState`; `enum FreshnessDecision { case sendMine, waitForPeer, doNothing }`; `func resolveFreshness(mine: ClipState, peer: ClipState) -> FreshnessDecision`.

- [ ] **Step 1: Write the decision table as a fixture**

```json
{
  "comment": "Freshness resolution table, shared by both implementations. Hashes are abbreviated but compared as strings, exactly as the real hex hashes are.",
  "cases": [
    {"name": "equal-hashes",        "mine": {"sha256": "aa", "ts": 5},    "peer": {"sha256": "aa", "ts": 9},    "expect": "doNothing"},
    {"name": "mine-null",           "mine": {"sha256": null, "ts": 0},    "peer": {"sha256": "bb", "ts": 1},    "expect": "waitForPeer"},
    {"name": "peer-null",           "mine": {"sha256": "aa", "ts": 1},    "peer": {"sha256": null, "ts": 9},    "expect": "sendMine"},
    {"name": "both-null",           "mine": {"sha256": null, "ts": 0},    "peer": {"sha256": null, "ts": 0},    "expect": "doNothing"},
    {"name": "mine-fresher",        "mine": {"sha256": "aa", "ts": 10},   "peer": {"sha256": "bb", "ts": 9},    "expect": "sendMine"},
    {"name": "peer-fresher",        "mine": {"sha256": "aa", "ts": 8},    "peer": {"sha256": "bb", "ts": 9},    "expect": "waitForPeer"},
    {"name": "tie-mine-greater",    "mine": {"sha256": "bb", "ts": 7},    "peer": {"sha256": "aa", "ts": 7},    "expect": "sendMine"},
    {"name": "tie-peer-greater",    "mine": {"sha256": "aa", "ts": 7},    "peer": {"sha256": "bb", "ts": 7},    "expect": "waitForPeer"}
  ]
}
```

`peer-null` is the row that also closes v1's documented loss of Mac copies made while the PC was off. `mine-null` is the row whose absence would put a `Double` next to a `nil` and crash the Python side on every handshake following a PC reboot.

- [ ] **Step 2: Write the failing test**

Drive every fixture case through `resolveFreshness` and assert the expected decision. Add one test that the fixture file is non-empty, so a mis-read file cannot pass vacuously.

- [ ] **Step 3: Run to verify it fails**

- [ ] **Step 4: Implement**

```swift
// Sources/clipwire/Freshness.swift
import Foundation

struct ClipState: Codable, Equatable {
    let sha256: String?
    let ts: Double

    func encodePayload() -> Data {
        (try? JSONEncoder().encode(self)) ?? Data("{}".utf8)
    }

    static func decodePayload(_ data: Data) throws -> ClipState {
        try JSONDecoder().decode(ClipState.self, from: data)
    }
}

enum FreshnessDecision: String, Equatable {
    case sendMine
    case waitForPeer
    case doNothing
}

/// Decides which side sends after both have announced what they hold.
///
/// Timestamps are never compared when either hash is nil — that comparison is
/// what would otherwise put a number next to a null and take the Python agent
/// down on every handshake with an empty clipboard.
///
/// The tie is broken by comparing hashes rather than privileging a machine, so
/// both implementations run one formula instead of a mirrored pair of
/// conditions. Mirrored conditions drifting apart has already bitten this
/// project twice.
func resolveFreshness(mine: ClipState, peer: ClipState) -> FreshnessDecision {
    switch (mine.sha256, peer.sha256) {
    case (nil, nil):
        return .doNothing
    case (nil, _):
        return .waitForPeer
    case (_, nil):
        return .sendMine
    case let (mineHash?, peerHash?):
        if mineHash == peerHash { return .doNothing }
        if mine.ts > peer.ts { return .sendMine }
        if mine.ts < peer.ts { return .waitForPeer }
        return mineHash > peerHash ? .sendMine : .waitForPeer
    }
}
```

- [ ] **Step 5: Run the tests, then commit**

```bash
git add Sources/clipwire/Freshness.swift fixtures/freshness.json Tests/clipwireTests/FreshnessTests.swift
git commit -m "Add freshness resolution rule and its shared decision table (Swift)"
```

---

### Task 6: Freshness resolution rule (Python)

**Files:**
- Modify: `agent/clipwire-agent.py` — above the `__main__` guard
- Test: `agent/tests/test_freshness.py`

**Interfaces:**
- Consumes: `fixtures/freshness.json` from Task 5.
- Produces: `SEND_MINE`, `WAIT_FOR_PEER`, `DO_NOTHING` string constants matching the fixture's `expect` values; `resolve_freshness(mine, peer)` taking and comparing `(sha256, ts)` pairs; `encode_clip_state(sha256, ts) -> bytes`; `decode_clip_state(payload) -> tuple`.

- [ ] **Step 1: Write the failing test** driving the same `fixtures/freshness.json` through `resolve_freshness`, resolving the path with `parents[2]` as `test_fixtures.py` already does. Assert the file is non-empty.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement** the identical rule, in the same order, with the same tie-break. Write the comparison as one expression per branch so the two implementations can be read side by side.

- [ ] **Step 4: Run both suites. The same fixture must produce the same decisions in both languages** — that is the entire point of putting the table in a file.

- [ ] **Step 5: Commit**

```bash
git add agent/clipwire-agent.py agent/tests/test_freshness.py
git commit -m "Add freshness resolution rule against the shared table (Python agent)"
```

---

### Task 7: Persistent clip-state store (Swift)

**Files:**
- Create: `Sources/clipwire/ClipStateStore.swift`
- Test: `Tests/clipwireTests/ClipStateStoreTests.swift`

**Interfaces:**
- Produces: `struct ClipStateStore { init(path: String); func load() -> ClipState?; func save(_ state: ClipState) }`; `ClipStateStoreConstants.defaultPath = "~/.local/state/clipwire/clip-state.json"`; `func resolveStartupState(currentHash: String?, stored: ClipState?, now: Double) -> ClipState`.

- [ ] **Step 1: Write the failing test**

Cover: a round trip through a temp path; a missing file loads as `nil`; an unreadable or malformed file loads as `nil` rather than throwing; writes are atomic, following `StatusFile.swift`'s existing temp-file-and-replace pattern.

Then cover `resolveStartupState`, which is the heart of the wake flow:

- stored hash equals the current clipboard hash → the stored `ts` is returned, **not** `now`;
- stored hash differs → `now` is returned (the content changed while nothing was watching);
- nothing stored → `now`;
- current hash is `nil` → a state with a `nil` hash, and the timestamp is irrelevant.

The first of those is the assertion that makes the wake flow work: get it wrong and a clip copied while the peer slept is stamped `now`, wins every reconciliation, and clobbers systematically in the other direction.

- [ ] **Step 2: Run to verify it fails.**

- [ ] **Step 3: Implement.** Reuse `expandTilde`. Log through the caller rather than printing.

- [ ] **Step 4: Run the tests, then commit.**

```bash
git commit -m "Add persistent clip-state store and startup resolution (Swift)"
```

---

### Task 8: Persistent clip-state store (Python)

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_clip_state_store.py`

**Interfaces:**
- Produces: `clip_state_path()` returning `$XDG_STATE_HOME/clipwire/clip-state.json` with the same `/run`-independent fallback logic already used for the runtime directory; `load_clip_state()`, `save_clip_state(sha256, ts)`, `resolve_startup_state(current_hash, stored, now)`.

Mirror Task 7 exactly, including the four `resolve_startup_state` cases. Write the file atomically — a torn state file must read as absent, not as a corrupt timestamp.

- [ ] Same five steps; commit as `"Add persistent clip-state store and startup resolution (Python agent)"`.

---

### Task 9: Wire reconciliation into the Mac agent

**Files:**
- Modify: `Sources/clipwire/main.swift`, `Sources/clipwire/Pasteboard.swift`
- Test: `Tests/clipwireTests/AgentWiringTests.swift`, `Tests/clipwireTests/HandleFrameTests.swift`

**Interfaces:**
- Consumes: everything above.
- Produces: `handleFrame` extended for `.clipState`; the watcher's `onChange` carrying an observation timestamp.

- [ ] **Step 1: Write the failing tests**, extending the existing `handleFrame` suite:

- an incoming `.clip` is decoded through `ClipPayload`, and the **peer's** `ts` is what gets stored — not `now`;
- the echo suppression is still armed **before** the pasteboard write, on this path too;
- an incoming `.clipState` that loses produces no send;
- an incoming `.clipState` that wins produces exactly one `.clip` frame, carrying our stored `ts`;
- clip-state is sent once when the pasteboard first becomes readable, and not again.

- [ ] **Step 2–4: implement and verify.** The watcher reports `(payload, observedAt)`; a local change stores `(hash, now)` in the store and sends a clip carrying that timestamp; an applied remote clip stores `(hash, peerTs)`.

- [ ] **Step 5: Commit** as `"Wire freshness reconciliation into the Mac agent"`.

---

### Task 10: Wire reconciliation into the PC agent

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_lifecycle.py`, `agent/tests/test_watcher.py`

Mirror Task 9: send clip-state inside `clipboard_became_ready` after the store has been consulted; handle an incoming clip-state through `resolve_freshness`; decode incoming clips through `decode_clip_payload` and store the peer's timestamp; stamp local changes with the observation time.

**Keep `_last_seen` and its connect-time seed.** They suppress intra-session non-events — a GPaste signal reporting no actual change — which is a different problem from inter-session freshness and was a real defect when it was missing. Removing them because "the handshake handles it now" reintroduces that defect.

- [ ] Five steps as usual; commit as `"Wire freshness reconciliation into the PC agent"`.

---

### Task 11: Safety-net poll for a dead event source

**Files:**
- Modify: `agent/clipwire-agent.py`
- Test: `agent/tests/test_watcher.py`

`GPasteWatcher.available()` probes the bus name, but GPaste tracks the clipboard through a gnome-shell extension: after a GNOME upgrade the daemon can be alive and the bus answering while the extension is disabled, so `Update` never fires and PC→Mac sync is silently dead.

- [ ] **Step 1: Write the failing test.** A watcher whose signal source produces nothing, driven past two safety-net intervals with the clipboard content changing, must report the change and log the switch exactly once.

- [ ] **Step 2–4:** add a 30-second poll alongside the subscription. **It must call the same `_local_change` as a signal does** — same observation, same one-shot suppression, same `_last_seen`. A parallel path would be a second copy of echo logic this project has already fixed two races in. On a change the signal path missed, log once and switch to polling for the rest of the connection. Note in a comment that a clip caught only by the safety net carries a timestamp up to 30 seconds late.

- [ ] **Step 5: Commit** as `"Add a safety-net poll for a silently dead GPaste event source"`.

---

### Task 12: Skew logging, CI matrix, README

**Files:**
- Modify: `Sources/clipwire/main.swift`, `agent/clipwire-agent.py`, `.github/workflows/ci.yml`, `README.md`

- [ ] **Skew.** On receiving `hello`, compute `abs(now - peer.sent_at)` and log it; warn above five seconds. **Not** the difference between the peer's clip timestamp and the local clock — that is the age of the clip, and a clip legitimately copied this morning is hours old, so warning on it would fire on nearly every handshake and teach everyone to ignore the log. Add a test that the warning threshold is applied to the right quantity, by feeding a hello whose `sent_at` is current but whose clip timestamp is a day old and asserting no warning.

- [ ] **CI.** Add Python 3.13 to the `python` job via a matrix, so the version production runs is the version under test.

- [ ] **README.** Two additions: after a GNOME upgrade, check `gnome-extensions list --enabled | grep -i gpaste`, because a disabled extension silently kills PC→Mac sync; and a plain statement that a password copied on the Mac lands in GPaste's on-disk history on the PC and stays there, since 1Password's clearing produces an empty clipboard and empty clips are never synced — with `gpaste-client delete-history` named as the remedy.

- [ ] **Commit** as `"Add skew logging, a production Python version in CI, and operational notes"`.

---

## Self-Review

**Spec coverage.** Wire changes → Tasks 1–4; resolution rule → 5–6; the persistent store that makes the wake flow work → 7–8; reconciliation wiring → 9–10; event-source liveness → 11; skew, CI and documentation → 12. The acceptance checklist is the spec's, and is manual by nature.

**Deliberately unchanged.** `_last_seen` and its seed (Task 10 says so explicitly, because an implementer is likely to think the handshake replaces them); the frame envelope; the mismatch handling, which is what makes a half-updated deployment fail loudly.

**Not covered by any automated test, by nature.** The representation round trip through two real clipboards, and every acceptance item. Task 3's `nfd-clip` and `crlf-clip` vectors pin the byte-level half; the rest needs both machines.
