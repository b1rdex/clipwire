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
