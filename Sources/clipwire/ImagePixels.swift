// Sources/clipwire/ImagePixels.swift
import CoreGraphics
import Foundation
import ImageIO

// See StatusFile.swift's StatusConstants for why this is `static let` on an
// enum rather than a bare top-level `let`.
enum ImagePixelConstants {
    /// One byte per channel, RGBA, no padding between rows.
    static let bytesPerPixel = 4
    static let bitsPerComponent = 8

    /// How many differing bytes `imagePixelDifference` counts before it stops
    /// counting and reports a floor instead. The log line exists to answer one
    /// question -- did the samples move, or is this a different picture -- and
    /// four thousand differing bytes answers it exactly as well as sixteen
    /// million do, at a bounded price on the channel's decode thread.
    static let differenceReportCap = 4096

    /// The stride `imagePixelDifference` compares with `memcmp` before it
    /// looks at individual bytes. Whole chunks that match are skipped, so the
    /// other extreme -- two buffers differing in a handful of bytes, where the
    /// cap above never fires -- does not pay for a byte-by-byte walk either.
    static let differenceChunkBytes = 4096
}

/// Whether two encoded images are the same picture -- the question
/// `handleFrame`'s `.imageClip` case asks before deciding whose bytes to
/// keep.
///
/// **Defined operationally, because the loose reading has a trap.** "Identical
/// pixels" here means byte equality of the two RGBA buffers after decoding both
/// images into one fixed layout -- one channel order, one alpha layout, eight
/// bits a component -- with each image **re-tagged** as sRGB rather than
/// converted into it, so the samples pass through untouched.
///
/// Re-tagging rather than converting is the whole point, and the first version
/// of this file got it wrong. GPaste does not merely drop `pHYs` when it
/// re-encodes; it drops the colour profile with it (`iCCP`, `sRGB`, `gAMA`,
/// `cHRM` -- everything ancillary). So a profile difference is not an edge
/// case: it is part of the firing CONDITION, present exactly whenever this fix
/// is needed. Converting both into sRGB made the comparison answer "different"
/// for every real screenshot on the machine this was written for -- measured,
/// 12,530 of 76,800 bytes, max delta **2**, which is rounding and not a
/// picture.
///
/// The question being asked is not "are these the same picture" but **"is the
/// peer's version derived from mine"**. Equal samples are the evidence of
/// derivation, and the profile is then not a difference to see past -- it is
/// the thing being rescued. Keeping the local bytes is right because they are
/// the original, not because the two are interchangeable.
///
/// Measured on this Mac rather than assumed, because the whole fix rests on
/// it. Identical, decoded through this exact pipeline:
///
/// - bare PNG vs the same PNG carrying `pHYs`
/// - bare vs the same plus `sRGB` + `gAMA` + `cHRM`
/// - a **Display P3**-tagged PNG vs an untagged re-encode of it -- the case a
///   real screenshot from a wide-gamut display actually produces
/// - a real `screencapture -c` PNG (`IHDR iCCP eXIf pHYs iTXt iDOT IDAT IDAT
///   IEND`, 160x120 pixels displaying at 80x60) against a re-encode that drops
///   `iCCP` and `pHYs`
///
/// Different **samples** still compare different -- re-tagging does not turn
/// this into a dimension check. That is pinned by its own test.
///
/// One premise here is not measured: that GPaste's re-encode leaves the samples
/// alone. The harness's fake preserves them by construction and so cannot
/// answer it, and the only real observation is that the byte count grew from
/// 105,700 to 180,287, which proves different filtering and says nothing about
/// samples. `imagePixelDifference` below logs the evidence on the mismatch
/// path so the first real reconnect settles it. If it turns out they move, no
/// pixel comparison can work and the answer is provenance instead -- the PC
/// announcing the hash it was GIVEN beside the hash it read back.
///
/// **Every uncertain answer is `false`.** Undecodable bytes, a context that
/// cannot be allocated, a decoder that reports no pixel data: all of them mean
/// "not identical". A wrong `false` costs what today already costs -- the
/// peer's bytes are applied. A wrong `true` throws away the peer's image and
/// leaves the user pasting something they were never sent.
///
/// **A greyscale image is one of those `false`s, always**, and the consequence
/// is worth naming rather than leaving to be re-derived from
/// `normalizedRGBA`'s `copy(colorSpace:)` line: `CGImage.copy(colorSpace:)`
/// refuses to re-tag across colour MODELS, monochrome against RGB, so a
/// greyscale pair cannot be normalised at all and answers `false` even when
/// the two are byte-identical. The density fix therefore never fires for one,
/// and a greyscale retina screenshot still comes back from the PC at double
/// size, exactly as everything did before v3.1. That is the safe direction and
/// it is not a crash, which is what `testAGreyscaleImageIsNeverIdenticalToAnything`
/// pins; it is recorded rather than fixed because nothing on the owner's Mac
/// produces greyscale screenshots.
///
/// Not `NSBitmapImageRep`, despite `SystemPasteboard` using it for the
/// TIFF->PNG conversion next door: its `bitmapData` layout follows the SOURCE
/// image (channel order, alpha position, bit depth, colour space all vary with
/// what was decoded), which is precisely the "same format" this function has
/// to impose. Drawing into a context this code specifies is the only way to
/// get one layout for both sides.
///
/// Cost: two full decodes plus two `width * height * 4` buffers, on the
/// channel's decode thread, once per incoming image that lands on a pasteboard
/// already holding one. The dimension check below runs first and is nearly
/// free -- `CGImageSourceCreateImageAtIndex` parses the header without
/// decoding pixel data -- so images that are obviously different cost nothing.
/// The PNGs themselves are bounded by `FrameConstants.maxImageBytes` on the
/// way in.
func imagePixelsIdentical(_ lhs: Data, _ rhs: Data) -> Bool {
    guard let left = decodeImage(lhs), let right = decodeImage(rhs) else { return false }
    // Compared explicitly, not left to the buffers: a 4x1 and a 2x2 image
    // produce the same 16 bytes, so equal buffers alone would call two
    // differently shaped pictures the same one and keep the wrong bytes.
    guard left.width == right.width, left.height == right.height else { return false }
    guard let leftPixels = normalizedRGBA(left), let rightPixels = normalizedRGBA(right) else {
        return false
    }
    return leftPixels == rightPixels
}

/// The header only, at this point: Core Graphics defers the pixel decode until
/// something draws the image, which is what makes the dimension check above
/// cheap.
private func decodeImage(_ data: Data) -> CGImage? {
    guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
    return CGImageSourceCreateImageAtIndex(source, 0, nil)
}

/// The image's pixels in one canonical layout: sRGB, 8 bits a component,
/// R-G-B-A in memory order, alpha premultiplied, no row padding.
///
/// `byteOrder32Big` with `premultipliedLast` is what spells RGBA rather than
/// BGRA -- the pair is the format, and either half alone would leave the
/// channel order up to the platform.
///
/// Premultiplication is lossy, and precisely about what it should be: two
/// images differing only in the colour of FULLY TRANSPARENT pixels come out
/// byte-identical here. That is the right answer for this comparison -- those
/// are the same picture, and keeping the local bytes (which carry the
/// metadata) is correct for them -- but it is a real collapse rather than the
/// no-op it might read as, so it is written down. Opaque pixels are unaffected,
/// and a difference in any visible pixel survives.
///
/// The context starts zeroed and the draw composites over it. That is not a
/// blend against anything meaningful (there is nothing underneath), and it is
/// deterministic for a given input, which is all this comparison needs.
private func normalizedRGBA(_ image: CGImage) -> Data? {
    let width = image.width
    let height = image.height
    guard width > 0, height > 0 else { return nil }
    let bytesPerRow = width * ImagePixelConstants.bytesPerPixel
    guard let space = CGColorSpace(name: CGColorSpace.sRGB),
          let context = CGContext(data: nil, width: width, height: height,
                                  bitsPerComponent: ImagePixelConstants.bitsPerComponent,
                                  bytesPerRow: bytesPerRow, space: space,
                                  bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
                                      | CGBitmapInfo.byteOrder32Big.rawValue)
    else { return nil }
    // Re-tag as sRGB rather than converting into it, so the draw below moves
    // samples through unchanged and only normalises layout and alpha.
    //
    // Converting was the first implementation and it made this whole fix inert
    // on the machine it was written for. GPaste strips `iCCP` with the same
    // motion that strips `pHYs`, so a profile difference is part of the firing
    // CONDITION, present exactly whenever the fix is needed. Measured on a real
    // screenshot from the owner's Mac: 12,530 of 76,800 bytes differed, max
    // delta 2 -- rounding from converting a display-tagged original into sRGB
    // while its untagged re-encode is already read as sRGB. Same picture, and
    // the comparison said no.
    //
    // The question this answers is not "are these the same picture" but "is
    // the peer's version derived from mine". Equal samples are the evidence of
    // that, and the profile is then not a difference to see past -- it is the
    // thing being rescued.
    //
    // `copy(colorSpace:)` returns nil when the models disagree (a greyscale
    // source), which falls through to `false` and today's behaviour.
    guard let retagged = image.copy(colorSpace: space) else { return nil }
    context.draw(retagged, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let pixels = context.data else { return nil }
    return Data(bytes: pixels, count: height * bytesPerRow)
}

/// Describes why two images did not compare equal, for the log line on the
/// mismatch path. Returns `nil` when there is nothing useful to say.
///
/// This exists because the fix it reports on rests on an unmeasured premise:
/// that GPaste's re-encode leaves the pixel samples alone. Our harness's fake
/// re-encoder preserves them by construction, so it cannot answer the
/// question, and the only real observation is that the byte count grew from
/// 105,700 to 180,287 -- which proves different filtering and says nothing
/// about samples.
///
/// So the release measures it. If the premise holds, this line never appears
/// for a re-encoded screenshot; if it appears with a small delta, the samples
/// moved and the whole comparison approach needs replacing with provenance --
/// having the PC announce the hash it was GIVEN alongside the hash it read
/// back, which needs no pixels at all and survives any transformation.
///
/// **Reports a floor rather than a count once the difference is obvious**, and
/// that is the difference between a diagnostic and a stall. The first version
/// walked `zip(a, b)` over every byte of both buffers: measured on a 2560x1600
/// same-dimension pair, 16,384,000 bytes, **2.149 s** on the channel's decode
/// thread -- against 0.077 s for `imagePixelsIdentical` next door, which is the
/// whole rest of the work. Nothing bounds that: `FrameConstants.maxImageBytes`
/// caps the PNG at 4 MiB, not the pixel count, and a large flat screenshot
/// compresses far below the cap. And this is the path the release intends to
/// exercise, on every reconnect, if the samples do move -- so it had to be
/// cheap on precisely the input it was written to explain. Measured again
/// after, on that same pair: **0.077 s**, which is the two decodes and nothing
/// measurable on top of them, in a debug build and a release one alike.
///
/// "At least N of M bytes differ, max delta at least D" settles the same
/// question. Rounding (samples that moved) reads as a small delta over a large
/// count; a different picture reads as a large delta. Neither answer needs the
/// exact total, and the exact total is the only thing the cap gives up.
func imagePixelDifference(_ lhs: Data, _ rhs: Data) -> String? {
    guard let left = decodeImage(lhs), let right = decodeImage(rhs) else {
        return "one of them did not decode"
    }
    guard left.width == right.width, left.height == right.height else {
        return "different dimensions (\(left.width)x\(left.height) vs \(right.width)x\(right.height))"
    }
    guard let a = normalizedRGBA(left), let b = normalizedRGBA(right) else {
        return "could not normalise one of them"
    }
    guard a.count == b.count else { return "different buffer sizes" }
    let found = boundedDifference(a, b)
    guard found.differing > 0 else { return nil }
    // The dimensions travel with every one of these lines, not only the
    // "different dimensions" one above: a count of differing bytes says
    // nothing without the size of the picture it came out of, and this line's
    // whole job is to be read months later by someone deciding whether the
    // premise held.
    let size = "\(left.width)x\(left.height)"
    guard found.truncated else {
        return "\(size): \(found.differing) of \(a.count) bytes differ, max delta \(found.maxDelta)"
    }
    return "\(size): at least \(found.differing) of \(a.count) bytes differ, "
        + "max delta at least \(found.maxDelta)"
}

/// Counts the bytes that differ between two equally sized buffers, and the
/// largest gap any pair of them showed, stopping at
/// `ImagePixelConstants.differenceReportCap` differences and saying so.
///
/// Two bounds, for the two ways a full walk gets expensive. `memcmp` over
/// `differenceChunkBytes` at a time skips matching regions wholesale, so a pair
/// differing in a few bytes costs a handful of `memcmp` calls rather than a
/// byte-by-byte walk; the cap stops the opposite case, where nearly every byte
/// differs and the answer was already obvious after the first chunk. Between
/// them, no input walks sixteen million bytes one at a time.
///
/// `withUnsafeBytes` rather than `zip`, and that alone was most of the 2.149 s:
/// `Data`'s iterator is not a pointer walk.
private func boundedDifference(_ a: Data, _ b: Data)
    -> (differing: Int, maxDelta: Int, truncated: Bool) {
    var differing = 0
    var maxDelta = 0
    var truncated = false
    a.withUnsafeBytes { lhs in
        b.withUnsafeBytes { rhs in
            guard let leftBase = lhs.baseAddress, let rightBase = rhs.baseAddress else { return }
            let count = min(lhs.count, rhs.count)
            var offset = 0
            while offset < count {
                let chunk = min(ImagePixelConstants.differenceChunkBytes, count - offset)
                if memcmp(leftBase + offset, rightBase + offset, chunk) != 0 {
                    for index in offset..<(offset + chunk) where lhs[index] != rhs[index] {
                        differing += 1
                        maxDelta = max(maxDelta, abs(Int(lhs[index]) - Int(rhs[index])))
                        if differing >= ImagePixelConstants.differenceReportCap {
                            truncated = true
                            return
                        }
                    }
                }
                offset += chunk
            }
        }
    }
    return (differing, maxDelta, truncated)
}
