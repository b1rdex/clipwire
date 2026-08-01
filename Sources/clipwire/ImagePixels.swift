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
    var differing = 0
    var maxDelta = 0
    for (x, y) in zip(a, b) where x != y {
        differing += 1
        maxDelta = max(maxDelta, abs(Int(x) - Int(y)))
    }
    guard differing > 0 else { return nil }
    return "\(differing) of \(a.count) bytes differ, max delta \(maxDelta)"
}
