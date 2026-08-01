"""Shared guts of the fake `wl-copy`, `wl-paste` and `gdbus` beside this file.

These three stand in for the Linux clipboard tools so that the real agent
(`agent/clipwire-agent.py`) can run on a Mac, against the real Swift binary,
in the pairing harness. They are deliberately NOT importable test doubles:
the agent forks them by name through `PATH`, exactly as it forks the real
ones on the PC, so what the harness exercises is the real subprocess
plumbing -- argv, pipes, exit codes, EOF -- and not a drawing of it.

THE SHARED STATE FILE
---------------------
All three read and write one JSON file, named by `$CLIPWIRE_FAKE_CLIPBOARD_STATE`.
It is shared so that EITHER side can change the clipboard: the agent writes
through `wl-copy`, and the harness writes the file directly to stand in for a
person copying on the PC. An `Update` follows any change by anyone (see the
fake `gdbus`), so the direct PC->Mac direction exists in the harness at all --
an `Update` per agent write would have covered only the echo direction.

    {
      "generation": "<opaque token, changed by every writer>",
      "types": ["image/png"],          # what --list-types prints, in order
      "body": "<base64>",              # served for ANY offered type
      "substitute": false              # see SUBSTITUTION below
    }

One body for the whole selection, not one per type, because that is what the
real wl-copy holds: it offers a list of MIME types and hands the same buffer
to whoever asks for any of them.

`generation` is opaque -- equality is the only operation on it. The fake
`gdbus` actually watches a hash of the whole file, so a harness that rewrites
the file without touching `generation` still produces an `Update`; the field
exists so that re-copying identical bytes is still a change, the way it is on
a real clipboard.

A missing file is an empty selection, which is a normal state. A missing
`$CLIPWIRE_FAKE_CLIPBOARD_STATE`, or one naming a directory that does not
exist, is a broken harness and exits 2 loudly -- see EXIT CODES.

Writers are assumed not to be concurrent. The agent writes the clipboard only
from its protocol loop, so its `wl-copy` invocations are serial; a harness
that writes the file directly while a `wl-copy` is in flight can lose one of
the two updates. There is no lock, on purpose: it would be a mechanism this
harness has no use for.

SUBSTITUTION
------------
GPaste does not keep the bytes it is handed. It takes the selection over and
re-offers a re-encode -- measured on the real PC at 105,700 bytes in and
180,287 out -- and that substitution is the direct cause of the density bug
this release fixes. With `"substitute": true`, `wl-copy` stores a re-encode of
the PNG it was given instead of the PNG itself, so a read returns a different
encoding of what was written.

It is a REAL re-encode (see reencode_png): inflate, unfilter to raw scanlines,
then re-filter and re-deflate, with IHDR/PLTE/tRNS carried over byte for byte.
The decoded pixels are therefore identical BY CONSTRUCTION, not by test --
which is the whole point, because the fix compares decoded pixels and a fake
that corrupted the image would make that comparison fail for the wrong reason
and prove nothing.

Every ancillary chunk is dropped, including `pHYs` (the density, which is the
bug) and `iCCP`/`sRGB`/`gAMA` (the colour profile). Dropping the profile is
faithful -- GPaste drops it too -- and it has a consequence the harness must
respect: **an image fixture must not carry a non-sRGB ICC profile**. A
Display-P3 original against an untagged re-encode decodes to genuinely
different RGBA once both are normalised to one colour space, and the pixel
comparison would go red for a real reason that looks exactly like the fix
failing.

Substitution happens at WRITE time, so the stored bytes are the re-encode and
every later read agrees with them. GPaste's real takeover is a second or two
after the copy; a delayed mode is deliberately not built here (the design
defers it), so the harness sees one state change and one `Update` where the
real machine sees two.

WHAT THEY COST, MEASURED ON THIS MAC
------------------------------------
An invocation is 25-30 ms warm; the first one of an agent run has been seen
taking 0.2-0.8 s, which is the cost of that process's first `python3` child
rather than anything here (deleting __pycache__ changes nothing). Both are
well inside SUBPROCESS_TIMEOUT (3 s), but the first is FORCE-LOGGED by the
agent, so "clipboard read (--list-types) took 0.8s" in a harness log is
expected and is not a hang.

A re-encode of a 1024x768 RGBA PNG takes 0.27 s -- the unfiltering is a
per-byte Python loop -- and it INFLATES: 4.2x on incompressible noise, and
GPaste's own measured growth was 1.7x. An image close to MAX_IMAGE_BYTES can
therefore come back over the limit, which the agent handles with a log line
("the clipboard re-offered an image of N bytes: over the image limit") and
which is real behaviour rather than an artefact of this fake.

EXIT CODES
----------
    0   did what was asked
    1   nothing to paste -- no selection, or the requested type is not
        offered. This is what the real tools report for an empty clipboard,
        and the agent reads it as "nothing to read", which is a normal state.
    2   the fake itself is misconfigured or was invoked in a way it does not
        model. NEVER 1, because 1 is indistinguishable from an empty
        clipboard: a harness pointed at nothing would look exactly like a
        harness with an empty clipboard, and would pass while testing
        nothing.

THE INVOCATION LOG
------------------
Every invocation appends a line to `<state file>.log`. It is not decoration:
the agent spawns `wl-copy` with **stderr on /dev/null** (WaylandClipboard.write),
so a fake `wl-copy` that failed would fail invisibly, and "the agent never
called us at all" is otherwise indistinguishable from "the clipboard was
empty". The harness reads this file to assert its own world.
"""
import base64
import json
import os
import struct
import sys
import time
import zlib

STATE_ENV = "CLIPWIRE_FAKE_CLIPBOARD_STATE"

# What the real wl-copy offers for a text selection: the type it was given
# plus the X11-era aliases. The agent's choose_kind() reads this list to
# decide text-versus-image, and its text body fetch asks for
# "text/plain;charset=utf-8" by name, so a harness writing the state file
# directly should offer at least that one.
TEXT_ALIASES = ("text/plain;charset=utf-8", "text/plain", "TEXT", "STRING", "UTF8_STRING")

EMPTY_STATE = {"generation": "0", "types": [], "body": "", "substitute": False}


# ---------------------------------------------------------------------------
# Process plumbing
# ---------------------------------------------------------------------------

def die(program, message):
    """Exit 2 with a complaint. See EXIT CODES: never 1."""
    line = "%s: %s" % (program, message)
    print(line, file=sys.stderr, flush=True)
    try:
        note(program, "FAILED %s" % message)
    except OSError:
        pass    # the log lives beside a state file we may not have found
    raise SystemExit(2)


def state_path(program):
    """The shared state file, or exit 2.

    The missing-directory check is the one that matters. Without it a state
    path pointing somewhere unwritable would make every read report an empty
    clipboard -- a green harness that tested nothing.
    """
    path = os.environ.get(STATE_ENV)
    if not path:
        die(program, "$%s is not set; this is a fake, it needs one" % STATE_ENV)
    path = os.path.abspath(path)
    if not os.path.isdir(os.path.dirname(path)):
        die(program, "$%s=%s names a directory that does not exist" % (STATE_ENV, path))
    return path


def note(program, message):
    """One line in the invocation log. Best-effort and never fatal: a fake
    that died because it could not write its own log would be worse than one
    that ran unobserved.

    O_APPEND with a single write() call, so concurrent fakes interleave whole
    lines rather than fragments.
    """
    path = os.environ.get(STATE_ENV)
    if not path:
        return
    line = "%.6f %s %s\n" % (time.time(), program, message)
    try:
        fd = os.open(os.path.abspath(path) + ".log",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# The state file
# ---------------------------------------------------------------------------

def load(path):
    """The current state, or EMPTY_STATE when there is no file yet.

    A file that is present but unreadable as JSON is NOT treated as empty: a
    truncated write would otherwise show up as "the clipboard was cleared",
    which is a plausible-looking lie. Callers get the exception.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return dict(EMPTY_STATE)
    state = json.loads(raw.decode("utf-8"))
    for key, value in EMPTY_STATE.items():
        state.setdefault(key, value)
    return state


def save(path, state):
    """Replace the state file atomically, so a reader polling it sees either
    the old snapshot or the new one and never half of each."""
    temporary = "%s.tmp.%d" % (path, os.getpid())
    with open(temporary, "wb") as handle:
        handle.write(json.dumps(state, indent=1, sort_keys=True).encode("utf-8"))
        handle.write(b"\n")
    os.replace(temporary, path)


def body_bytes(state):
    return base64.b64decode(state.get("body") or "")


def set_body(state, types, data):
    """The one place a write is assembled, so `generation` cannot be forgotten
    by one caller and remembered by another. Every other key the caller was
    holding -- `substitute`, and anything a harness added -- survives, because
    this mutates the state it was handed instead of building a fresh one.
    A fresh object would silently clear substitution mode on the agent's first
    write, and the image half of the harness would then pass while proving
    nothing."""
    state["types"] = list(types)
    state["body"] = base64.b64encode(data).decode("ascii")
    state["generation"] = "%d" % time.time_ns()
    return state


def is_text_type(mime):
    return mime.startswith("text/") or mime in ("TEXT", "STRING", "UTF8_STRING")


def offered_types_for(mime):
    """What a selection of this type offers.

    A plain-text copy offers the aliases the real wl-copy adds, because
    choose_kind() reads that list and the agent's own text write goes out as
    "text/plain;charset=utf-8". Anything else -- image/png, and any text type
    outside the alias list -- offers exactly what it was given, which is a
    simplification of the real tool and the only one the agent can see.
    """
    if mime in TEXT_ALIASES:
        return list(TEXT_ALIASES)
    return [mime]


# ---------------------------------------------------------------------------
# The PNG re-encoder
# ---------------------------------------------------------------------------

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

# Carried over into the re-encode. IHDR because the geometry must not change;
# PLTE because it is critical for colour type 3; tRNS because dropping it
# would change visible pixels, which is the one thing a re-encode standing in
# for GPaste must not do. Everything else -- pHYs, iCCP, sRGB, gAMA, cHRM,
# tEXt, eXIf ... -- is dropped, which is both what GPaste does and the entire
# point of this function.
KEPT_CHUNKS = (b"IHDR", b"PLTE", b"tRNS")

# Tried in order until the output differs from the input. The first is the
# realistic one: filter None on every scanline with maximum deflate is what a
# fast, naive encoder emits, and against a well-filtered original it GROWS the
# file the way GPaste's re-encode grew the measured screenshot. The rest exist
# only so that an input that happens to already be in that exact shape still
# produces different bytes; a substitution that silently returned its input
# would leave the harness green and blind.
_ENCODE_ATTEMPTS = (9, 1, 6)


class NotAPNG(Exception):
    """Raised for anything reencode_png does not model: not a PNG at all, an
    interlaced one, or a truncated one."""


def _chunks(data):
    if not data.startswith(PNG_MAGIC):
        raise NotAPNG("not a PNG (bad signature)")
    offset = len(PNG_MAGIC)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset:offset + 4])
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if len(body) != length:
            raise NotAPNG("truncated %s chunk" % kind.decode("ascii", "replace"))
        yield kind, body
        offset += 12 + length
    if offset != len(data):
        raise NotAPNG("trailing bytes after the last chunk")


def _chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def _paeth(a, b, c):
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    return b if pb <= pc else c


def _unfilter(raw, height, stride, step):
    """Raw scanlines, filters removed. `step` is the filter's byte distance to
    the pixel on the left -- ceil(bits per pixel / 8), never less than one.

    Nothing here interprets a pixel: bit depth and colour type only reach this
    function as `stride` and `step`, so every non-interlaced PNG works,
    including sub-byte palettes, and the scanlines that come out are the ones
    that went in.
    """
    lines = []
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        if at + 1 + stride > len(raw):
            raise NotAPNG("image data is shorter than the header says")
        kind = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        if kind == 0:
            pass
        elif kind == 1:
            for i in range(step, stride):
                line[i] = (line[i] + line[i - step]) & 0xFF
        elif kind == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif kind == 3:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif kind == 4:
            for i in range(stride):
                left = line[i - step] if i >= step else 0
                upper_left = previous[i - step] if i >= step else 0
                line[i] = (line[i] + _paeth(left, previous[i], upper_left)) & 0xFF
        else:
            raise NotAPNG("unknown scanline filter %d" % kind)
        lines.append(bytes(line))
        previous = line
    if at != len(raw):
        raise NotAPNG("image data is longer than the header says")
    return lines


CHANNELS = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}


def reencode_png(data):
    """The same pixels, different bytes, no density and no colour profile.

    A genuine re-encode rather than a byte-mangle: the image data is inflated,
    unfiltered back to raw scanlines, then re-filtered and re-deflated. IHDR
    is copied byte for byte and the scanlines are reproduced exactly, so what
    a decoder gets out is identical -- by construction, which is what makes
    this fake usable as evidence for a fix that compares decoded pixels.

    Raises NotAPNG for anything it does not model. Interlaced PNGs are the
    real gap: their filtering runs per Adam7 pass, and nothing in this project
    produces one.
    """
    kept, compressed = [], []
    header = None
    for kind, body in _chunks(data):
        if kind == b"IDAT":
            compressed.append(body)     # one image, possibly split across chunks
            continue
        if kind == b"IHDR":
            header = body
        if kind in KEPT_CHUNKS:
            kept.append((kind, body))
    if header is None or len(header) != 13:
        raise NotAPNG("no usable IHDR")
    if not compressed:
        raise NotAPNG("no image data")
    width, height, depth, colour, _, _, interlace = struct.unpack(">IIBBBBB", header)
    if interlace != 0:
        raise NotAPNG("interlaced PNGs are not modelled")
    if colour not in CHANNELS:
        raise NotAPNG("unknown colour type %d" % colour)

    bits = CHANNELS[colour] * depth
    stride = (width * bits + 7) // 8
    lines = _unfilter(zlib.decompress(b"".join(compressed)), height, stride, max(1, bits // 8))

    # Filter None on every scanline: the naive encoder's choice, and the one
    # that makes the output visibly a different encoding rather than a
    # near-copy of the input.
    scanlines = b"".join(b"\x00" + line for line in lines)
    for level in _ENCODE_ATTEMPTS:
        out = PNG_MAGIC
        for kind, body in kept:
            out += _chunk(kind, body)
        out += _chunk(b"IDAT", zlib.compress(scanlines, level))
        out += _chunk(b"IEND", b"")
        if out != data:
            return out
    raise NotAPNG("re-encoding reproduced the input byte for byte")
