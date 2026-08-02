#!/usr/bin/env python3
"""Prove a commit touches nothing but a `private`/`fileprivate` modifier.

Task 3 removes nine leading modifiers so a later task can move those nine
declarations into new files, without asking a reviewer to prove by eye that
nothing else moved with them. This is that proof, mechanical rather than
read: for two revisions of the same paths, every line that differs must
equal the old line with its first "private " or "fileprivate " substring
removed, and no line may be added or removed.

Reads through `git show <rev>:<path>`, never the working tree directly, so
comparing the dirty tree against a commit needs a revision for the dirty
side -- `git stash create` manufactures one (a real, unreferenced commit
object) without touching the working tree, the index, or the stash ref
list, which is what the release's own verification step relies on.

The line-count-mismatch message names the first line where the two
revisions actually diverge (a plain positional scan, not a diff engine --
exact for the single inserted/removed line this check exists to catch),
not just the before/after counts, so an added or removed line is
diagnosable without a second tool. Same reasoning as `split_manifest.py`'s
check-3 message enhancement (see task-1-report.md).

Textual, not syntax-aware: it looks for "private " or "fileprivate "
anywhere on the line, not specifically a leading modifier keyword before an
identifier. Every one of Task 3's nine real edits removes a leading
modifier at the start of the line (after indentation), so this distinction
does not change today's verdict -- but a future caller feeding it a line
where "private " appears inside a comment or string literal would get a
verdict about text, not about Swift access control. Recorded rather than
silently tightened: the brief specifies this check's shape, and Task 3 has
no case that needs the narrower rule.
"""
import subprocess
import sys

MODIFIERS = ("private ", "fileprivate ")


def at(rev, path):
    """The exact bytes of `path` as stored at `rev` -- never the working
    tree, so a working-tree-only checkout filter can never be in the loop
    between what was committed and what this checks."""
    result = subprocess.run(["git", "show", "%s:%s" % (rev, path)],
                             capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("%s: could not read %s:%s -- %s" %
                          (path, rev, path, result.stderr.strip()))
    return result.stdout


def strip_modifier(line):
    """`line` with its first "private " or "fileprivate " removed.

    A substring search, not a leading-token match -- see the module
    docstring's "textual, not syntax-aware" note. If neither modifier is
    present, `line` is returned unchanged, which -- since this is only ever
    compared against a `b` already known to differ from `a` -- fails the
    comparison rather than silently matching.
    """
    for modifier in MODIFIERS:
        if modifier in line:
            return line.replace(modifier, "", 1)
    return line


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
        if b != stripped:
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
        a_lines = at(rev_a, path).splitlines()
        b_lines = at(rev_b, path).splitlines()
        total += check(a_lines, b_lines, path)
        print("  ok  %s" % path)
    print("\n%d changed line(s) total, all bare modifier removals (private/fileprivate)" %
          total)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
