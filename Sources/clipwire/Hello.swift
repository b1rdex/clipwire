// Sources/clipwire/Hello.swift
import Foundation

enum ProtocolConstants {
    static let version = 3
    static let agentVersion = "0.1.0"

    static var helloPayload: Data {
        (try? JSONEncoder().encode(
            HelloPayload(version: version, agent: agentVersion,
                         sentAt: Date().timeIntervalSince1970)
        )) ?? Data()
    }
}

/// Wire shape of a hello frame's JSON payload:
/// `{"protocol": <int>, "agent": "<version>", "sent_at": <epoch seconds>}`.
/// `version` maps to the wire key `protocol` (a Swift keyword) via `CodingKeys`,
/// the same pattern `Config` already uses for its own snake_case wire keys.
private struct HelloPayload: Codable {
    let version: Int
    let agent: String?
    // Optional, like `agent`: we always populate it in what WE build (see
    // `helloPayload` above), but decoding stays tolerant of a peer that
    // omits it -- an old v1 peer, or any hand-built test payload -- so
    // `decodeHello` keeps reporting the real version mismatch instead of
    // degrading to "malformed hello" the moment a v1 peer's payload lacks a
    // key v2 introduced. `skewLogLine` reads it back out and treats a
    // missing value as "not measurable", never as an error.
    //
    // That tolerance is narrower than "Optional" suggests, and it is the one
    // place the two sides deliberately disagree -- written down here because
    // it reads as a bug to anyone who finds it from one side only. An absent
    // key and an explicit `null` are the ONLY two shapes both sides accept.
    // Every other shape fails the WHOLE payload here, so the Mac logs
    // "malformed hello from peer" and records a mismatch: a string or a bool
    // as `typeMismatch`, and `NaN`, `Infinity`, `1e309` and an integer too
    // large for a Double as `dataCorrupted` (probed against this exact
    // struct). `agent/clipwire-agent.py`'s `skew_log_line` ignores all of
    // them in silence and syncs on. Left as is deliberately: making this
    // field lenient means a custom `init(from:)`, which changes what counts
    // as a decodable hello -- and the `NaN` case already had this outcome
    // before skew measurement existed, so this is the blessed shape rather
    // than a new divergence.
    let sentAt: Double?

    enum CodingKeys: String, CodingKey {
        case version = "protocol"
        case agent
        case sentAt = "sent_at"
    }
}

/// Parses a peer's hello payload. Returns `nil` if the JSON cannot be
/// decoded at all -- `runAgent()`'s `.hello` case treats that the same as
/// a version mismatch, since an undecodable declaration is not something
/// this side can confirm as compatible.
func decodeHello(_ payload: Data) -> (version: Int, agent: String?, sentAt: Double?)? {
    guard let decoded = try? JSONDecoder().decode(HelloPayload.self, from: payload) else {
        return nil
    }
    return (decoded.version, decoded.agent, decoded.sentAt)
}

enum SkewConstants {
    // Above this, the two clocks disagree by enough that a freshness
    // comparison between them can pick the wrong side. Compared with `>`:
    // five seconds exactly is the boundary, not a warning. Written into the
    // message below as a literal rather than interpolated, so the Python
    // twin of that line does not have to reproduce a second float-formatting
    // bridge byte for byte; the tests pin the literal against this constant.
    static let warnSeconds: Double = 5
}

/// The peer's clock offset from ours, as a log line -- or `nil` when it
/// cannot be measured.
///
/// Measures `abs(now - sentAt)` from the HELLO, never the age of a clip: a
/// clip legitimately copied this morning is hours old, so warning on that
/// would fire on nearly every handshake and teach everyone to ignore the log.
///
/// A missing or non-finite `sentAt` means the same thing -- skew is not
/// measurable -- and returns `nil`. An unmeasurable peer clock is not a
/// protocol violation, so this neither warns nor reports a mismatch.
///
/// Mirrors `agent/clipwire-agent.py`'s `skew_log_line` one branch at a time,
/// including the exact text of both outcomes, the way the two "over the frame
/// cap" lines already match. `String(format:)` with no explicit locale is
/// non-localized, so `%.1f` writes the same "." separator Python's `%` does,
/// on any machine.
///
/// The `isFinite` guard is the mirror of the Python side's, where it is
/// load-bearing: `json.loads` accepts the bare literals `NaN`/`Infinity`, and
/// `abs(now - nan) > 5.0` is `False`, so an unguarded implementation there
/// logs `peer clock skew nan` and silently never warns. Foundation's
/// `JSONDecoder` rejects those tokens outright, so on this side such a payload
/// never gets past `decodeHello` -- the guard costs one clause and keeps the
/// two functions readable as one formula. `HelloPayload.sentAt`'s own comment
/// lists every shape where that rejection makes the two sides diverge; it is
/// wider than the non-finite literals alone.
func skewLogLine(peerSentAt: Double?, now: Double) -> String? {
    guard let sentAt = peerSentAt, sentAt.isFinite else { return nil }
    let skew = abs(now - sentAt)
    if skew > SkewConstants.warnSeconds {
        return String(format: "peer clock skew %.1fs — over 5s, check the clock on both machines",
                      skew)
    }
    return String(format: "peer clock skew %.1fs", skew)
}
