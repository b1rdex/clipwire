#!/usr/bin/env python3
"""Prove a commit touched no executable line.

Strips comments from two git revisions of the same paths and diffs what is
left. An empty diff means the change was comment-only -- which is what the
v3.0.1 spec requires of the one repair commit, so that a reviewer never has
to tell a repaired bearing from a smuggled edit by reading.

Swift is stripped with a line scanner rather than a regex over the whole
file: `//` inside a string literal would otherwise eat the rest of the line.
The current sources contain no such literal and no block comments -- both
checked -- but the scanner does not depend on that staying true.

Python goes through `tokenize`, which is exact by construction: it knows a
`#` inside a string from a real comment, and 54 lines of the agent contain
both characters.
"""
import difflib
import io
import subprocess
import sys
import tokenize


def strip_swift(text):
    out = []
    for line in text.splitlines():
        result, i, in_string, escaped = [], 0, False, False
        while i < len(line):
            c = line[i]
            if escaped:
                escaped = False
            elif c == "\\" and in_string:
                escaped = True
            elif c == '"':
                in_string = not in_string
            elif c == "/" and not in_string and line[i:i + 2] == "//":
                break
            result.append(c)
            i += 1
        stripped = "".join(result).rstrip()
        if stripped:
            out.append(stripped)
    return "\n".join(out)


def strip_python(text):
    """Blank out comment spans in place, keeping every line where it was.

    Rebuilding from tokens would destroy the line structure and make the
    "n lines differ" count meaningless -- it would be counting tokens.

    A docstring is deliberately NOT stripped. It is a string expression the
    module evaluates, not a comment, so a change to one is a change to the
    file's behaviour by the same definition the rest of this tool uses. That
    is stricter than a human would be, and it is the right way round: the
    tool's job is to be unable to wave something through.
    """
    lines = text.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type != tokenize.COMMENT:
                continue
            row, col = tok.start[0] - 1, tok.start[1]
            lines[row] = lines[row][:col]
    except (tokenize.TokenError, IndentationError) as error:
        # Never silently fall back to a weaker comparison: a file that will
        # not tokenise must fail the check, not pass it by another route.
        raise SystemExit("tokenize failed, cannot verify: %r" % (error,))
    return "\n".join(line.rstrip() for line in lines if line.strip())


def at(rev, path):
    return subprocess.run(["git", "show", "%s:%s" % (rev, path)],
                          capture_output=True, text=True, check=True).stdout


def main(argv):
    if len(argv) < 3:
        raise SystemExit("usage: strip_comments.py <rev-before> <rev-after> <path>...")
    before, after, paths = argv[0], argv[1], argv[2:]
    # Refuse anything that is not source. Handed a markdown file, the Swift
    # scanner happily reports "2 executable lines differ" -- a confident,
    # meaningless verdict, which is worse than no answer at all.
    wrong = [p for p in paths if not p.endswith((".swift", ".py"))]
    if wrong:
        raise SystemExit("only .swift and .py can be checked; refusing: " + ", ".join(wrong))
    bad = []
    for path in paths:
        strip = strip_python if path.endswith(".py") else strip_swift
        try:
            b, a = strip(at(before, path)), strip(at(after, path))
        except subprocess.CalledProcessError:
            bad.append("%s: missing in one revision -- a repair commit adds no files" % path)
            continue
        if b != a:
            # A real diff, not a positional zip: one inserted line shifts
            # every line after it, and a zip would call them all changed --
            # reporting 1694 for a 30-line insertion, which reads as a
            # catastrophe rather than as the small thing it is.
            delta = [l for l in difflib.unified_diff(b.splitlines(), a.splitlines(), n=0)
                     if l[:1] in "+-" and l[:3] not in ("+++", "---")]
            bad.append("%s: %d executable line(s) differ" % (path, len(delta)))
            for line in delta[:6]:
                bad.append("      " + line[:100])
        else:
            print("  ok  %s -- comment-only" % path)
    if bad:
        print("\nNOT comment-only:")
        for line in bad:
            print("  " + line)
        return 1
    print("\nevery path is comment-only")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
