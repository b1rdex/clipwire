// Sources/clipwire/ClipStateStore.swift
import Foundation

// See StatusFile.swift's StatusConstants for why this is `static let` on an
// enum rather than a bare top-level `let`.
enum ClipStateStoreConstants {
    static let defaultPath = "~/.local/state/clipwire/clip-state.json"
}

/// Persists the last known `(sha256, ts)` pair to disk, so a process that
/// starts fresh -- the PC agent on every connection (spawned new by sshd
/// each time), either side after a crash or a Mac sleep/wake cycle -- can
/// still answer "how old is what I hold" for content it never personally
/// observed. Written on every locally-observed clipboard change, on every
/// applied remote clip, and by the startup announcement -- each of the four
/// through `persistClipState` (ClipStateAnnouncement.swift), which is where
/// they are enumerated. Read at startup by whoever calls `resolveStartupState`
/// below with the result, and again by `handleFrame`'s `.clipState` case.
///
/// It stores a `ClipState` -- the WIRE type -- and that is a statement about
/// this machine rather than a shortcut. v3.1 wrapped it in a `StoredClipState`
/// carrying a second, Mac-internal hash: the density fix kept this side's own
/// bytes whenever an incoming image decoded to the same pixels, so the record
/// had to say both what this side ANNOUNCES and what its clipboard RETURNS.
/// v3.2 deleted that fix -- it never fired on real hardware, because GPaste
/// re-encodes through the embedded ICC profile and the samples genuinely move
/// (398,267 of 614,400 bytes on a measured screenshot) -- and with it the only
/// value this machine held back from the wire. What remains is `(sha256, ts,
/// kind)`, every field of which is announced, so there is nothing left for a
/// wrapper to keep apart. The PC is where a store field now outlives a single
/// exchange, and its `origin` travels too.
///
/// The wrapper encoded FLAT, delegating to `ClipState`'s own `Codable` on the
/// same container, so a record written by v3.1 is byte-identical to one
/// written here and nothing has to migrate in either direction. Pinned by
/// `testAStoreFileWrittenAsABareClipStateStillLoads`, which writes the bytes
/// literally rather than round-tripping: a round trip passes under a nested
/// encoding too.
struct ClipStateStore {
    let url: URL

    // Guards `save()`'s write-then-replace sequence. Task 9 added this
    // type's first two concurrent callers -- the pasteboard watcher's timer
    // thread (a local change) and the channel's decode thread (an applied
    // remote clip) -- so two `save()` calls can now genuinely overlap. Both
    // write the SAME fixed temp path (`url.appendingPathExtension("tmp")`)
    // with a plain, non-atomic `Data.write(to:)`; if one thread's write
    // interleaves with another's, whichever `replaceItemAt` runs next moves
    // a corrupt file into place, `load()` then reads it as "nothing stored"
    // (its documented, and otherwise correct, response to a torn file), and
    // the NEXT connection's `announceClipState` resolves `stored: nil` and
    // stamps `now` on content that is actually old -- inflating its age and
    // winning a reconciliation it should have lost. The exact clobber this
    // design exists to prevent, arriving by a different door.
    //
    // A lock (rather than a unique temp path per write) keeps every
    // existing caller unchanged and preserves the fixed name's self-cleaning
    // property: a process that crashes mid-write leaves at most one stray
    // `.tmp` file, always overwritten by the next save, rather than a
    // per-write name that would need its own cleanup if ever abandoned.
    // `NSLock` (a class, hence `Sendable`) as a stored `let` on this struct
    // means every copy of a given `ClipStateStore` instance shares the SAME
    // lock -- exactly what's needed here, since `wireAgent` constructs one
    // instance and both concurrent call sites capture copies of it.
    //
    // `load()` needs no lock of its own: `replaceItemAt`'s rename is atomic
    // at the OS level, so once this lock has kept the temp file itself from
    // being torn, any reader of `url` sees either the complete old file or
    // the complete new one, never a mix -- the entire point of the
    // temp-file-and-replace pattern in the first place.
    private let lock = NSLock()

    init(path: String) {
        url = URL(fileURLWithPath: expandTilde(path))
    }

    /// `nil` covers three different failure reasons identically, on
    /// purpose: no file has ever been written, the file exists but cannot
    /// be read (permissions, or -- as this suite covers directly -- not a
    /// regular file at all), and the file exists and is readable but does
    /// not decode as a `ClipState` (a write torn by a crash mid-rename, or
    /// anything else malformed). `resolveStartupState`'s "nothing stored"
    /// branch already treats all three identically, so collapsing them
    /// here rather than throwing keeps that caller from having to
    /// distinguish reasons it would handle the same way regardless.
    /// Mirrors `Status.read`'s use of `try?` across the same two failure
    /// points in StatusFile.swift.
    ///
    /// Decoded by `ClipState.init(from:)`, the same initializer the wire uses,
    /// which is what keeps a v2 store file (a real hash, no `kind` key)
    /// rejected here rather than loaded as "a hash of unknown kind" -- see
    /// that initializer's own comment. A file written by v3.1 loads unchanged
    /// too: its extra `localSha256` key is one the keyed container never asks
    /// for. (No such file exists in the wild, since the fix that wrote it
    /// never fired, but the decode costs nothing and saying so is cheaper
    /// than a migration.)
    func load() -> ClipState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(ClipState.self, from: data)
    }

    /// Throws rather than swallowing, unlike `load()` above -- so a future
    /// caller has something to log, rather than this type logging or
    /// printing on its own. Follows `Status.write(to:)`'s temp-file-and-
    /// replace pattern in StatusFile.swift exactly, for the same reason:
    /// `replaceItemAt` is atomic, so `load()` above -- possibly running in
    /// a different process -- can never observe a half-written file.
    func save(_ record: ClipState) throws {
        lock.lock()
        defer { lock.unlock() }
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let tmp = url.appendingPathExtension("tmp")
        try JSONEncoder().encode(record).write(to: tmp)
        _ = try FileManager.default.replaceItemAt(url, withItemAt: tmp)
    }
}

/// The judgement that makes the wake flow work. `currentHash` and
/// `currentKind` are the pasteboard's hash and kind *right now*, at
/// startup -- both produced by the same `pasteboard.read()` call, never
/// derived independently; `stored` is whatever `ClipStateStore.load()`
/// last returned; `now` is the caller's clock.
///
/// A timestamp can come from three places, in precedence order: a local
/// change this process watched happen, a clip received from the peer
/// (carrying the peer's own timestamp, stored unchanged), and -- the case
/// this function exists for -- content that predates this process
/// entirely. The PC agent is spawned fresh by sshd on every connection, and
/// the Mac's channel drops on every sleep/wake cycle, so "predates this
/// process" is not an edge case; it is the ordinary shape of a clip copied
/// on one side while the other slept or was disconnected.
///
/// If the hash on disk matches what the clipboard holds now, the content
/// has not changed since it was last recorded, so the *stored* timestamp
/// and the *stored* kind -- not `currentKind` -- are the real age and kind
/// of that content, and are returned unchanged: unchanged content did not
/// change what kind of content it is either, and the stored value is the
/// one this side already announced to a peer, possibly on an earlier
/// connection. Returning `now` here instead would make every such clip look
/// freshly copied, winning it every reconciliation and clobbering the peer
/// systematically. If the hashes differ, or nothing was ever stored, the
/// content changed (or appeared) while nothing was watching, and only `now`
/// is honest about its age -- `currentKind` is equally honest about what it
/// is, since it came from that exact same read, and is returned as-is
/// rather than guessed.
///
/// There is one hash on disk again, and so no question of which one this
/// compares. v3.1's store carried a second, Mac-internal hash and this
/// comparison had to name it explicitly; v3.2 deleted that field with the
/// density fix it existed for, so `sha256` is both what this side announces
/// and what its clipboard returns. The match branch still returns `stored`
/// rather than rebuilding a record around `currentHash` -- but with the hashes
/// equal on that branch by definition, what it is now saying is that the
/// stored `ts` and `kind` survive, and that any field a later version adds
/// survives with them. On the PC, where the store carries an `origin` the Mac
/// never records, that second half is load-bearing and `resolve_startup_state`
/// says so itself.
///
/// Before Task 8 made `pasteboard.read()` kind-aware, this parameter did
/// not exist and this branch hardcoded `.text`: `resolveCurrentClipState`
/// (ClipStateAnnouncement.swift), this function's only non-test caller, could only ever
/// derive `currentHash` from a text read, so there was no independent
/// "current kind" to thread through yet. Task 6's own note on this function
/// named the exact failure a purely mechanical fix to the CALLER (not to
/// this signature) would have left behind: once the read returned a
/// `(kind:, data:)` pair, a caller that computed `currentHash =
/// sha256Hex(read.data)` while quietly dropping the kind would compile
/// clean and pass the whole suite, silently fabricating `.text` for a PNG
/// hash right here -- one frame below the line that actually changed, with
/// no call-site error to point at it, since a same-arity caller changing
/// its body is invisible to every test that only checks THIS function's
/// behaviour. Fixed by threading the kind through as a real parameter
/// instead of leaving it something only the caller could derive: dropping
/// it is now a compile error at every call site, everywhere it is tested,
/// rather than a wrong answer only the one production call site would ever
/// produce. See `resolveCurrentClipState`'s own doc comment for the other
/// half -- it is what derives `currentKind` from the read's pair.
///
/// A `nil` currentHash (the pasteboard empty, unreadable, or holding content
/// over its kind's limit -- see `resolveCurrentClipState`, which is what turns
/// all three into this one value) always
/// wins over whatever is on disk, regardless of what was previously stored:
/// `resolveFreshness` never compares timestamps when either side's hash is
/// `nil`, so the timestamp attached here is never actually read. Its kind is
/// a literal `nil` too -- not `currentKind` -- matching the nil-iff-nil rule
/// `ClipState.init(from:)` enforces on the wire: a caller reporting a `nil`
/// hash (nothing announceable on the pasteboard, whichever of the three
/// reasons it was) has no real kind to go with it either, and `ClipState`'s memberwise
/// initializer does not enforce that rule, so a leak here would reach the
/// wire and be rejected by the peer's decoder rather than by anything local.
func resolveStartupState(currentHash: String?, currentKind: ClipKind?,
                         stored: ClipState?, now: Double) -> ClipState {
    guard let currentHash else {
        return ClipState(sha256: nil, ts: now, kind: nil)
    }
    if let stored, stored.sha256 == currentHash {
        return stored
    }
    // The content changed, so whatever was recorded is gone: what the
    // clipboard now returns IS what this side will announce.
    return ClipState(sha256: currentHash, ts: now, kind: currentKind)
}
