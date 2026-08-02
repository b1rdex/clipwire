#!/usr/bin/env python3
"""Prove a test file's split manifest reconstructs the original byte-for-byte.

v3.2.1 splits three large test files while production stays frozen. Frozen
production witnesses nothing about the tests -- a split can drop a test,
rename it out of discovery, or silently gut a file, and "git diff Sources/
is empty" would stay true throughout. This tool is one leg of the proof that
none of that happened: given a manifest naming which line INTERVALS of the
original file became which shard file, it checks that

  1. the intervals are disjoint and their union is the whole original file
     (no gap, no overlap, nothing left uncovered);
  2. concatenating the original's lines through those intervals reproduces
     the original blob byte-for-byte (independent of what the shard files
     actually contain -- this alone would catch a manifest whose intervals
     just happen to add up to the right LINE COUNT without being the right
     LINES, and it is the one check that would fail if the file used
     anything but bare `\\n` line endings, since interval arithmetic assumes
     that and nothing here re-derives it from content);
  3. each shard file on disk is exactly its declared scaffold plus its
     declared intervals, in the order the manifest lists them -- nothing
     added, nothing missing, nothing reordered.

The original is read with `git show <blob>:<path>`, never from the working
copy, so the proof does not depend on whatever produced the shard files --
a manifest can be checked before, during or after the split that it
describes, and against a blob nobody has looked at in months.

Every failure calls fail(), which prints a diagnostic naming the file and
the specific inconsistency and exits 1. Nothing here ever prints a passing
report for input it could not fully read: a manifest naming an unreadable
path, an unparsable JSON document, or a shard field of the wrong shape is
refused outright, the same way a missing revision is. A checker that goes
quiet and guesses is worse than one that stops -- this project has shipped
that defect three times before (scripts/comment_only.py's history), always
as a confident wrong number rather than an error.
"""
import hashlib
import json
import subprocess
import sys


def fail(message):
    """Refuse rather than report. The brief does not define this; this does:
    a diagnostic naming the file and the specific inconsistency, then exit 1.
    Never a bare traceback, never a silently wrong number."""
    sys.stderr.write("FAIL: %s\n" % message)
    sys.exit(1)


def git_show(rev, path):
    """Read `path` at `rev` as raw bytes -- never the working copy.

    Bytes, not text: splitlines()/join must reproduce the blob exactly,
    including whatever line terminators it actually uses. Decoding to str
    and re-encoding would let Python's universal-newline translation
    quietly launder a real difference (a CRLF file would decode and
    re-encode as if it were LF, and the byte-identity proof below would be
    proving something other than what it claims to).
    """
    r = subprocess.run(["git", "show", "%s:%s" % (rev, path)], capture_output=True)
    if r.returncode != 0:
        fail("could not read %s at %s -- %s" %
             (path, rev, r.stderr.decode("utf-8", "replace").strip()))
    return r.stdout


def read_working(path):
    """Read a produced/working-tree file as raw bytes. A missing or
    unreadable shard file is refused, not treated as empty -- an empty
    `want` list would otherwise make a missing file look like a file that
    correctly has no scaffold and no intervals."""
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        fail("could not read produced file %s -- %s" % (path, e))


def require(condition, message):
    if not condition:
        fail(message)


def validate_shape(manifest):
    """Check the manifest is well-formed BEFORE verify() trusts any of it.

    A malformed manifest (missing key, an interval that is not a
    [start, end] pair, an interval with end < start) must be refused by
    name, not surfaced later as a cryptic KeyError/TypeError or -- worse --
    silently misread as some other interval. This is the same "refuse
    rather than report" rule applied to the manifest document itself, not
    just to the files it names.
    """
    require(isinstance(manifest, dict), "manifest is not a JSON object")
    for key in ("original", "blob", "files"):
        require(key in manifest, "manifest is missing required key %r" % key)
    require(isinstance(manifest["original"], str) and manifest["original"],
            "manifest['original'] must be a non-empty path string")
    require(isinstance(manifest["blob"], str) and manifest["blob"],
            "manifest['blob'] must be a non-empty revision string")
    require(isinstance(manifest["files"], list) and manifest["files"],
            "manifest['files'] must be a non-empty list")
    for i, f in enumerate(manifest["files"]):
        where = "manifest['files'][%d]" % i
        require(isinstance(f, dict), "%s is not a JSON object" % where)
        for key in ("path", "scaffold_before", "scaffold_after", "intervals"):
            require(key in f, "%s is missing required key %r" % (where, key))
        require(isinstance(f["path"], str) and f["path"],
                "%s['path'] must be a non-empty string" % where)
        for side in ("scaffold_before", "scaffold_after"):
            require(isinstance(f[side], list) and all(isinstance(l, str) for l in f[side]),
                    "%s['%s'] must be a list of strings" % (where, side))
        require(isinstance(f["intervals"], list) and f["intervals"],
                "%s['intervals'] must be a non-empty list" % where)
        for j, iv in enumerate(f["intervals"]):
            ivwhere = "%s['intervals'][%d]" % (where, j)
            require(isinstance(iv, list) and len(iv) == 2 and
                    all(isinstance(x, int) and not isinstance(x, bool) for x in iv),
                    "%s must be a [start, end] pair of integers" % ivwhere)
            s, e = iv
            require(s >= 1, "%s starts at %d, must be >= 1" % (ivwhere, s))
            require(e >= s, "%s is [%d, %d], end must be >= start" % (ivwhere, s, e))


def verify(manifest):
    original_blob = git_show(manifest["blob"], manifest["original"])
    # splitlines(), NOT split(b"\n"): a file ending in a newline gives split()
    # one extra empty trailing element, so n would be one too many and the
    # totality check below would reject a manifest whose intervals are
    # exactly right. The interval maps for this release were computed with
    # splitlines() and this checker must agree with them (E12 in the plan).
    original = original_blob.splitlines()
    n = len(original)

    # 1. disjoint and total: every interval from every file, sorted by start,
    # must pick up exactly where the previous one left off, from line 1 to n.
    spans = [(s, e, f["path"]) for f in manifest["files"] for s, e in f["intervals"]]
    covered = sorted(spans)
    expect = 1
    for s, e, path in covered:
        if s != expect:
            fail("gap or overlap at line %d: next interval starts %d (in %s)" %
                 (expect, s, path))
        expect = e + 1
    if expect != n + 1:
        fail("intervals cover %d lines, file has %d" % (expect - 1, n))

    # 2. byte-identical reconstruction, in ORIGINAL line order (sorted by
    # start, same as check 1 -- a file's shards can be reordered in the
    # manifest without that alone being wrong; check 3 below is what catches
    # intervals listed out of order WITHIN one shard).
    rebuilt = b"\n".join(l for s, e, _ in covered for l in original[s - 1:e])
    if original_blob.endswith(b"\n"):
        # splitlines() ate the file's own trailing newline; splitlines() is
        # exactly why we do not see a spurious empty final element in check
        # 1, but it means rebuilding with a plain b"\n".join() is one byte
        # short whenever the source ends in one -- which all three of this
        # release's targets do (checked: both end `...}\n` / `...main()\n`).
        # Put back exactly the terminator that was there, not one
        # unconditionally: a blob with no final newline must not gain one.
        rebuilt += b"\n"
    if hashlib.sha256(rebuilt).digest() != hashlib.sha256(original_blob).digest():
        fail("reconstruction differs from the committed blob %s:%s (rebuilt %d bytes, "
             "blob %d bytes)" % (manifest["blob"], manifest["original"],
                                  len(rebuilt), len(original_blob)))

    # 3. every produced file is scaffold + its own intervals, IN THE ORDER
    # THE MANIFEST LISTS THEM -- unlike check 2, this is order-sensitive by
    # design, so a manifest that lists a file's own intervals out of order
    # is caught here even though check 1/2 cannot see it (check 1 only
    # counts lines; check 2 re-sorts across the whole manifest before
    # concatenating, so it reconstructs the ORIGINAL regardless of a single
    # file's internal interval order).
    for f in manifest["files"]:
        before = [line.encode("utf-8") for line in f["scaffold_before"]]
        after = [line.encode("utf-8") for line in f["scaffold_after"]]
        want_lines = before + [l for s, e in f["intervals"] for l in original[s - 1:e]] + after
        got_lines = read_working(f["path"]).splitlines()
        if want_lines != got_lines:
            first = next((i for i in range(min(len(want_lines), len(got_lines)))
                          if want_lines[i] != got_lines[i]), None)
            if first is not None:
                fail("%s is not scaffold + its declared intervals: differs at its line %d "
                     "-- want %r, got %r" %
                     (f["path"], first + 1, want_lines[first], got_lines[first]))
            fail("%s is not scaffold + its declared intervals: has %d line(s), want %d" %
                 (f["path"], len(got_lines), len(want_lines)))


def main(argv):
    if len(argv) != 2 or argv[0] != "verify":
        raise SystemExit("usage: split_manifest.py verify <manifest.json>")
    manifest_path = argv[1]
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError as e:
        fail("could not read manifest %s -- %s" % (manifest_path, e))
    try:
        manifest = json.loads(text)
    except json.JSONDecodeError as e:
        fail("manifest %s is not valid JSON -- %s" % (manifest_path, e))
    validate_shape(manifest)
    verify(manifest)
    total_intervals = sum(len(f["intervals"]) for f in manifest["files"])
    print("OK  %s reconstructs %s:%s byte-for-byte across %d file(s), %d interval(s)" %
          (manifest_path, manifest["blob"], manifest["original"],
           len(manifest["files"]), total_intervals))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
