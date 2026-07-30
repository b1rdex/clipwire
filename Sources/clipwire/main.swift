// Sources/clipwire/main.swift
import Foundation

// The real CLI — run / status / init / install — lands in a later task.
// This file exists now because an executableTarget with no main.swift and
// no @main has no entry point and fails at link time, which would leave CI
// red for several commits.
FileHandle.standardError.write(Data("clipwire: the CLI is not implemented yet\n".utf8))
exit(2)
