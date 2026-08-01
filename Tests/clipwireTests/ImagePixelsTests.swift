// Tests/clipwireTests/ImagePixelsTests.swift
//
// `imagePixelsIdentical` is the one judgement the density fix rests on: it
// decides whether an incoming image is the same picture as the one this
// pasteboard already holds, and therefore whether the local bytes -- the ones
// still carrying `pHYs` -- survive. Both of its answers are consequential, so
// both are pinned here: a wrong `true` throws away a picture the user was
// actually sent, a wrong `false` leaves the retina bug exactly as it was.
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import clipwire

/// Real PNGs, built by ImageIO rather than hand-assembled, so what these tests
/// feed the comparison is what a real encoder emits -- including whatever
/// colour tagging ImageIO puts on its output, which is the half of the problem
/// a hand-rolled fixture would quietly omit.
enum TestPNG {
    /// Deterministic, fully opaque RGBA samples. `seed` changes the picture.
    static func samples(width: Int, height: Int, seed: Int = 0) -> [UInt8] {
        var samples = [UInt8]()
        samples.reserveCapacity(width * height * 4)
        for y in 0..<height {
            for x in 0..<width {
                let red: Int = (x * 37 + y * 11 + seed) % 256
                let green: Int = (x * 5 + y * 91 + seed * 7) % 256
                let blue: Int = (x * 200 + y * 3 + seed * 29) % 256
                samples.append(UInt8(red))
                samples.append(UInt8(green))
                samples.append(UInt8(blue))
                // Opaque throughout: premultiplication is then a no-op, so a
                // difference these tests see is a difference in the picture
                // rather than in a rounding rule.
                samples.append(0xFF)
            }
        }
        return samples
    }

    /// `width` x `height` from a generated pattern. `dpi` adds the density
    /// chunk, which is the metadata this whole release is about.
    static func make(width: Int, height: Int, seed: Int = 0, dpi: Double? = nil) -> Data {
        encode(samples(width: width, height: height, seed: seed),
               width: width, height: height, dpi: dpi)
    }

    /// The encoder itself, taking raw RGBA, so a test can differ by one
    /// sample rather than by a whole pattern.
    ///
    /// What ImageIO actually emits here, dumped rather than assumed: `IHDR`,
    /// `sRGB`, `eXIf`, `pHYs` (only with a `dpi`), `IDAT`, `IEND` -- so these
    /// fixtures carry a real colour tag, and `strippingAncillaryChunks` below
    /// really does remove one. Without that, the stripped-fixture test would
    /// exercise only the density difference and would look like it covered
    /// the colour normalisation while covering nothing of the kind.
    static func encode(_ samples: [UInt8], width: Int, height: Int, dpi: Double? = nil,
                       space: CGColorSpace = CGColorSpace(name: CGColorSpace.sRGB)!) -> Data {
        let provider = CGDataProvider(data: Data(samples) as CFData)!
        let image = CGImage(
            width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
            bytesPerRow: width * 4, space: space,
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.premultipliedLast.rawValue
                                     | CGBitmapInfo.byteOrder32Big.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        return writePNG(image, dpi: dpi)
    }

    /// One 8-bit channel, no alpha, a MONOCHROME colour space -- not a
    /// variation on `encode` above but a different image at the CGImage level,
    /// which is the point: `CGImage.copy(colorSpace:)` refuses to re-tag
    /// across colour models, so this is the input `imagePixelsIdentical`
    /// cannot normalise and always answers `false` for.
    static func greyscale(width: Int, height: Int, dpi: Double? = nil) -> Data {
        var samples = [UInt8]()
        samples.reserveCapacity(width * height)
        for y in 0..<height {
            for x in 0..<width { samples.append(UInt8((x * 37 + y * 11) % 256)) }
        }
        let provider = CGDataProvider(data: Data(samples) as CFData)!
        let image = CGImage(
            width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8,
            bytesPerRow: width, space: CGColorSpaceCreateDeviceGray(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        return writePNG(image, dpi: dpi)
    }

    /// The PNG destination both builders share, so the density chunk is
    /// attached the same way for either kind of image.
    private static func writePNG(_ image: CGImage, dpi: Double?) -> Data {
        let out = NSMutableData()
        let destination = CGImageDestinationCreateWithData(out, UTType.png.identifier as CFString,
                                                           1, nil)!
        var properties: [CFString: Any] = [:]
        if let dpi {
            properties[kCGImagePropertyDPIWidth] = dpi
            properties[kCGImagePropertyDPIHeight] = dpi
        }
        CGImageDestinationAddImage(destination, image, properties as CFDictionary)
        precondition(CGImageDestinationFinalize(destination))
        return out as Data
    }

    /// Every ancillary chunk removed, the image data untouched -- the exact
    /// shape GPaste's re-encode leaves behind, and the same rule
    /// `Tests/fakes/fake_clipboard.py` applies (`KEPT_CHUNKS`). `pHYs` goes,
    /// which is the bug; `iCCP`/`sRGB`/`gAMA`/`cHRM` go with it, which is the
    /// trap, since the result decodes in a different colour space from the
    /// original unless something normalises them.
    ///
    /// Chunk surgery rather than a re-encode: PNG chunks are independent and
    /// each carries its own CRC, so dropping whole chunks needs no
    /// recompression and cannot alter a single sample. That is what makes
    /// these fixtures evidence -- the pixels are identical BY CONSTRUCTION,
    /// so a comparison that says otherwise is the comparison being wrong.
    static func strippingAncillaryChunks(_ png: Data) -> Data {
        let kept: Set<String> = ["IHDR", "PLTE", "tRNS", "IDAT", "IEND"]
        var out = Data(png.prefix(8))     // the signature
        var offset = 8
        while offset + 8 <= png.count {
            let length = Int(png[(offset)..<(offset + 4)].reduce(UInt32(0)) { $0 << 8 | UInt32($1) })
            let kind = String(decoding: png[(offset + 4)..<(offset + 8)], as: UTF8.self)
            let end = offset + 12 + length
            guard end <= png.count else { break }
            if kept.contains(kind) { out.append(png[offset..<end]) }
            offset = end
        }
        return out
    }
}

final class ImagePixelsTests: XCTestCase {
    /// The bug's own shape: one PNG carries the density, the other is what
    /// comes back after a re-encode that dropped it. Same picture, different
    /// bytes -- and the comparison has to see through the difference, or the
    /// fix never fires and the screenshot keeps pasting at double size.
    func testTheSameImageWithAndWithoutItsDensityIsTheSamePixels() {
        let dense = TestPNG.make(width: 9, height: 7, dpi: 144)
        let plain = TestPNG.make(width: 9, height: 7)

        XCTAssertNotEqual(dense, plain, "the fixtures must differ as BYTES, or this proves nothing")
        XCTAssertTrue(imagePixelsIdentical(dense, plain))
    }

    /// GPaste drops the colour profile along with `pHYs`, so the copy that
    /// comes back carries no colour information at all. This is the whole
    /// GPaste shape in one fixture pair.
    ///
    /// What it does NOT prove, stated so nobody reads more into it: ImageIO
    /// reads an untagged PNG as sRGB, so a stripped copy of an sRGB-tagged
    /// original decodes into the same space either way, and this pair would
    /// still compare equal even if the comparison used each image's own space
    /// instead of a fixed one -- measured, by making that change and watching
    /// this test stay green. The choice that this pair genuinely cannot see,
    /// and the one v3.1 got wrong first, is re-tagging against CONVERTING:
    /// `testAWideGamutOriginalAgainstAnUntaggedCopyIsIdentical` below is where
    /// that shows up, and it inverted -- name and assertion together -- when
    /// the conversion came out.
    func testAnImageStrippedOfEveryAncillaryChunkIsStillTheSamePixels() {
        let original = TestPNG.make(width: 9, height: 7, dpi: 144)
        let stripped = TestPNG.strippingAncillaryChunks(original)

        XCTAssertNotEqual(original, stripped, "the strip must have removed something")
        XCTAssertTrue(imagePixelsIdentical(original, stripped),
                      "one colour space for both, or the fix never fires on a real screenshot")
    }

    /// The other direction, and the one that keeps the test above from being
    /// satisfied by a function that returns `true` for anything decodable.
    func testTwoDifferentPicturesOfTheSameSizeAreNotIdentical() {
        XCTAssertFalse(imagePixelsIdentical(TestPNG.make(width: 9, height: 7, seed: 0),
                                            TestPNG.make(width: 9, height: 7, seed: 1)))
    }

    /// ONE channel of ONE pixel apart, by one, with the density difference
    /// laid on top -- so a comparison that gave up somewhere short of every
    /// byte (dimensions only, a sampled subset, a hash of the header) would
    /// pass everything above and still fail here. This is the assertion that
    /// makes "identical pixels" mean identical.
    func testOneChannelOfOnePixelApartIsNotIdentical() {
        var samples = TestPNG.samples(width: 4, height: 4)
        let dense = TestPNG.encode(samples, width: 4, height: 4, dpi: 144)
        samples[9] ^= 0x01
        let altered = TestPNG.encode(samples, width: 4, height: 4)

        XCTAssertTrue(imagePixelsIdentical(dense, TestPNG.encode(TestPNG.samples(width: 4, height: 4),
                                                                 width: 4, height: 4)),
                      "the control: the same samples, one with density, one without")
        XCTAssertFalse(imagePixelsIdentical(dense, altered),
                       "a single sample off by one is a different picture")
    }

    /// 4x1 and 2x2 built from the SAME sixteen bytes decode to byte-identical
    /// buffers -- row-major with no padding, so the shape leaves no trace in
    /// the pixel data at all. Comparing buffers alone would call them one
    /// picture and keep the wrong bytes; the dimension check exists for
    /// exactly this.
    ///
    /// The samples have to be shared for this to bite. Two independently
    /// generated pictures of those shapes differ in their bytes as well as
    /// their dimensions, so a comparison with no dimension check at all still
    /// answers correctly -- verified by removing the check and watching this
    /// test, in its earlier form, stay green.
    func testSameBufferLengthWithDifferentDimensionsIsNotIdentical() {
        let shared = TestPNG.samples(width: 4, height: 1)
        let wide = TestPNG.encode(shared, width: 4, height: 1)
        let square = TestPNG.encode(shared, width: 2, height: 2)

        XCTAssertFalse(imagePixelsIdentical(wide, square))
    }

    /// Byte-identical input is the degenerate case, and it must not be
    /// special-cased away: the branch that calls this also stores a local
    /// hash, so "the peer sent back exactly what we hold" has to reach it.
    func testIdenticalBytesAreIdenticalPixels() {
        let png = TestPNG.make(width: 5, height: 5, dpi: 144)
        XCTAssertTrue(imagePixelsIdentical(png, png))
    }

    /// Anything that will not decode is "not identical", never a crash and
    /// never an optimistic `true`. The 9-byte stub is the one this suite
    /// already uses elsewhere as a stand-in PNG; it has the signature and
    /// nothing else.
    func testUndecodableBytesAreNeverIdentical() {
        let stub = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x07])
        let real = TestPNG.make(width: 3, height: 3)

        XCTAssertFalse(imagePixelsIdentical(stub, real))
        XCTAssertFalse(imagePixelsIdentical(real, stub))
        XCTAssertFalse(imagePixelsIdentical(stub, stub),
                       "two undecodable blobs are not a picture, identical or otherwise")
        XCTAssertFalse(imagePixelsIdentical(Data(), Data()))
    }

    /// *** The case this fix exists for. *** It used to assert the opposite,
    /// and inverting it is the whole of what changed in v3.1's second pass.
    ///
    /// The first implementation converted both images INTO sRGB before
    /// comparing, which made the fix inert on the machine it was written for.
    /// GPaste strips the colour profile with the same motion that strips
    /// `pHYs`, so a profile difference is not an edge case — it is part of the
    /// firing CONDITION, present exactly whenever the fix is needed. Measured
    /// on a real screenshot from the owner's Mac: 12,530 of 76,800 bytes
    /// differed, max delta **2** — rounding from converting a display-tagged
    /// original into sRGB while its untagged re-encode is already read as sRGB.
    /// The same picture, and the comparison said no.
    ///
    /// The question being asked is not "are these the same picture" but "is
    /// the peer's version derived from mine". Equal samples are the evidence
    /// of derivation, and the profile is then not a difference to see past —
    /// it is the thing being rescued.
    ///
    /// So `normalizedRGBA` re-tags rather than converts, and this case must be
    /// IDENTICAL. If it ever goes back to `false`, the fix has silently
    /// stopped working for every real screenshot on a wide-gamut display,
    /// while every other test in this file stays green.
    func testAWideGamutOriginalAgainstAnUntaggedCopyIsIdentical() {
        let samples = TestPNG.samples(width: 9, height: 7)
        let p3 = TestPNG.encode(samples, width: 9, height: 7, dpi: 144,
                                space: CGColorSpace(name: CGColorSpace.displayP3)!)
        XCTAssertTrue(imagePixelsIdentical(p3, TestPNG.strippingAncillaryChunks(p3)),
                      "a stripped wide-gamut image carries the same samples, which is the "
                      + "evidence that it was derived from ours -- the profile is what we keep")
        XCTAssertTrue(imagePixelsIdentical(p3, p3), "the same tagged bytes are still the same picture")
    }

    /// Different SAMPLES must still be different, or re-tagging would have
    /// turned the comparison into "same dimensions" and thrown away a picture
    /// the user was actually sent.
    func testDifferentSamplesAreStillDifferentAfterRetagging() {
        let a = TestPNG.samples(width: 9, height: 7)
        var b = a
        b[0] = b[0] &+ 40
        let p3 = TestPNG.encode(a, width: 9, height: 7, dpi: 144,
                                space: CGColorSpace(name: CGColorSpace.displayP3)!)
        let other = TestPNG.encode(b, width: 9, height: 7, dpi: 144,
                                   space: CGColorSpace(name: CGColorSpace.sRGB)!)
        XCTAssertFalse(imagePixelsIdentical(p3, other))
    }

    /// A truncated PNG -- a real failure mode for content that crossed a wire
    /// -- decodes to nothing here rather than to a partial image that might
    /// compare equal to something.
    func testATruncatedImageIsNeverIdentical() {
        let png = TestPNG.make(width: 6, height: 6)
        XCTAssertFalse(imagePixelsIdentical(png, png.prefix(png.count / 2)))
    }

    /// A greyscale image can never take the density fix, and the consequence
    /// is worth pinning rather than leaving as an inference from
    /// `normalizedRGBA`'s `copy(colorSpace:)` line. `CGImage.copy(colorSpace:)`
    /// returns nil when the colour MODELS disagree -- monochrome against RGB --
    /// so a greyscale pair answers `false` here even when the two are the same
    /// bytes, and `handleFrame` then applies the peer's copy exactly as it did
    /// before v3.1.
    ///
    /// That is the safe direction (a wrong `false` costs what today already
    /// costs; a wrong `true` throws away a picture the user was sent), and it
    /// is not a crash, which is the other thing this asserts. What it costs is
    /// real and stated: a greyscale screenshot from a retina display still
    /// comes back at double size. No screenshot on the owner's Mac is
    /// greyscale, which is why this is recorded rather than fixed.
    func testAGreyscaleImageIsNeverIdenticalToAnything() {
        let grey = TestPNG.greyscale(width: 9, height: 7, dpi: 144)

        XCTAssertFalse(imagePixelsIdentical(grey, TestPNG.strippingAncillaryChunks(grey)),
                       "a greyscale pair cannot be normalised into RGBA, so the fix never "
                       + "fires for one -- safe, and the peer's copy is applied as before")
        XCTAssertFalse(imagePixelsIdentical(grey, grey),
                       "not even against itself -- the answer is about the colour model, not "
                       + "about the pixels")
    }

    // MARK: - the diagnostic that settles whether the samples moved

    /// The line has to carry enough to answer the question it exists for, and
    /// the dimensions are half of that: a count of differing bytes means
    /// nothing without the size of the picture it came from.
    ///
    /// One sample apart, by a known amount, so both numbers are checkable
    /// rather than merely present.
    func testTheDifferenceReportNamesTheDimensionsTheCountAndTheDelta() {
        var samples = TestPNG.samples(width: 4, height: 4)
        let original = TestPNG.encode(samples, width: 4, height: 4, dpi: 144)
        samples[9] = samples[9] &+ 3
        let altered = TestPNG.encode(samples, width: 4, height: 4)

        XCTAssertEqual(imagePixelDifference(original, altered),
                       "4x4: 1 of 64 bytes differ, max delta 3")
    }

    /// Nothing to say when the samples match, which is the answer the release
    /// is hoping for: the line never appears at all for a re-encode that left
    /// the pixels alone.
    func testAStrippedCopyHasNoDifferenceToReport() {
        let original = TestPNG.make(width: 9, height: 7, dpi: 144)

        XCTAssertNil(imagePixelDifference(original, TestPNG.strippingAncillaryChunks(original)))
    }

    /// *** The bound. *** The first version walked every byte of both buffers
    /// with `zip`: 2.149 s for a 2560x1600 pair, on the channel's decode
    /// thread, for a log line. `FrameConstants.maxImageBytes` bounds the PNG,
    /// not the pixel count, so nothing about the frame cap bounded that -- and
    /// this is the path the release intends to exercise on every reconnect.
    ///
    /// So it counts to `differenceReportCap` and reports a floor. This pair is
    /// two different pictures of a size that makes the cap fire, and the "at
    /// least" wording is what proves it stopped rather than finished: an
    /// unbounded walk would report a number in the hundreds of thousands, and
    /// a bounded one that forgot to SAY so would be a lie in the log.
    ///
    /// The delta is left unpinned on purpose, and it is a floor for the same
    /// reason the count is: it is the largest gap among the bytes the scan
    /// reached before it stopped, not the largest in the picture. Asserting a
    /// literal there would be asserting where the cap happened to land.
    func testTheDifferenceReportStopsCountingOnceTheAnswerIsObvious() throws {
        let a = TestPNG.make(width: 200, height: 200, seed: 0, dpi: 144)
        let b = TestPNG.make(width: 200, height: 200, seed: 5)

        let reported = try XCTUnwrap(imagePixelDifference(a, b))

        XCTAssertTrue(reported.hasPrefix("200x200: at least "
                                         + "\(ImagePixelConstants.differenceReportCap) of 160000 "
                                         + "bytes differ, max delta at least "),
                      "got: \(reported)")
    }

    /// The other extreme, and the one the cap does nothing for: a pair
    /// differing in a single byte still has to be walked to the end to know
    /// that. The `memcmp` stride is what keeps that walk cheap, and this is
    /// the assertion that it walks it CORRECTLY -- a stride that skipped a
    /// chunk it should have looked at reports nothing at all here, and the
    /// exact count and delta pin the byte it found.
    ///
    /// The differing byte sits in the final chunk on purpose: 40,000 bytes of
    /// buffer against a 4,096-byte stride, so it is found by the tenth
    /// comparison and not the first.
    func testASingleDifferingByteInTheLastChunkIsStillFound() {
        var samples = TestPNG.samples(width: 100, height: 100)
        let original = TestPNG.encode(samples, width: 100, height: 100, dpi: 144)
        samples[samples.count - 2] = samples[samples.count - 2] &+ 7
        let altered = TestPNG.encode(samples, width: 100, height: 100)

        XCTAssertGreaterThan(samples.count, ImagePixelConstants.differenceChunkBytes,
                             "the buffer has to span several strides, or this proves nothing")
        XCTAssertEqual(imagePixelDifference(original, altered),
                       "100x100: 1 of 40000 bytes differ, max delta 7")
    }

    /// Different dimensions never reach the byte scan: the answer is the shape
    /// difference, and it is the one the reader needs.
    func testDifferentDimensionsAreReportedAsSuch() {
        XCTAssertEqual(imagePixelDifference(TestPNG.make(width: 4, height: 3),
                                            TestPNG.make(width: 3, height: 4)),
                       "different dimensions (4x3 vs 3x4)")
    }

    /// Bytes that will not decode are reported as such rather than crashing or
    /// claiming a pixel difference nobody measured.
    func testUndecodableBytesAreReportedAsUndecodable() {
        let stub = Data([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x07])

        XCTAssertEqual(imagePixelDifference(stub, TestPNG.make(width: 3, height: 3)),
                       "one of them did not decode")
    }
}
