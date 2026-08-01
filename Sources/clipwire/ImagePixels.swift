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
/// pixels" here means byte equality of the two RGBA buffers after decoding
/// BOTH images into one fixed format: one colour space (sRGB), one channel
/// order, one alpha layout, eight bits a component. GPaste does not merely
/// drop `pHYs` when it re-encodes; it drops the colour profile with it
/// (`iCCP`, `sRGB`, `gAMA`, `cHRM` -- everything ancillary), so two images
/// that are the same picture arrive tagged differently and "compare them as
/// they decode" yields differences that are real in the buffer and meaningless
/// on screen. Normalising both into one destination space is what makes the
/// comparison answer the question actually being asked.
///
/// Measured on this Mac rather than assumed, because the whole fix rests on
/// it. Identical samples, decoded through this exact pipeline:
///
/// - bare PNG vs the same PNG carrying `pHYs`: IDENTICAL
/// - bare vs the same plus `sRGB` + `gAMA` + `cHRM`: IDENTICAL
/// - the owner's own 259-byte retina fixture vs the harness's re-encode of it
///   (`Tests/fakes/fake_clipboard.py`, which drops exactly what GPaste drops):
///   IDENTICAL
/// - a **Display P3**-tagged PNG vs an untagged re-encode of it: DIFFER, 96 of
///   140 bytes, by up to 52/255
///
/// That last one is a real limit and is stated rather than hidden: if a
/// screenshot carries a wide-gamut ICC profile and the peer's copy comes back
/// without it, the two are genuinely different pictures once both are read in
/// one colour space, this returns `false`, and the incoming bytes are applied
/// exactly as they were before v3.1 -- the bug is not fixed for that image,
/// but nothing is made worse. `Tests/fakes/fake_clipboard.py`'s own docstring
/// carries the matching constraint for the harness: an image fixture must not
/// carry a non-sRGB profile, or the comparison goes red for a real reason
/// that looks exactly like the fix failing.
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
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    guard let pixels = context.data else { return nil }
    return Data(bytes: pixels, count: height * bytesPerRow)
}
