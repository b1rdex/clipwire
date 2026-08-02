#!/usr/bin/env python3
"""Prove a commit touches nothing but a `private`/`fileprivate` modifier.

Task 3 removes nine leading modifiers so a later task can move those nine
declarations into new files, without asking a reviewer to prove by eye that
nothing else moved with them. This is that proof, mechanical rather than
read: for two revisions of the same paths, every line that differs must
equal the old line with its leading "private " or "fileprivate " modifier
removed, and no line may be added or removed.

Reads through `git show <rev>:<path>`, never the working tree directly, so
comparing the dirty tree against a commit needs a revision for the dirty
side -- `git stash create` manufactures one (a real, unreferenced commit
object) without touching the working tree, the index, or the stash ref
list, which is what the release's own verification step relies on.

Captured and compared as exact bytes, not filtered text: `git show`'s
stdout is captured raw (no `text=True`) and decoded explicitly, then split
on "\n" alone -- never `str.splitlines()`, which treats "\r\n" and a bare
"\r" as a line boundary equivalent to "\n". A CRLF/LF difference in the
stored blob therefore shows up as changed line content, the same way
`git diff` shows it with `core.autocrlf` off, instead of disappearing
under Python's own universal-newline translation. (Fix round 1: an earlier
version passed `text=True` to `subprocess.run`, which performs exactly
that translation on capture -- confirmed to make a CRLF-only rewrite of
every line in a file compare as "0 changed lines", a vacuous pass on a
file where every byte differed. See task-3-report.md, fix round 1.)

The line-count-mismatch message names the first line where the two
revisions actually diverge (a plain positional scan, not a diff engine --
exact for the single inserted/removed line this check exists to catch),
not just the before/after counts, so an added or removed line is
diagnosable without a second tool. Same reasoning as `split_manifest.py`'s
check-3 message enhancement (see task-1-report.md).

Leading-modifier match, not a bare substring search (fix round 1): a line
counts as a modifier removal only when "private " or "fileprivate " is the
first token after indentation. Two failure modes this closes, both found
by review and reproduced before this line existed:

  - `"private " in line` matches inside `fileprivate` too, since
    "private " is a substring of "fileprivate " (starting at index 4) --
    so a genuine `fileprivate func f() {}` -> `func f() {}` removal used
    to strip the wrong eight characters, leaving a stray "file" prefix,
    and get rejected as "not a bare modifier removal". Anchoring the
    match at the start of the (indentation-stripped) line makes the two
    modifiers mutually exclusive: a line beginning "fileprivate " never
    also begins "private ", so which one is checked first no longer
    matters.
  - `"private "` inside a comment or string used to match too -- a pure
    comment edit like `// uses a private helper` -> `// uses a helper`
    was accepted as a bare modifier removal. A line where the modifier
    text appears anywhere other than as the leading token cannot be
    classified as a modifier removal and is rejected, not accepted: this
    check fails safe on ambiguous input rather than guessing.
"""
import subprocess
import sys

MODIFIERS = ("fileprivate ", "private ")


def at(rev, path):
    """The exact bytes of `path` as stored at `rev` -- never the working
    tree, so a working-tree-only checkout filter can never be in the loop
    between what was committed and what this checks.

    Captured without `text=True` (see module docstring, fix round 1):
    `subprocess.run(text=True)` performs universal-newline translation on
    the captured stream, which would silently erase the CRLF/LF
    distinction this function's contract promises to preserve. The raw
    bytes are decoded explicitly instead.
    """
    result = subprocess.run(["git", "show", "%s:%s" % (rev, path)],
                             capture_output=True)
    if result.returncode != 0:
        raise SystemExit("%s: could not read %s:%s -- %s" %
                          (path, rev, path,
                           result.stderr.decode("utf-8", "replace").strip()))
    return result.stdout.decode("utf-8")


def split_lines(text):
    """`text` split into lines the way `str.splitlines()` counts them for
    plain "\n"-terminated text (one trailing newline does not create an
    extra empty line; a genuine blank line before EOF still does), but
    without `splitlines()`'s byte-losing side effect of treating "\r\n"
    and a bare "\r" as a line boundary in their own right. Only the
    single trailing "\n", if present, is removed before splitting on "\n"
    alone -- so a "\r" anywhere, including one immediately before that
    final "\n", stays attached to its line as content, and a CRLF-only
    rewrite of a file shows up as every line having changed.
    """
    if text.endswith("\n"):
        text = text[:-1]
    return text.split("\n")


def strip_modifier(line):
    """`line` with its leading "private " or "fileprivate " modifier
    removed -- leading meaning the first token after indentation, not
    merely present somewhere on the line (see module docstring, fix
    round 1). Returns `None` if no such leading modifier is present,
    which -- since this is only ever compared against a `b` already known
    to differ from `a` -- fails the comparison in `check` rather than
    silently matching.
    """
    stripped = line.lstrip(" \t")
    indent = line[:len(line) - len(stripped)]
    for modifier in MODIFIERS:
        if stripped.startswith(modifier):
            return indent + stripped[len(modifier):]
    return None


def first_divergence(a_lines, b_lines):
    """1-based index of the first line where the two sequences differ, or
    one past the shorter sequence's end if one is a strict prefix of the
    other. Correct -- not just plausible -- for a single inserted or
    removed line with everything else byte-identical: content shifts by
    exactly one position starting at the change, so the first positional
    mismatch IS the change, not a guess at it."""
    for i, (a, b) in enumerate(zip(a_lines, b_lines), 1):
        if a != b:
            return i
    return min(len(a_lines), len(b_lines)) + 1


def check(a_lines, b_lines, path):
    if len(a_lines) != len(b_lines):
        i = first_divergence(a_lines, b_lines)
        raise SystemExit(
            "%s: line count changed: %d -> %d (first difference at line %d)" %
            (path, len(a_lines), len(b_lines), i))
    changed = 0
    for i, (a, b) in enumerate(zip(a_lines, b_lines), 1):
        if a == b:
            continue
        changed += 1
        stripped = strip_modifier(a)
        if stripped is None or b != stripped:
            raise SystemExit(
                "%s:%d is not a bare modifier removal:\n  was %r\n  now %r" %
                (path, i, a, b))
    return changed


def main(argv):
    if len(argv) < 3:
        raise SystemExit("usage: modifier_only.py <revA> <revB> <path>...")
    rev_a, rev_b, paths = argv[0], argv[1], argv[2:]
    total = 0
    for path in paths:
        a_lines = split_lines(at(rev_a, path))
        b_lines = split_lines(at(rev_b, path))
        total += check(a_lines, b_lines, path)
        print("  ok  %s" % path)
    print("\n%d changed line(s) total, all bare modifier removals (private/fileprivate)" %
          total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
