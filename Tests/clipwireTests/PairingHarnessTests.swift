// Tests/clipwireTests/PairingHarnessTests.swift
//
// The first test in this project's history that runs BOTH implementations
// against each other.
//
// Every defect clipwire ever shipped -- the whitespace trim, GPaste's image
// re-encode, the leaked gdbus child, the safety net declaring a healthy source
// dead, the retina doubling -- was found by running it on the owner's
// machines, never by a test, because the Swift and Python halves had never
// once executed together: each suite drives its own side against scripted
// pipes, and a scripted pipe agrees with whatever the side under test
// believes. This file closes that.
//
// WHAT ACTUALLY RUNS. `Channel` spawns `python3 agent/clipwire-agent.py`
// through the seam v3.1 task 1 opened (an injectable executable path and
// argv, defaulted to `/usr/bin/ssh` + `sshArguments`, so production is
// untouched). The agent finds `Tests/fakes/wl-copy`, `wl-paste` and `gdbus`
// ahead of the real tools on `PATH` and forks them BY NAME, exactly as it
// forks the real ones on the PC. So what crosses is a real frame, over a real
// pipe, between two real implementations: real argv, real EOF, real SIGPIPE.
// The only drawings in the picture are the Linux clipboard (which cannot
// exist on a Mac) and `NSPasteboard` (whose own translation layer is pinned
// separately, in PasteboardTests).
//
// IT ASSERTS ITS OWN WORLD BEFORE IT TESTS ANYTHING, AND REFUSES. See
// `PairingHarness.start()`. A harness that degrades to a weaker run and
// reports success is worse than one that fails: this release's own pipe-write
// experiment did exactly that, and reached a correct conclusion from invalid
// evidence. The precondition that earns its keep is the third one -- that the
// agent is on the GPASTE EVENT PATH, not polling. Measured during task 3:
// with a `gdbus` that answers only `monitor`, the agent logs "GPaste
// unavailable, falling back to polling every 1.0s" AND THE CLIP STILL
// CROSSES. Every clip assertion below would pass while the pump, the worker
// and the safety net -- where v3's two production defects actually lived --
// never ran once. That blindness would arrive as a green suite, which is the
// one failure mode this file exists to remove.
// `testTheHarnessRefusesAnAgentOnThePollingFallback` keeps that measurement
// as a test rather than as a memory.
//
// IT ALSO STAGES RECONNECTS, since v3.1 task 7. A reconnect is the event
// freshness reconciliation exists for, and the only way to observe the density
// bug's convergence: `reconnect()` drops the channel and dials again, which
// spawns a brand-new agent exactly as sshd does. Every precondition above is
// re-asserted for each connection, and each is a COUNT against a baseline
// rather than a `contains` -- the agent's log and the fakes' invocation log
// both accumulate, so a search that was right for the first connection answers
// instantly, on the previous agent's output, for the second. Measured, with the
// second dial removed on purpose: the counted form refuses in 15s at "a HELLO
// from the agent", naming what is missing; the `contains` form satisfies all
// four preconditions on the dead connection's output and fails 41s later at a
// clip assertion that can only report a symptom.
import XCTest
@testable import clipwire

final class PairingHarnessTests: XCTestCase {
    /// Held rather than passed to `addTeardownBlock` so that a test which
    /// fails mid-way still hangs up the agent: the block form would need the
    /// harness to be `Sendable`, which it is not and has no reason to be.
    private var harness: PairingHarness?

    override func tearDown() {
        harness?.stop()
        harness = nil
        super.tearDown()
    }

    private func connected(eventSource: PairingHarness.EventSource = .gpaste,
                           substituting: Bool = false,
                           tierSeconds: (fast: Double?, slow: Double?, safetyNet: Double?)
                               = (nil, nil, nil))
    throws -> PairingHarness {
        let harness = try PairingHarness(eventSource: eventSource, substituting: substituting,
                                         tierSeconds: tierSeconds)
        self.harness = harness
        try harness.start()
        return harness
    }

    // MARK: - a clip crosses the loop: both kinds, both directions

    /// Mac -> PC, text. The user copies on the Mac; `PasteboardWatcher`
    /// observes it, `handleLocalChange` encodes a `.clip` frame, the agent
    /// decodes it and hands it to `wl-copy`.
    ///
    /// Non-ASCII on purpose: the text codec keeps bytes as bytes on both
    /// sides, and a UTF-8 round trip through Swift's `String`, the wire, and
    /// Python's `bytes` is exactly the sort of thing that survives two
    /// separate unit suites and fails between them.
    func testTextCopiedOnTheMacReachesThePCsClipboard() throws {
        let harness = try connected()

        harness.pasteboard.set("copied on the Mac ✓")

        try harness.waitForThePCsClipboard(toHold: Data("copied on the Mac ✓".utf8),
                                           offeredAs: "text/plain;charset=utf-8")
    }

    /// PC -> Mac, text. Nobody writes through `wl-copy` here: the harness
    /// changes the shared clipboard state directly, which is what a person
    /// copying on the PC does. The fake `gdbus` emits an `Update` for any
    /// change by anyone, so this direction exists at all -- an `Update` per
    /// agent write would have covered only the echo direction.
    func testTextCopiedOnThePCReachesTheMacsPasteboard() throws {
        let harness = try connected()

        try harness.copyOnThePC(text: "copied on the PC ✓")

        try harness.waitForTheMacsPasteboard(toHold: .text,
                                             Data("copied on the PC ✓".utf8))
    }

    /// Mac -> PC, image: a `.imageClip` frame (type 0x03), the codec that
    /// carries a PNG rather than text.
    ///
    /// Measured, not assumed: sending this as a `.clip` instead fails here
    /// AND fails three existing tests in HandleFrameTests, which pin the
    /// frame type directly. What this one adds is the consequence rather than
    /// the mistake -- the failure names `wl-copy --type
    /// text/plain;charset=utf-8 <- 95 bytes` in the fakes' invocation log and
    /// a PNG offered under five text MIME types on the peer's clipboard,
    /// which is what the user would actually have been left holding.
    func testAnImageCopiedOnTheMacReachesThePCsClipboard() throws {
        let harness = try connected()

        harness.pasteboard.setImage(PairingHarness.png)

        try harness.waitForThePCsClipboard(toHold: PairingHarness.png, offeredAs: "image/png")
    }

    /// PC -> Mac, image. The last of the four legs, and the one that needs
    /// every part of the event path at once: the fake `gdbus`'s `Update`, the
    /// agent's pump, its worker thread, its two-call read (`--list-types`,
    /// then the body), and `handleFrame`'s `.imageClip` case.
    func testAnImageCopiedOnThePCReachesTheMacsPasteboard() throws {
        let harness = try connected()

        try harness.copyOnThePC(png: PairingHarness.png)

        try harness.waitForTheMacsPasteboard(toHold: .image, PairingHarness.png)
    }

    // MARK: - the density bug, in the loop that used to produce it

    /// The bug this whole release exists for, run end to end against the two
    /// real implementations -- and, since v3.2's Task 6b connected the rule
    /// to the decision, the fix rather than the defect.
    ///
    /// WHAT USED TO HAPPEN. A retina screenshot copied on the Mac carries
    /// `pHYs` -- 160x120 pixels that `NSImage` displays at 80x60. It reaches
    /// the PC, GPaste takes the selection over and re-offers something else,
    /// and the PC hashes WHAT IT READ BACK, which is correct and is not what
    /// this test questions. The PC therefore held a different hash at a later
    /// timestamp, won the next reconnect, and handed the Mac back a copy of
    /// its own screenshot with the density gone -- which then pasted at
    /// 160x120. Measured on the owner's machines: 259 bytes in, 632 back,
    /// displaying at double size after one reconnect. Measured here, before
    /// the wiring: exactly one frame from the PC on the first reconnect,
    /// which is the frame the counts below now assert is absent.
    ///
    /// WHY TWO EARLIER FIXES DID NOT. v3.1 tried comparing decoded pixels:
    /// if the peer's image has the same pixels as the local one, keep the
    /// local bytes, which are the ones carrying the metadata. That comparison
    /// was tuned against a fake clipboard whose substitution preserved samples
    /// by construction, and it passed here. On the real machines it never
    /// fires -- GPaste applies the image's embedded ICC profile as it loads it
    /// and writes the result untagged, so the samples move: 398,267 of 614,400
    /// bytes, max delta 20, on a 480x320 screenshot with the Mac's original
    /// captured before it travelled. Both strategies and their measurements
    /// are tabulated in the v3.2 provenance design. Every content comparison
    /// INFERS provenance, lossily, from a fact the PC knew directly and threw
    /// away: it wrote the Mac's bytes and read different ones back with nobody
    /// touching the clipboard in between. So the PC now announces the `origin`
    /// of what it holds, `resolveProvenance` reads it, and neither side
    /// deduces anything.
    ///
    /// WHAT THE HARNESS STAGES. The fake clipboard's substituting mode,
    /// deliberately HARSHER than the real transformation: it drops the
    /// ancillary chunks AND moves every pixel sample, so no comparison of image
    /// content can pass it. Measured on this fixture: 95 bytes in, 84 out,
    /// `IHDR pHYs IDAT IEND` becoming `IHDR IDAT IEND`, and all 16 bytes of the
    /// decoded 2x2 RGBA differing with a max delta of 236. That asymmetry is
    /// the design and not an accident -- a fake KINDER than the world is
    /// precisely what let the fix ship inert while this file stayed green, and
    /// a fake harsher than it costs only false alarms. See SUBSTITUTION in
    /// `Tests/fakes/fake_clipboard.py`, and `ReencodeTests.test_every_pixel_moves`
    /// in `Tests/fakes/test_fakes.py`, which is where that property is now
    /// pinned exclusively -- and pinned harder than here, as equality against
    /// the exact keystream rather than as a reported difference. This side used
    /// to read it off `imagePixelDifference`'s log line; that decoder went with
    /// the comparison it served, so nothing in the SWIFT suite witnesses the
    /// sample movement any more. What this test can still see is that the bytes
    /// came back different at all, asserted below against the fakes' own
    /// invocation log. Provenance is indifferent to how far they moved, which
    /// is exactly why it survives a transformation no comparison could.
    ///
    /// WHY TWO RECONNECTS, and this is the one that catches an inert fix. The
    /// origin is recorded by the agent that WATCHED the substitution, and
    /// `sshd` kills that process at the hang-up -- so the first reconnect can
    /// pass on a value still in a dead process's memory only if it was written
    /// to disk, and the SECOND runs entirely off what the first one persisted.
    /// A `resolve_startup_state` that rebuilt its matched record around the
    /// current hash -- correct on all three fields anyone thinks to check,
    /// dropping the fourth -- fails here and only here. The second reconnect
    /// also runs the startup seed against the store the first left behind,
    /// which is what makes a false `clipboard changed while apart` visible.
    ///
    /// SO THE ASSERTIONS SAY, IN ORDER:
    ///
    /// - both sides resolve `doNothing` on BOTH reconnects, with zero clip
    ///   frames in either direction -> the suppression fires and is mutual.
    ///   Two counts, not one, because the two directions have different
    ///   witnesses: frames arriving here, and `wl-copy` invocations there;
    /// - the Mac still holds `PairingHarness.png`, the bytes it copied, with
    ///   its `pHYs` intact -> the screenshot pastes at 80x60;
    /// - each side's store still describes its OWN clipboard -- they now
    ///   deliberately DISAGREE on the hash, which is the whole point: two
    ///   machines holding the same picture in different bytes, each honest
    ///   about what it has;
    /// - no `clipboard changed while apart` -> neither store has drifted from
    ///   the clipboard it describes.
    ///
    /// ONE THING THIS TEST NO LONGER WITNESSES, stated rather than left to be
    /// discovered. The old `waitForPeer` on the first reconnect was the only
    /// end-to-end observation of the 1 ms nudge surviving Python's `json` and
    /// Swift's `JSONDecoder` (0.001 on a ~1.75e9 unix timestamp). Provenance
    /// short-circuits before freshness, so no reconnect reaches a timestamp
    /// comparison any more and that precision loss would now be invisible
    /// here. The nudge itself is still pinned in the PC's own suite.
    func testARetinaScreenshotSurvivesTheRoundTripBecauseThePCSaysWhereItsBytesCameFrom() throws {
        let harness = try connected(substituting: true)

        // The Mac's user copies a screenshot: a PNG carrying `pHYs`.
        harness.pasteboard.setImage(PairingHarness.png)
        // GPaste's substitution, read from the fakes' own invocation log
        // rather than inferred -- a run where the mode was armed and somehow
        // did not fire would otherwise assert the fix against a clipboard
        // that never re-encoded anything, and pass.
        let reEncoded = try harness.waitForThePCToReEncodeTheImageItWasSent()
        XCTAssertNotEqual(reEncoded, PairingHarness.png,
                          "the fake stored the bytes it was handed; there is no substitution " +
                          "here to reconcile and the rest of this test proves nothing")
        // And the PC's agent absorbing that re-offer into its own store, at
        // the nudged timestamp and NAMING WHAT IT WAS GIVEN. This is the state
        // the reconnects run against, and waiting for it is not politeness:
        // hang up before it lands and the PC's next startup finds a clipboard
        // its store does not describe, and says so, correctly.
        try harness.waitForThePCsAgentToRecord(sha256Hex(reEncoded))
        XCTAssertEqual(harness.pcsClipState()?["origin"] as? String, sha256Hex(PairingHarness.png),
                       "the PC must have recorded the hash it was HANDED; without it there is no " +
                       "provenance for either side to read and the reconnects below prove nothing")

        // --- the first reconnect: nothing crosses -------------------------
        let clipsBefore = harness.clipsFromThePC()
        let writesBefore = harness.writesOnThePC()
        try harness.reconnect()
        XCTAssertEqual(try harness.decisionOnThisConnection(), "doNothing",
                       "the peer's clip-state names our own screenshot as its ancestor, so this " +
                       "side declines it outright rather than waiting for it to arrive")
        // The PC's own verdict, and the reason to wait for it before counting:
        // it is written after its announcement, so once it is in the file the
        // PC has had its chance to send and a zero count means it chose not to.
        try harness.waitForThePCToReconcileOnThisConnection()
        XCTAssertEqual(harness.clipsFromThePC() - clipsBefore, 0,
                       "measured at one before this rule was wired in: the PC's re-encode, coming " +
                       "back to overwrite the screenshot the Mac still holds")
        XCTAssertEqual(harness.writesOnThePC() - writesBefore, 0,
                       "and nothing goes the other way either -- one rule, both sides, standing " +
                       "down together rather than one waiting on the other")

        // BOTH sentences, in one file, from one connection. The two sides
        // take DIFFERENT branches of the same rule -- the PC holds the
        // derivative, this Mac holds the ancestor -- so a log carrying only
        // one of them means only one implementation stood down and the other
        // reached doNothing some other way. `Channel.attempt` prefixes the
        // agent's stderr with `remote: `, which is what puts the PC's line
        // here at all; nothing else in the text says which machine wrote it,
        // and that is deliberate.
        XCTAssertTrue(harness.logHolds("what we hold descends from the peer's clipboard: standing down"),
                      "the PC's own suppression must be visible, not inferred from its verdict")
        XCTAssertTrue(harness.logHolds("the peer's clipboard descends from what we hold: standing down"),
                      "and this side's, in the same file, from the same connection")

        XCTAssertEqual(harness.pasteboard.read()?.data, PairingHarness.png,
                       "the Mac keeps its own screenshot, `pHYs` and all -- if this holds the " +
                       "re-encoded bytes the density is gone and the image pastes at double size")
        XCTAssertEqual(harness.macsClipState()?.sha256, sha256Hex(PairingHarness.png),
                       "and its store describes its OWN clipboard. The two sides now disagree " +
                       "about the hash on purpose: they hold the same picture in different bytes")

        // --- the second reconnect: still nothing, off the disk -------------
        try harness.reconnect()
        XCTAssertEqual(try harness.decisionOnThisConnection(), "doNothing",
                       "the agent that watched the substitution is long dead; this verdict is " +
                       "reached entirely from what its store carried across")
        try harness.waitForThePCToReconcileOnThisConnection()
        XCTAssertEqual(harness.clipsFromThePC() - clipsBefore, 0,
                       "the origin must survive being written, loaded and RE-written by an " +
                       "announce that never saw the substitution")
        XCTAssertEqual(harness.writesOnThePC() - writesBefore, 0)
        XCTAssertEqual(harness.pasteboard.read()?.data, PairingHarness.png,
                       "no reconnect may move this clipboard again")

        // Last, and deliberately so. `agentLog()` flushes this side's queue,
        // but a line the PC has written to its stderr reaches that file only
        // once `attempt()`'s readability handler has read it -- a small window
        // in which a negative assertion could pass for the wrong reason. The
        // wait for the PC's own reconciliation line above closes it: that line
        // is written to the same stream after its announcement, so once it is
        // in the file, a false `clipboard changed while apart` from this
        // connection would be too.
        XCTAssertFalse(harness.logHolds("clipboard changed while apart"),
                       "nothing changed on either clipboard across these reconnects, so this " +
                       "line is a store failing to describe what its own clipboard returns")
    }

    // MARK: - the three ways provenance could be inert or harmful

    /// THE NIL CASE, in the loop. `None == None` is `True` in Python and
    /// `nil == nil` is `true` for a Swift `Optional`, so the naive spelling of
    /// `resolveProvenance` fires on an empty clipboard against a peer with no
    /// origin -- and that is not an exotic pairing, it is a locked or
    /// rebooted PC against an ordinary Mac, which happens daily. Both sides
    /// would stand down, and the `(_, nil) -> sendMine` recovery that hands a
    /// peer back the clipboard it lost would be dead for EVERY kind of
    /// content, not merely for images. A rule about screenshots would have
    /// silently deleted the recovery for text.
    ///
    /// `fixtures/provenance.json` pins the rule and the two suites pin the
    /// call sites, so what is left for this file is the only thing neither
    /// can see: that the recovery still happens between the two real
    /// implementations, over a real pipe, with a real agent process on the
    /// far end.
    ///
    /// BOTH DECISIONS ARE READ, and the nil case is precisely why that is not
    /// belt and braces. Break the guard on the PYTHON side alone and the Mac
    /// still resolves `sendMine`, still sends, and the PC still applies it --
    /// the apply path does not consult its own verdict -- so the clipboard
    /// assertion below passes over a PC that stood down when it should have
    /// been waiting. One rule, two implementations, two readings.
    ///
    /// The clipboard is emptied `whileApart`, in the window where no agent is
    /// watching, because that is what a reboot IS. Emptied on a live
    /// connection it would be a local change the PC observes and reports, and
    /// the reconnect this test turns on would never be reached.
    func testAnEmptyPeerIsStillHandedBackWhatItLostBecauseTwoAbsencesDoNotMatch() throws {
        let harness = try connected()
        let clip = "the clip the PC lost ✓"

        harness.pasteboard.set(clip)
        try harness.waitForThePCsClipboard(toHold: Data(clip.utf8),
                                           offeredAs: "text/plain;charset=utf-8")

        // The PC reboots, or its session locks: it comes back offering
        // nothing at all and announces a null hash.
        try harness.reconnect(whileApart: { try harness.emptyThePCsClipboard() })

        XCTAssertEqual(try harness.decisionOnThisConnection(), "sendMine",
                       "a peer holding nothing gets its clipboard back. Reading doNothing " +
                       "here is the naive rule matching this side's absent origin against " +
                       "the peer's absent hash")
        XCTAssertEqual(try harness.pcsDecisionOnThisConnection(), "waitForPeer",
                       "and the PC must be WAITING for it rather than standing down -- a PC " +
                       "that resolved doNothing still receives the clip below, so this line " +
                       "is the only thing that can tell the two apart")
        try harness.waitForThePCsClipboard(toHold: Data(clip.utf8),
                                           offeredAs: "text/plain;charset=utf-8")
    }

    /// THE PERSISTENCE CASE, read off the disk rather than off the outcome.
    ///
    /// `sshd` spawns one agent per connection, so the process that watched
    /// GPaste hand back different bytes than it was given is already dead at
    /// the announce that has to say so. The store is the only thing that
    /// crosses that gap, and an origin that lived only in `_expect_reoffer`
    /// would be the third release running in which this bug was fixed and
    /// nothing changed on the machine.
    ///
    /// WHY THIS IS NOT THE DENSITY TEST AGAIN. That one asserts the OUTCOME
    /// -- `doNothing`, no frames, the Mac's own bytes -- across the same two
    /// reconnects. This one asserts the CARRIER: after each reconnect the
    /// record on the PC's disk still names both the re-encode it holds and
    /// the ancestor it was born from, written there by an agent that never
    /// saw the substitution. The distinction is worth a test because the two
    /// go red at different moments and say different things: a
    /// `resolve_startup_state` that rebuilt its matched record around the
    /// current hash -- correct on all three fields anyone thinks to check --
    /// strips the fourth on the FIRST reconnect's announce, and the outcome
    /// test can only report that a screenshot came back.
    ///
    /// Both reconnects are asserted, not only the second. The design says the
    /// fix "dies on the second reconnect", and the reasoning behind that is
    /// about the origin surviving in the recording process's memory -- which
    /// is true of a unit test sharing one process and NOT true here, where
    /// every reconnect kills the agent outright. Asserting each one lets the
    /// failure name whichever is actually first rather than the one predicted.
    /// Measured, with the matched record rebuilt around the current hash: the
    /// FIRST reconnect goes red, because the announce that has to carry the
    /// origin is itself built from `resolve_startup_state`.
    ///
    /// WHICH LEAVES THE SECOND ITERATION'S TWO DECISION ASSERTIONS PROVING
    /// LESS THAN THEY LOOK LIKE THEY DO, and that is said here rather than
    /// left to be discovered. Under that same break the first reconnect's
    /// unsuppressed exchange CONVERGES the two sides -- the PC wins freshness
    /// by the nudge, sends its re-encode, and the Mac adopts it -- so by the
    /// second reconnect both hold one hash and `doNothing` is reached by
    /// freshness alone, with provenance never consulted. Both decision
    /// assertions stay green there while the store assertion is red, which is
    /// exactly the split that makes reading the disk worth a test of its own:
    /// on the second reconnect it is the only witness left.
    func testTheOriginOutlivesEveryAgentThatCarriesItAcross() throws {
        let harness = try connected(substituting: true)

        harness.pasteboard.setImage(PairingHarness.png)
        let reEncoded = try harness.waitForThePCToReEncodeTheImageItWasSent()
        XCTAssertNotEqual(reEncoded, PairingHarness.png,
                          "the fake stored the bytes it was handed; there is no substitution " +
                          "here to remember and the rest of this test proves nothing")
        try harness.waitForThePCsAgentToRecord(sha256Hex(reEncoded))
        XCTAssertEqual(harness.pcsClipState()?["origin"] as? String, sha256Hex(PairingHarness.png),
                       "the agent that WATCHED the substitution must write down the hash it " +
                       "was handed; everything below is that one record being carried forward")

        for connection in ["the first reconnect", "the second reconnect"] {
            try harness.reconnect()
            // Before the store is read: `announce_clip_state` persists what
            // it resolved and then sends it, and the reconciliation line
            // follows the send -- so once the PC's verdict is in the log its
            // save has already happened, and a read here cannot catch the
            // file mid-connection.
            XCTAssertEqual(try harness.pcsDecisionOnThisConnection(), "doNothing",
                           "\(connection): the agent that saw the substitution is long dead, " +
                           "so this verdict is reached entirely from what its store carried")
            XCTAssertEqual(harness.pcsClipState()?["sha256"] as? String, sha256Hex(reEncoded),
                           "\(connection): the PC still describes its OWN clipboard")
            XCTAssertEqual(harness.pcsClipState()?["origin"] as? String,
                           sha256Hex(PairingHarness.png),
                           "\(connection): and it must RE-persist the ancestor it never " +
                           "witnessed. A matched record rebuilt around the current hash looks " +
                           "right on every field but this one, and the fix then lasts exactly " +
                           "as long as the record that still has it")
            XCTAssertEqual(try harness.decisionOnThisConnection(), "doNothing",
                           "\(connection): and this side reads the origin it was sent")
        }
    }

    /// THE STALE CASE: the mirror direction, and the one that is easy to miss
    /// because the safe half of it is so obvious. An origin naming a hash the
    /// peer no longer holds simply never matches, which is harmless. The
    /// mirror is a silent loss: the PC holds new content `U` under an origin
    /// `M` that the Mac still holds, `mine.origin == peer.sha256` fires, and
    /// the PC NEVER SENDS `U`. A clip the user copied, gone, on an entirely
    /// routine path -- and gone quietly, because both sides agree they have
    /// nothing to do.
    ///
    /// So the origin has to die with the content it describes:
    /// `resolve_startup_state`'s changed branch clears it, and every store
    /// write outside the one recording branch writes none.
    ///
    /// STAGED THE ONLY WAY IT CAN HAPPEN. `U` is copied on the PC WHILE
    /// APART, because that is the whole shape of the hazard: a change the PC
    /// observes goes to the Mac over the live connection, which leaves the
    /// Mac no longer holding `M` and nothing for a stale origin to match. It
    /// takes an unobserved change for the PC to arrive at a reconciliation
    /// still holding the ancestor's origin, and an unobserved change is one
    /// made with no agent alive.
    ///
    /// TEXT, deliberately, for `U`. The substituting fake is armed for step
    /// one and stays armed, and `wl-copy` only re-encodes PNGs -- so text is
    /// the value that reaches the reconciliation unmangled, and it also
    /// proves the loss is not about images: the origin is stale, and what it
    /// would silence is whatever the user copied next.
    ///
    /// This connection DOES log `clipboard changed while apart`, correctly --
    /// the PC's clipboard genuinely changed while nothing was watching -- so
    /// unlike the density test above, its absence is not asserted here.
    func testAClipCopiedOnThePCWhileApartIsStillSentThoughItsAncestorSitsOnTheMac() throws {
        let harness = try connected(substituting: true)
        let copiedOnThePC = "copied on the PC while the Mac slept ✓"

        // Step one, only to manufacture a real origin: the Mac's screenshot,
        // applied and re-encoded, leaves the PC's store naming the Mac's own
        // hash as the ancestor of what it holds.
        harness.pasteboard.setImage(PairingHarness.png)
        let reEncoded = try harness.waitForThePCToReEncodeTheImageItWasSent()
        try harness.waitForThePCsAgentToRecord(sha256Hex(reEncoded))
        XCTAssertEqual(harness.pcsClipState()?["origin"] as? String, sha256Hex(PairingHarness.png),
                       "without a recorded origin there is no stale origin to survive, and " +
                       "this test would pass on a machine where provenance never ran at all")

        // Step two: the user copies something else on the PC, with no agent
        // alive to notice. The Mac still holds the screenshot the stale
        // origin names.
        try harness.reconnect(whileApart: {
            try harness.copyOnThePC(text: copiedOnThePC)
        })

        XCTAssertEqual(try harness.pcsDecisionOnThisConnection(), "sendMine",
                       "the PC's clipboard changed, so the origin describing what it USED to " +
                       "hold is dead with it. Reading doNothing here is the stale origin " +
                       "still matching the ancestor on the Mac -- and the user's clip is lost")
        XCTAssertEqual(try harness.decisionOnThisConnection(), "waitForPeer",
                       "and this side waits for it rather than standing down against an " +
                       "ancestry claim about content the PC no longer holds")
        try harness.waitForTheMacsPasteboard(toHold: .text, Data(copiedOnThePC.utf8))
    }

    // MARK: - GPaste re-offering its own clipboard is not a dead event source

    /// THE REGRESSION THIS WHOLE RELEASE EXISTS FOR, between the two real
    /// implementations. Production incident `2026-08-02T04:21:52Z`, spec 1.2,
    /// spec 9.1's first acceptance criterion.
    ///
    /// An image lands on the PC, the agent syncs it here, and one to six
    /// seconds later GPaste takes the selection back and re-offers the SAME
    /// picture under its own long type list -- emitting no `Update` for it and
    /// creating no history entry. Before v3.3 the safety net read that as
    /// "the content changed and the event source said nothing", armed, and
    /// confirmed on the next quiet tick: the connection dropped to 1 Hz
    /// `wl-paste` polling, which on this installation TAKES KEYBOARD FOCUS on
    /// every tick, for the rest of its life. On a machine where nothing was
    /// wrong.
    ///
    /// WHY THE INTERVALS ARE INJECTED, since it is the reason this test can
    /// exist at all. The verdict is reached on the safety net's poll thread,
    /// and until task 10's fix round nothing outside the agent could move
    /// that thread off `SAFETY_NET_POLL_SECONDS` -- the plan named
    /// `CLIPWIRE_SAFETY_NET_SECONDS` and never built it, so this scenario
    /// cost three 30-second ticks and was simply not written. `safetyNet`
    /// below is that variable. `fast` has to come with it and is not
    /// decoration: `uuid_frozen` is a delta between two slow ticks, so the
    /// fast tier must have read a uuid before the first of them or the slow
    /// tier reaches no verdict at all and this test would pass having proved
    /// nothing. `slow` stays nil on purpose -- it starts no timer, and
    /// setting it is the mistake the harness's own `tierSeconds` comment
    /// documents.
    ///
    /// WHAT THIS SIDE CAN AND CANNOT SEE. `_armed` is a field of a Python
    /// object in another process. What crosses is what the agent FORKED, so
    /// "the slow tier saw the divergence" is asserted as its own
    /// `wl-paste --list-types` coming back with the re-offered list -- a real
    /// fork, a real observation, twice. The arming and clearing of the run
    /// itself is pinned one altitude down, in
    /// `agent/tests/test_watcher_gpaste_reoffer.py`, which drives the real
    /// watcher against these same fakes. THE TWO ARE NOT DUPLICATES AND MUST
    /// NOT BE DE-DUPLICATED: that one can read the watcher's state and goes
    /// red at the exact commit boundary; this one is the only thing in the
    /// project that puts a real Swift peer on the other end of the pipe while
    /// it happens.
    ///
    /// MEASURED RED, without which the rest is decoration: against
    /// `git checkout 58d3c60 -- agent/clipwire-agent.py` -- the commit before
    /// `6ac6eec` brought in both the uuid discriminator and spec 4.2's
    /// settled-clears rule -- this test fails on the last assertion below,
    /// with the agent's own degrade line in the shared log.
    ///
    /// AND MEASURED REPEATEDLY, which is the part worth keeping, because the
    /// first two versions of this test were red only SOMETIMES. THE ARMING
    /// OPPORTUNITY IS ONE-SHOT: the token moves exactly once, so a run that
    /// is armed and then cleared by any transient can never re-arm -- the
    /// token never moves again. Every early-return in the waits above is
    /// therefore not a slow test, it is a test that reports success against a
    /// broken agent. Two such holes were found by re-running the mutation
    /// table rather than re-reading it, both fixed above, and the intervals
    /// below are the third fix: at `fast: 0.05` the agent forks `gdbus`
    /// twenty times a second and a single failed fork clears the run. Halving
    /// that pressure took the fix-deleted run from 8 of 10 caught to 18 of
    /// 18, with the correct agent green 6 of 6. If this test is ever seen
    /// flaking, it is measuring fork pressure, and the answer is longer
    /// intervals -- never a retry, which would hide exactly the defect it is
    /// here to catch.
    func testAGPasteReofferDoesNotDegradeTheConnection() throws {
        let harness = try connected(tierSeconds: (fast: 0.1, slow: nil, safetyNet: 0.4))

        // --- positive evidence, before any claim about an absence ---------
        //
        // `start()` already refuses a world where this line is missing, but
        // asserting it HERE is what stops this method green-lighting a run in
        // which the agent never started, never connected, or wrote no log:
        // every assertion below is about something NOT appearing, and all of
        // them are satisfied by a dead harness.
        XCTAssertTrue(harness.logHolds(PairingHarness.liveWatcherLine),
                      "the agent never reported a GPaste watcher, so nothing below is " +
                      "evidence about the event path -- it is evidence about silence")

        // The image lands, and actually crosses: the fake gdbus monitor's
        // Update, the agent's pump, its worker, the wire, and this side's
        // `.imageClip` handling. A clip that never arrived would leave every
        // absence below true for the wrong reason.
        try harness.copyOnThePC(png: PairingHarness.png)
        try harness.waitForTheMacsPasteboard(toHold: .image, PairingHarness.png)

        // The agent's slow tier takes its baseline, and BOTH halves of that
        // are waited for, in this order, because the scenario is unstageable
        // without either.
        //
        // First the fast tier must have read a uuid at all: the slow tier's
        // `uuid_frozen` is a delta between two of its own ticks, and the
        // earlier one records whatever the fast tier had by then -- `None`
        // included. Measured at about one run in six before this wait
        // existed: the warm-up tick landed first, the divergence tick then
        // read "unmeasured", no verdict could be reached, and this test
        // passed WITH THE FIX DELETED FROM THE AGENT. See
        // `waitForTheAgentsFastTierToReadAHistoryUuid`.
        try harness.waitForTheAgentsFastTierToReadAHistoryUuid()
        // Then a slow tick has to happen AFTER that reading -- hence a
        // baseline taken here rather than an absolute count, which the probes
        // already past would have satisfied on their own.
        let baselineProbes = harness.probesThatSaw(["image/png"])
        try harness.waitForTheAgentToProbeAndSee(["image/png"], atLeast: baselineProbes + 1)

        // --- GPaste takes the selection back, silently --------------------
        let entryBefore = harness.pcsClipboardGeneration()
        try harness.silentTakeover(types: PairingHarness.gpasteImageTypes)
        // The fixture's own invariant, pinned because breaking it leaves
        // everything below passing while staging a different scenario: a
        // re-offer creates NO history entry, which is what keeps the agent's
        // uuid frozen and the slow tier's discriminator meaningful. A
        // takeover that stamped a fresh `generation` would be a COPY wearing
        // a re-offer's name, the uuid would move, no verdict could be reached
        // for a reason that has nothing to do with the fix, and this test
        // would go on reporting success.
        XCTAssertEqual(harness.pcsClipboardGeneration(), entryBefore,
                       "the re-offer created a new clipboard entry, so this is a copy and not " +
                       "the takeover spec 1.2 measured")
        XCTAssertNotNil(entryBefore,
                        "the PC's clipboard has no change token at all, so the assertion above " +
                        "compared two absences and proved nothing")

        // Two ticks: the one that sees the divergence and arms, and the one
        // that would have confirmed it. Three probes guarantee both, since the
        // worker's read can inflate the count by at most one.
        try harness.waitForTheAgentToProbeAndSee(PairingHarness.gpasteImageTypes, atLeast: 4)

        // --- and the verdict that must not have been reached --------------
        //
        // The agent is hung up FIRST, and this is the difference between a
        // regression test and a decoration. Counting the agent's own probes
        // proves the ticks happened; it cannot prove a line the agent wrote
        // has crossed the pipe this side reads it through. Measured with the
        // fix removed from the agent: without this drain the assertion below
        // reported success on one run in three, against an agent that had
        // logged the verdict. See `hangUpAndDrainTheAgentsLog`.
        try harness.hangUpAndDrainTheAgentsLog()

        XCTAssertFalse(harness.logHolds("GPaste reported no clipboard change"),
                       "GPaste's own re-offer was diagnosed as a dead event source. The PC is " +
                       "now polling wl-paste for the rest of this connection, taking keyboard " +
                       "focus every tick, on a machine whose clipboard is working perfectly")
        // The Mac still holds what the PC sent it. A degrade is not the only
        // way this scenario can hurt: an agent that read the re-offer as a
        // fresh copy would send the same picture back, and on this fixture
        // that is invisible in the log and visible only here.
        XCTAssertEqual(harness.pasteboard.read()?.data, PairingHarness.png,
                       "the re-offer came back as a new clip and overwrote the Mac's own copy")
    }

    // MARK: - the harness refuses a world it cannot honestly test

    /// Task 3's measurement, kept as a test instead of as a sentence in a
    /// commit message: with a `gdbus` that answers `monitor` but not
    /// `introspect`, `GPasteWatcher.available()` is false, `make_watcher`
    /// returns the plain poller, and the five tests above would still pass --
    /// on a run that exercised none of the machinery they exist to cover.
    ///
    /// So the harness refuses, and this is the evidence that its refusal is
    /// not vacuous. Without a test in this shape, "the harness asserts the
    /// event path" is a claim about a code path nothing ever takes.
    func testTheHarnessRefusesAnAgentOnThePollingFallback() throws {
        let harness = try PairingHarness(eventSource: .answersOnlyMonitor)
        self.harness = harness

        XCTAssertThrowsError(try harness.start()) { error in
            guard case PairingHarness.Refusal.wrongWorld(let what, _) = error else {
                return XCTFail("expected the harness to refuse the polling fallback, got \(error)")
            }
            XCTAssertTrue(what.contains("falling back to polling"),
                          "the refusal must name what it saw in the agent's own log, so a " +
                          "reader does not have to guess which precondition failed: \(what)")
        }
    }

    // MARK: - the harness's own configuration plumbing

    /// `tierSeconds`'s two `exportEnvironment` calls run BEFORE
    /// `restoreEnvironment = restore` copies the accumulated dictionary in
    /// `init` -- `Dictionary` is a value type, so that assignment is a
    /// snapshot, and anything added to the local `restore` afterward is
    /// invisible to `stop()`'s restore loop. Nothing about that ordering is
    /// enforced by the type system: swap the two and `swift build` still
    /// succeeds, `setenv` still runs (so a test READING the variable while
    /// the harness is alive would still see it and stay green), and only
    /// `stop()` silently stops unsetting it -- leaking
    /// CLIPWIRE_FAST_TIER_SECONDS/CLIPWIRE_SLOW_TIER_SECONDS into every
    /// later XCTest in the same process, since nothing else in this file
    /// ever reads either name back to notice. This test is the one thing
    /// that fails if that ordering regresses.
    func testTierSecondsDoNotLeakIntoLaterTestsAfterStop() throws {
        // If either of these is not nil, a previous test already leaked --
        // and this test could not tell its own export apart from that leak.
        // XCTAssertNil records a failure and keeps running rather than
        // stopping the test, so a leak from a prior test shows up as THIS
        // test's own failure here, rather than as a silent pass below on
        // borrowed state it did not create.
        for name in ["CLIPWIRE_FAST_TIER_SECONDS", "CLIPWIRE_SLOW_TIER_SECONDS",
                     "CLIPWIRE_SAFETY_NET_SECONDS"] {
            XCTAssertNil(ProcessInfo.processInfo.environment[name],
                         "\(name) was already set before this test constructed a harness -- " +
                         "a prior test leaked it")
        }

        let harness = try PairingHarness(tierSeconds: (fast: 0.25, slow: 3.5, safetyNet: 7.5))
        self.harness = harness

        // The mechanism actually ran, not just "construction did not throw".
        // All three, because all three are separate `exportEnvironment` calls
        // and a fourth added below the snapshot would leak exactly as the
        // third would have: this is the test that fails, and the only one.
        XCTAssertEqual(ProcessInfo.processInfo.environment["CLIPWIRE_FAST_TIER_SECONDS"], "0.25")
        XCTAssertEqual(ProcessInfo.processInfo.environment["CLIPWIRE_SLOW_TIER_SECONDS"], "3.5")
        XCTAssertEqual(ProcessInfo.processInfo.environment["CLIPWIRE_SAFETY_NET_SECONDS"], "7.5")

        harness.stop()

        // The regression this test exists for: gone, not "0.25" forever.
        for name in ["CLIPWIRE_FAST_TIER_SECONDS", "CLIPWIRE_SLOW_TIER_SECONDS",
                     "CLIPWIRE_SAFETY_NET_SECONDS"] {
            XCTAssertNil(ProcessInfo.processInfo.environment[name],
                         "stop() did not restore \(name) -- it will leak into every later test")
        }
    }
}
