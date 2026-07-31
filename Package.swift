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
