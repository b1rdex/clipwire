// Sources/clipwire/ClipStateStore.swift
import Foundation

// See StatusFile.swift's StatusConstants for why this is `static let` on an
// enum rather than a bare top-level `let`.
enum ClipStateStoreConstants {
    static let defaultPath = "~/.local/state/clipwire/clip-state.json"
}

/// What this machine PERSISTS, as against what the two machines AGREE on --
/// and they are two different contracts, which is the whole reason this type
/// exists.
///
/// `ClipState` is the WIRE type: `ClipState.decodePayload` (Freshness.swift)
/// decodes exactly it off the wire, and until v3.1 this store loaded and
/// saved that same type. That was an accident of early work rather than a
/// decision, and it came within one review of leaking. The density fix needs
/// a second, Mac-internal hash (`localSHA256` below), and the obvious place
/// to put it was `ClipState` itself -- where it would have compiled, passed
/// the whole suite, and shipped. Both sides ignore unknown keys, re-verified
/// by execution for this task rather than taken on trust: Python's
/// `decode_clip_state` returns a normal `(sha256, ts, kind)` triple for a
/// payload carrying a fourth key, and `ClipState.init(from:)` here asks its
/// keyed container for three names and never for what else is there. So a
/// value that means something only on this Mac would have travelled to the PC
/// *silently*, rather than failing loudly on the first frame.
///
/// The two have different lifetimes as well as different audiences: this file
/// outlives agent versions on one machine, while the wire is what two
/// machines agree on in a single moment. Keeping them apart is what makes
/// "this field never reaches the peer" a fact about the types rather than a
/// promise about the code. If a later tidying pass sees a one-field wrapper
/// and moves to collapse it back into `ClipState`, this paragraph is the
/// reason not to; `ClipStateStoreTests.testTheLocalHashNeverReachesTheWire`
/// is the same reason as an assertion.
struct StoredClipState: Equatable {
    /// The canonical state: exactly what this side ANNOUNCES to the peer, and
    /// the only half of this record that ever reaches the wire. After the
    /// density fix it can hold the PEER's hash for content this side is
    /// holding its own bytes of -- see `localSHA256`.
    let state: ClipState

    /// The hash of what THIS machine's clipboard actually hands back, when
    /// that is not `state.sha256`. `nil` means "the same as `state.sha256`".
    ///
    /// Optional rather than required-and-defaulted so that a store file
    /// written before v3.1 -- which cannot have the key, because it did not
    /// exist -- keeps loading and keeps behaving exactly as it did: absent is
    /// today's behaviour, and no migration step runs anywhere.
    ///
    /// The two hashes differ in exactly one state, and it is the one the
    /// density fix creates: this side received an image from the peer whose
    /// PIXELS matched what it already held, kept its own bytes (they carry
    /// the density and the colour profile GPaste dropped), and adopted the
    /// peer's hash as the canonical one so the next reconciliation resolves
    /// `doNothing` instead of pulling the degraded copy back across. The
    /// store then no longer describes what the clipboard returns -- which is
    /// precisely what this field repairs, since `resolveStartupState` below
    /// compares against `localHash` and so still sees unchanged content as
    /// unchanged. Without it, the next startup would find a hash the
    /// clipboard does not hold, stamp `now`, and log a false `clipboard
    /// changed while apart`: the defect protocol v3 closed, reopened by its
    /// own fix.
    ///
    /// Compare `localHash`, not this, against anything read from the
    /// pasteboard: this is the raw stored field and is `nil` in every
    /// ordinary case.
    let localSHA256: String?

    /// The hash of what this machine's clipboard returns -- the one to
    /// compare a fresh `pasteboard.read()` against. Falls back to the
    /// canonical hash, which is what "the store and the clipboard agree"
    /// means and what every pre-v3.1 file recorded.
    var localHash: String? { localSHA256 ?? state.sha256 }
}

/// Flat on disk -- `{"sha256": ..., "ts": ..., "kind": ..., "localSha256": ...}`
/// -- rather than the nested shape a synthesized conformance would produce
/// (`{"state": {...}, "localSHA256": ...}`). That is not cosmetic: the owner's
/// existing `clip-state.json` is a bare `ClipState`, and under a nested
/// encoding it would fail to decode, `load()` would report "nothing stored"
/// (its documented answer to a torn file), and the next announcement would
/// stamp `now` on content that is actually old and win a reconciliation it
/// should have lost -- the exact clobber the persistent store exists to
/// prevent, arriving through the migration. Pinned by
/// `testAPreV31StoreFileStillLoadsWithNoLocalHash`, which writes the old bytes
/// literally rather than round-tripping -- a round trip passes under either
/// shape, verified by making the encoding nested on both sides and watching
/// only the three flat-file tests go red while every round trip stayed green.
///
/// Both halves delegate to `ClipState`'s own `Codable` on the SAME container,
/// so the three wire fields are encoded and decoded by exactly the code that
/// encodes and decodes them for the wire -- including
/// `ClipState.init(from:)`'s kind/hash pairing rule, which is what keeps a v2
/// store file (a real hash, no `kind` key) rejected here rather than loaded as
/// "a hash of unknown kind". See that initializer's own comment.
extension StoredClipState: Codable {
    private enum CodingKeys: String, CodingKey {
        case localSHA256 = "localSha256"
    }

    init(from decoder: Decoder) throws {
        state = try ClipState(from: decoder)
        localSHA256 = try decoder.container(keyedBy: CodingKeys.self)
            .decodeIfPresent(String.self, forKey: .localSHA256)
    }

    func encode(to encoder: Encoder) throws {
        try state.encode(to: encoder)
        var container = encoder.container(keyedBy: CodingKeys.self)
        // `encodeIfPresent`: a `nil` local hash writes no key at all, so an
        // ordinary record is byte-identical to what pre-v3.1 wrote and stays
        // readable by anything that only knows `ClipState`.
        try container.encodeIfPresent(localSHA256, forKey: .localSHA256)
    }
}

/// Persists the last known `(sha256, ts)` pair to disk, so a process that
/// starts fresh -- the PC agent on every connection (spawned new by sshd
/// each time), either side after a crash or a Mac sleep/wake cycle -- can
/// still answer "how old is what I hold" for content it never personally
/// observed. Written on every locally-observed clipboard change, on every
/// applied remote clip, and by the startup announcement -- each of the four
/// through `persistClipState` (ClipStateAnnouncement.swift), which is where they are
/// enumerated. Read at startup by whoever calls `resolveStartupState` below
/// with the result, and again by `handleFrame`'s `.clipState` case.
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
    /// A `StoredClipState`, not a `ClipState`, since v3.1 -- see that type
    /// for why the store stopped being the wire type. A file written before
    /// v3.1 still loads, with `localSHA256` absent, which is what makes this
    /// a widening rather than a migration.
    func load() -> StoredClipState? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(StoredClipState.self, from: data)
    }

    /// Throws rather than swallowing, unlike `load()` above -- so a future
    /// caller has something to log, rather than this type logging or
    /// printing on its own. Follows `Status.write(to:)`'s temp-file-and-
    /// replace pattern in StatusFile.swift exactly, for the same reason:
    /// `replaceItemAt` is atomic, so `load()` above -- possibly running in
    /// a different process -- can never observe a half-written file.
    func save(_ record: StoredClipState) throws {
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
/// Which hash "the hash on disk" means is the whole reason `StoredClipState`
/// carries two, and v3.1 made the answer explicit: `localHash` -- what this
/// machine's own clipboard returns -- never `state.sha256`, which after the
/// density fix can be the PEER's hash for bytes this side is holding its own
/// version of. Comparing the canonical hash against a live read would find a
/// difference that is not a change, stamp `now` on content nobody touched,
/// and log a false `clipboard changed while apart`: the defect protocol v3
/// closed, reopened by its own fix. The two coincide for every record written
/// before v3.1 and for every ordinary record since, which is exactly why the
/// wrong one passes every test that does not construct the differing state on
/// purpose -- see `testTheSeedComparesAgainstTheLocalHashNotTheCanonicalOne`.
///
/// The matching branch returns the stored record WHOLE, rather than rebuilding
/// it around `currentHash`, for the same reason. Rebuilding is what the
/// pre-v3.1 body did (the two hashes were the same value, so it could not
/// matter), and it would now put the LOCAL hash into the canonical slot: this
/// side would announce bytes the peer has never seen, the peer would answer
/// with the copy it holds, and a frame would come back on every reconnect
/// forever instead of only the first.
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
                         stored: StoredClipState?, now: Double) -> StoredClipState {
    guard let currentHash else {
        // `localSHA256: nil` alongside the nil canonical hash, for the reason
        // the kind is nil here: a state that announces nothing has nothing
        // for a later read to be compared against either, and carrying a
        // stale local hash under a nil canonical one would make the next
        // startup measure the clipboard against content this side has already
        // declared it no longer holds.
        return StoredClipState(state: ClipState(sha256: nil, ts: now, kind: nil), localSHA256: nil)
    }
    if let stored, stored.localHash == currentHash {
        return stored
    }
    // The content changed, so whatever the two hashes used to describe is
    // gone: what the clipboard now returns IS what this side will announce,
    // and `nil` is how that is recorded -- no local hash, because there is no
    // divergence left to record.
    return StoredClipState(state: ClipState(sha256: currentHash, ts: now, kind: currentKind),
                           localSHA256: nil)
}
