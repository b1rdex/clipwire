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
