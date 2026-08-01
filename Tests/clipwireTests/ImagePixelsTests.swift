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
    /// this test stay green. The test that discriminates between those two
    /// implementations is
    /// `testAWideGamutOriginalAgainstAnUntaggedCopyIsNotIdentical` below.
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

    /// *** A stated limit, not a requirement. *** A wide-gamut original whose
    /// profile the peer's copy has lost is genuinely a different picture once
    /// both are read in one colour space -- measured through this exact
    /// pipeline at 96 of 140 bytes differing, by up to 52/255 -- so the
    /// comparison says no and the incoming bytes are applied exactly as they
    /// were before v3.1. The density bug is not fixed for that image; nothing
    /// is made worse either.
    ///
    /// Pinned so the boundary is a decision rather than a surprise during the
    /// manual check on the real machines: a Display P3 screenshot will not
    /// take the keep-the-local-bytes path. If a later change makes this
    /// `true` on purpose -- by comparing raw samples instead of normalised
    /// ones, say -- this test should be deleted along with an explanation,
    /// not adjusted until it passes. `Tests/fakes/fake_clipboard.py` carries
    /// the matching constraint for the harness.
    func testAWideGamutOriginalAgainstAnUntaggedCopyIsNotIdentical() {
        let samples = TestPNG.samples(width: 9, height: 7)
        let p3 = TestPNG.encode(samples, width: 9, height: 7, dpi: 144,
                                space: CGColorSpace(name: CGColorSpace.displayP3)!)
        XCTAssertFalse(imagePixelsIdentical(p3, TestPNG.strippingAncillaryChunks(p3)),
                       "a stripped wide-gamut image reads as a different picture, and this "
                       + "comparison must not claim otherwise")
        XCTAssertTrue(imagePixelsIdentical(p3, p3), "the same tagged bytes are still the same picture")
    }

    /// A truncated PNG -- a real failure mode for content that crossed a wire
    /// -- decodes to nothing here rather than to a partial image that might
    /// compare equal to something.
    func testATruncatedImageIsNeverIdentical() {
        let png = TestPNG.make(width: 6, height: 6)
        XCTAssertFalse(imagePixelsIdentical(png, png.prefix(png.count / 2)))
    }
}
