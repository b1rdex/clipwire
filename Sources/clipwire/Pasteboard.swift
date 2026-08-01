// Sources/clipwire/Pasteboard.swift
import AppKit
import Foundation

protocol PasteboardReading {
    var changeCount: Int { get }

    /// The one canonical read: `(kind, bytes)` for whatever the pasteboard
    /// currently holds, or `nil` when it holds nothing this agent syncs.
    /// Every caller — the watcher's poll, the startup reconciliation
    /// (`resolveCurrentClipState`) and the reconciliation send branch — goes
    /// through this single call, so which content wins when more than one
    /// kind is on offer is decided in exactly one place (`chooseKind`)
    /// rather than reimplemented per call site. Mirrors
    /// `WaylandClipboard.read()` on the PC.
    func read() -> (kind: ClipKind, data: Data)?
}

/// The one write operation an incoming clip needs. Kept separate from
/// `PasteboardReading` (rather than folded into one protocol) because the
/// two sides are consumed by different owners: `PasteboardWatcher` only
/// ever reads, and the frame handler that applies an incoming clip
/// (`handleFrame` in main.swift) only ever writes. Splitting them means a
/// test can substitute a recording spy for the write side alone, without
/// needing to fake `changeCount`/`read` too — see `HandleFrameTests.swift`.
protocol PasteboardWriting {
    func write(kind: ClipKind, data: Data)
}

/// The narrow slice of `NSPasteboard` this file actually uses, so the
/// TIFF→PNG path and the type-selection rule are testable without touching
/// the machine's real clipboard. `NSPasteboard` is not injectable as-is:
/// there is one `general` board per login session, shared with every other
/// running app, so a test that exercised `SystemPasteboard` against it would
/// both depend on and destroy whatever the user had copied.
///
/// Six members, all of them calls `SystemPasteboard` provably makes — the
/// smaller this is, the less of AppKit a fake has to imitate convincingly.
/// `NSPasteboard` satisfies every one of them with its own existing
/// signatures, so the conformance below is empty and there is no adapter
/// layer that could itself be wrong.
///
/// `string(forType:)` earns its place next to `data(forType:)` rather than
/// being folded into it: it is what keeps bytes that are NOT valid UTF-8
/// from ever entering the text path. `NSPasteboard` returns `nil` for such
/// bytes (verified directly: `setData(Data([0xFF, 0xFE]), forType: .string)`
/// then `string(forType: .string)` is nil while `data(forType: .string)`
/// hands back `fffe`), so this read refuses them and `read()` reports
/// nothing — nothing is synced, which is the correct outcome for content
/// this protocol cannot carry.
///
/// A raw `data(forType: .string)` read would instead sync them WRONG, and
/// permanently. The bytes would be hashed as-is by `sha256Hex` for the
/// clip-state we store and announce, while the clip that actually goes out
/// is built by `String(decoding:as:UTF8.self)` (main.swift, in both
/// `wireAgent`'s `onChange` and `handleFrame`'s `.sendMine` branch), which
/// substitutes U+FFFD. The peer would then store the hash of the SUBSTITUTED
/// text, the two stores would disagree about the same clip forever, and
/// every subsequent reconnect would resolve `sendMine` and re-send it — an
/// unbounded loop, not a one-off drop.
///
/// Note what this is NOT: it is not what keeps an applied remote clip from
/// echoing back. That path is byte-identical under either implementation —
/// `handleFrame` arms `EchoGuard` with `Data(decoded.text.utf8)` and writes
/// those same bytes, so a subsequent read returns them whichever call
/// fetches them, and the digests match either way. The hazard here is
/// LOCAL content that was never valid UTF-8 to begin with. Pinned by
/// `testInvalidUTF8UnderTheStringTypeReadsAsNothing`.
protocol PasteboardBackend: AnyObject {
    var changeCount: Int { get }
    var types: [NSPasteboard.PasteboardType]? { get }
    func string(forType dataType: NSPasteboard.PasteboardType) -> String?
    func data(forType dataType: NSPasteboard.PasteboardType) -> Data?
    @discardableResult func clearContents() -> Int
    @discardableResult func setData(_ data: Data?, forType dataType: NSPasteboard.PasteboardType) -> Bool
}

extension NSPasteboard: PasteboardBackend {}

/// Which kind to sync, given the types the pasteboard is offering.
///
/// Text wins. Spreadsheets put a bitmap of the copied cells alongside the
/// text, so preferring the image would turn every copied range into a
/// picture of a table — a regression of the primary flow in exchange for the
/// new one. Screenshots and "Copy Image" carry no plain text, so they still
/// arrive as images.
///
/// Pinned against `fixtures/clipkind.json`, the same file the PC agent's
/// `choose_kind` is pinned against, so the two implementations of this one
/// rule cannot drift. That table carries a shared `expect` column (the
/// decision) and one vocabulary column per side: `types` for the
/// Wayland/X11 MIME strings `wl-paste --list-types` prints, `uti` for the
/// Uniform Type Identifiers `NSPasteboard` speaks. The split is not merely
/// cosmetic — the two sides' sets of USABLE image types genuinely differ:
/// the PC considers only `image/png`, because GPaste re-offers whatever it
/// holds as PNG among a long list of types (verified on the live machine
/// down to a JPEG reading back as valid PNG), while this side also accepts
/// `public.tiff` as a FALLBACK, for sources that offer TIFF without PNG.
/// Those are real: AppKit's own `NSImage` pasteboard writing
/// (`writeObjects([NSImage])`, what an app copying an image typically does)
/// offers exactly `public.tiff` and its legacy NeXT alias, with no PNG at
/// all — verified directly on this machine, on a private named pasteboard.
/// A translation at this boundary could not express a per-side difference
/// like that without mapping one side's types onto the other's and lying
/// about one of them.
///
/// Screenshots are NOT such a source, contrary to what an earlier version of
/// this comment (and the task brief) claimed: `screencapture -x -c` puts
/// `public.png` on the pasteboard FIRST, ahead of TIFF and six other
/// formats — verified on a real Mac. So the common case takes `readPNG()`'s
/// direct path and is never re-encoded, which is the outcome to preserve;
/// the TIFF branch is there for the `NSImage` writers, not for screenshots.
///
/// The text branch matches only `NSPasteboard.PasteboardType.string`
/// (`public.utf8-plain-text`) rather than any text-ish UTI, deliberately:
/// that is the exact type `SystemPasteboard.read()` then asks for, so
/// choosing `.text` here means a body read that can actually succeed.
/// Accepting `public.plain-text` or `public.rtf` too would only manufacture
/// a state where this function says "text" and the read that follows finds
/// nothing — AppKit already maps the legacy `NSStringPboardType` onto
/// `public.utf8-plain-text` before it ever reaches `types`.
func chooseKind(offeredTypes: [String]) -> ClipKind? {
    if offeredTypes.contains(NSPasteboard.PasteboardType.string.rawValue) { return .text }
    if offeredTypes.contains(NSPasteboard.PasteboardType.png.rawValue)
        || offeredTypes.contains(NSPasteboard.PasteboardType.tiff.rawValue) {
        return .image
    }
    return nil
}

final class SystemPasteboard: PasteboardReading, PasteboardWriting {
    private let board: PasteboardBackend
    /// Optional and defaulted to `nil` so every test that constructs a
    /// `SystemPasteboard` for its read/write behaviour compiles unchanged;
    /// only `runAgent()` passes a real one. The same idiom
    /// `PasteboardWatcher` below already uses, for the same reason.
    private let log: Log?

    /// Defaults to the real board, so `runAgent()` reads as it always did
    /// and the injection point exists only for tests.
    init(_ board: PasteboardBackend = NSPasteboard.general, log: Log? = nil) {
        self.board = board
        self.log = log
    }

    var changeCount: Int { board.changeCount }

    func read() -> (kind: ClipKind, data: Data)? {
        guard let kind = chooseKind(offeredTypes: (board.types ?? []).map(\.rawValue)) else {
            return nil
        }
        switch kind {
        case .text:
            // Empty is nothing to sync, not an empty clip — the same answer
            // `WaylandClipboard.read()` gives for an empty stdout.
            guard let string = board.string(forType: .string), !string.isEmpty else { return nil }
            return (.text, Data(string.utf8))
        case .image:
            guard let png = readPNG() else { return nil }
            return (.image, png)
        }
    }

    /// PNG as offered, otherwise a TIFF converted to PNG.
    ///
    /// The wire format is PNG (`ImagePayload` carries PNG bytes, and the PC
    /// hands `wl-copy --type image/png`), so a board offering only TIFF has
    /// to be converted here or not synced at all. PNG already on the board
    /// is taken byte-for-byte rather than round-tripped: both sides
    /// reconcile by comparing hashes, and re-encoding identical content
    /// would change its hash.
    ///
    /// Which order matters more than it looks. Screenshots offer PNG first
    /// (verified: `screencapture -x -c` puts `public.png` ahead of TIFF and
    /// six other formats), so the commonest image on this pasteboard takes
    /// the direct path and is never decoded and re-encoded — a Retina
    /// screenshot is tens of megabytes, and this read runs inside
    /// `PasteboardWatcher`'s lock. The TIFF branch is the fallback for
    /// sources that genuinely offer no PNG, of which AppKit's own
    /// `NSImage`/`writeObjects` is one (verified: `public.tiff` and its
    /// legacy NeXT alias, nothing else).
    ///
    /// A failed conversion is `nil` — never the unconverted TIFF, never a
    /// placeholder, both of which would put bytes on the wire that claim to
    /// be PNG and are not. It is logged here rather than by the caller (as
    /// the task brief specified) because `read()` returns a bare optional:
    /// at every call site, `nil` from a failed conversion is
    /// indistinguishable from `nil` for an empty pasteboard — the ordinary,
    /// uneventful case that must stay silent — and `resolveCurrentClipState`,
    /// one of those call sites, has no logger at all. Logging where the
    /// distinction still exists is the only place it can be made.
    private func readPNG() -> Data? {
        if let png = board.data(forType: .png), !png.isEmpty { return png }
        guard let tiff = board.data(forType: .tiff), !tiff.isEmpty else { return nil }
        guard let png = NSBitmapImageRep(data: tiff)?.representation(using: .png, properties: [:]),
              !png.isEmpty else {
            log?.line("could not convert the pasteboard image to PNG: dropping it")
            return nil
        }
        return png
    }

    /// `clearContents()` first, always: without it the previous item's other
    /// representations survive, so writing an incoming image over an old
    /// text clip would leave BOTH on the board — and text wins, so the very
    /// next read would return the stale text instead of the image just
    /// applied.
    ///
    /// No read-back afterwards, and deliberately not: `NSPasteboard` returns
    /// the bytes it was given, and nothing else takes ownership of the
    /// selection here. The PC side's `_write_clip` does re-read after
    /// writing, but that exists for a Wayland-side fact — GPaste takes over
    /// the selection and RE-ENCODES images, so the bytes it serves back are
    /// not the bytes `wl-copy` was handed, and its hash has to be recomputed
    /// from what the clipboard actually ended up holding. Nothing on this
    /// side re-encodes anything, so a symmetric read-back here would add a
    /// race (the watcher polls the same board) in exchange for no
    /// information. Do not add one for symmetry.
    func write(kind: ClipKind, data: Data) {
        board.clearContents()
        // `setData` rather than `setString` for text: the two are equivalent
        // for `public.utf8-plain-text` (both write the UTF-8 bytes), and one
        // call covers both kinds, which keeps `PasteboardBackend` a member
        // smaller.
        board.setData(data, forType: kind == .text ? .string : .png)
    }
}

/// Polls NSPasteboard.changeCount. The comparison is an integer read in-process,
/// so a sub-second interval costs nothing — unlike the PC side, which has to
/// fork a process and read the whole clipboard.
///
/// Two actors touch this watcher's state: its own `poll()`, invoked on the
/// timer's background queue, and `noteWrittenLocally(kind:payload:)`, called by the
/// channel's frame handler (a later task) from a different thread when it
/// writes an incoming clip to the pasteboard. Task 11 found the PC analogue
/// of this — the watcher thread and the main thread shared echo-suppression
/// state with no lock, and the watcher read the clipboard *before* consuming
/// the suppression, so two back-to-back incoming clips made it compare a
/// stale value and echo one of them back to the peer.
///
/// `stateLock` guards `echo` *and* `lastChangeCount` together, and covers the
/// entire [read changeCount -> compare -> read text -> consult echo] sequence
/// in `poll()` as one critical section, matched by `noteWrittenLocally`
/// taking the same lock for its arm. This is stricter than it looks like it
/// needs to be, and deliberately so: an earlier version of this fix moved
/// only `echo` under the lock and left `changeCount` read (and
/// `lastChangeCount` written) beforehand, unlocked. That leaves a gap where a
/// second incoming clip can arrive — armed *and* written — between this
/// poll's changeCount read and its lock acquisition. The read-and-compare
/// that follows still lands on consistent data (it reads whatever is
/// actually current and correctly matches it against `echo`), but
/// `lastChangeCount` gets recorded one generation behind what was actually
/// consumed. The next poll then sees a changeCount it hasn't recorded,
/// treats already-suppressed content as a fresh unobserved change, finds
/// `echo` already spent, and resends it. Reading `changeCount` under the
/// same lock as the rest closes this: whatever generation `poll()` records
/// is provably the one it just read text and consulted `echo` for, because
/// nothing else can touch `echo` in between. Unlike the PC side, none of
/// this needs a generation counter — but NOT, since Task 8, because the read
/// is always cheap. `SystemPasteboard.read()` is an in-process NSPasteboard
/// call rather than a forked `wl-paste`, and for text it is as fast as it
/// ever was; for an IMAGE it now also runs `NSBitmapImageRep` decode plus PNG
/// re-encode, which on a Retina screenshot is genuinely slow — comparable to
/// the wl-paste call the PC side had to dodge — and `pollLocked()` runs it
/// inside `stateLock`, so `noteWrittenLocally` can block for that long.
/// That cost is bounded and accepted rather than overlooked: the
/// `changeCount` guard returns BEFORE the read on an unchanged board, so a
/// conversion happens at most once per clipboard change, never once per tick,
/// however long an image sits there. And a generation counter would not help
/// anyway — the reason it is unnecessary is a correctness argument, not a
/// latency one: a counter keyed off `echo`'s own arm count would not catch
/// this specific gap at all, since the arm that matters here can complete
/// before `poll()` starts, leaving the counter unchanged across the whole
/// call. A single lock around the full sequence is sufficient; if the
/// image-read latency ever becomes a problem, the fix is to read outside the
/// lock and re-validate, not to reach for a counter.
///
/// (The converted PNG is then discarded, because `pollLocked()` keeps text
/// only — see its own comment. That waste ends with Task 11, which is what
/// makes an image observation worth sending.)
///
/// This lock is still not a complete contract on its own: it says nothing
/// about the ORDER in which the future frame handler writes to the
/// pasteboard versus calling `noteWrittenLocally`, because `PasteboardWatcher`
/// does not perform that write. For the suppression to be armed before its
/// own change becomes observable, that caller must call `noteWrittenLocally`
/// *before* writing to the pasteboard, not after — arm-then-write, which is
/// also already the convention the Python agent's `_write_clip` follows on
/// the PC side. Ordering alone (without this lock) would still race, since
/// `noteWrittenLocally` itself is not atomic with `poll()`'s read; this lock
/// alone (with the wrong order) would still let a poll observe a
/// written-but-unarmed change and echo it. Both are required together; the
/// second is this class's responsibility, the first belongs to the task
/// that builds the frame handler.
final class PasteboardWatcher {
    var onChange: ((Data, Double) -> Void)?

    private let pasteboard: PasteboardReading
    private let pollInterval: TimeInterval
    private var lastChangeCount: Int
    private var echo = EchoGuard()
    private var timer: DispatchSourceTimer?
    // Optional, and defaulted to nil, so every existing call site (this
    // class predates any need to log) keeps compiling unchanged; only
    // `runAgent()` passes a real one. `Log` is `Sendable` and `line(_:)`
    // only enqueues onto its own serial queue, so calling it from inside
    // `pollLocked()`'s critical section below is safe and does not hold
    // `stateLock` for any meaningful extra time.
    private let log: Log?

    // Guards `echo` and `lastChangeCount` together — see the class doc
    // comment for why both, not just `echo`, need to be under this lock.
    private let stateLock = NSLock()

    init(pasteboard: PasteboardReading, pollInterval: TimeInterval, log: Log? = nil) {
        self.pasteboard = pasteboard
        self.pollInterval = pollInterval
        self.lastChangeCount = pasteboard.changeCount
        self.log = log
    }

    func noteWrittenLocally(kind: ClipKind, payload: Data) {
        stateLock.lock()
        echo.noteWrittenLocally(kind: kind, payload: payload)
        stateLock.unlock()
    }

    func poll() {
        guard let (toSend, observedAt) = pollLocked() else { return }
        // Invoked after the lock is released, both because it can be slow
        // (it hands off to the channel) and because a callback that
        // re-entered the watcher while the lock was still held would
        // deadlock against a non-reentrant NSLock.
        onChange?(toSend, observedAt)
    }

    /// The entire read-and-decide sequence, as one critical section shared
    /// with `noteWrittenLocally`. `defer` releases the lock on every path,
    /// including the early "nothing changed" return, so a raised guard can
    /// never leak a held lock into the next `noteWrittenLocally` call.
    ///
    /// The timestamp is read here, under the same lock as the text it is
    /// paired with, because it must be the moment of OBSERVATION -- this
    /// poll's read -- not the moment `onChange` eventually runs, which is
    /// deliberately invoked outside the lock and can lag behind it. This
    /// timestamp becomes the outgoing clip's `ts`; a receiving peer stores
    /// it unchanged (see `handleFrame`'s `.clip` case), so inflating it here
    /// would misstate how old the content actually is everywhere downstream.
    private func pollLocked() -> (Data, Double)? {
        stateLock.lock()
        defer { stateLock.unlock() }

        let current = pasteboard.changeCount
        guard current != lastChangeCount else { return nil }
        // Record the new count before any early return, so non-text content
        // cannot wedge the watcher into rescanning the same item forever —
        // and, per the class doc comment, record the generation this same
        // locked call is about to read and consult echo for, not one
        // observed earlier and now stale.
        lastChangeCount = current

        // Text or nothing. Since Task 8 made the read kind-aware, an image
        // on the pasteboard comes back as a real `(.image, bytes)` pair
        // instead of nil, and `onChange`'s consumer (`wireAgent`) wraps
        // whatever it is handed in a `ClipPayload` — the TEXT codec — via
        // `String(decoding:as:UTF8.self)`. Emitting an image here would
        // therefore put a mojibake transliteration of a PNG on the wire and
        // into both persistent stores. Keeping this text-only is a scope
        // boundary, not an oversight: syncing a local image change is later
        // work (Task 11), which needs the image send, apply and announce
        // wiring together rather than one call site at a time. Until then an
        // image observation is skipped exactly as it always was — the
        // difference is that it is now skipped on purpose, pinned by
        // `testAnImageOnThePasteboardIsNotEmittedAsText`. The PC agent's
        // `_local_change` carries the same guard for the same reason.
        guard let read = pasteboard.read(), read.kind == .text, !read.data.isEmpty else { return nil }
        let text = read.data
        // `wireAgent` wraps this text in `ClipPayload(ts:text:)` before it
        // ever reaches the wire, adding an 8-byte prefix -- so the bound
        // here must leave room for it. Checking `text.count` alone (exact
        // before Task 9, when this text WAS the frame payload) would let
        // text at exactly the cap encode to a payload 8 bytes over the
        // TEXT limit. `maxTextBytes`, not the (larger) `maxPayloadBytes`
        // the decoder enforces: since Task 4 the two are separate
        // constants, and content between the two would still fit inside a
        // frame -- this guard is the text-specific policy limit, not a
        // wire-safety necessity.
        guard text.count + ClipPayloadConstants.timestampBytes <= FrameConstants.maxTextBytes else {
            // Logged so a user whose large local copy never reaches the
            // peer has something to look at, matching the Python agent's
            // existing "skipping a clip of N bytes" line for the same limit.
            log?.line("skipping a clip of \(text.count) bytes: over the text limit")
            return nil
        }
        guard echo.shouldSend(kind: .text, payload: text) else { return nil }
        return (text, Date().timeIntervalSince1970)
    }

    func start() {
        // Cancelling any previous timer before installing a new one keeps a
        // second start() from leaking the first timer as an orphaned,
        // still-firing source.
        timer?.cancel()
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
