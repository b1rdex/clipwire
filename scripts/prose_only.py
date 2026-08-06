#!/usr/bin/env python3
"""Prove that two revisions of a Python file differ ONLY in prose.

"Prose" means comments and docstrings. `scripts/comment_only.py` cannot do
this job and says so in its own docstring: it deliberately does not strip
docstrings, so it reports any docstring edit as an executable change. Measured
on the v3.3 branch, it called that branch's own documentation commit "NOT
comment-only: 285 executable line(s) differ". A tool that fails on correct
work teaches whoever runs it to ignore it, which is worse than having no tool.

THE METHOD, and why it is stronger than a line-based diff: both revisions are
parsed to an AST, every docstring node is removed, and the two trees are
compared by `ast.dump()` WITHOUT attributes -- so line numbers, column offsets
and whitespace cannot enter the comparison, and neither can a comment, which
never reaches the AST at all. If the dumps are equal, the executable content
is identical by construction rather than by inspection.

A docstring-only body would become syntactically empty, so a `Pass` is put in
its place on BOTH sides; that keeps a function whose whole body is a docstring
comparing equal to the same function with `pass` instead, which is what "only
the prose changed" means for that edit.

Usage:
    prose_only.py <rev> <rev> <path>...     # compare two git revisions
    prose_only.py --files <a.py> <b.py>     # compare two files on disk

Exit 0 when every path differs only in prose, 1 otherwise.
"""
import ast
import subprocess
import sys


def strip_prose(tree):
    """Remove every docstring node, in place, and return the tree."""
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body.pop(0)
            if not body:
                body.append(ast.Pass())
    return tree


def skeleton(source, label):
    try:
        return ast.dump(strip_prose(ast.parse(source)))
    except SyntaxError as error:
        print("  %s does not parse: %s" % (label, error))
        return None


def git_show(rev, path):
    result = subprocess.run(["git", "show", "%s:%s" % (rev, path)],
                            capture_output=True)
    if result.returncode != 0:
        return None
    return result.stdout.decode("utf-8")


def prose_counts(source):
    """(total lines, comment lines, docstring lines) -- for the report only.

    Never part of the verdict: a count that happened to match would say
    nothing about whether the same code is underneath.
    """
    lines = source.splitlines()
    comments = sum(1 for line in lines if line.strip().startswith("#"))
    docs = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant) and \
                    isinstance(body[0].value.value, str):
                docs += body[0].end_lineno - body[0].lineno + 1
    return len(lines), comments, docs


def compare(before, after, label):
    a, b = skeleton(before, label + " (before)"), skeleton(after, label + " (after)")
    if a is None or b is None:
        return False
    ok = a == b
    t0, c0, d0 = prose_counts(before)
    t1, c1, d1 = prose_counts(after)
    print("  %s: %s" % (label, "PROSE ONLY" if ok else "CODE CHANGED"))
    print("      lines %d -> %d   comments %d -> %d   docstrings %d -> %d"
          % (t0, t1, c0, c1, d0, d1))
    return ok


def main(argv):
    if len(argv) >= 4 and argv[1] == "--files":
        before = open(argv[2]).read()
        after = open(argv[3]).read()
        return 0 if compare(before, after, argv[3]) else 1
    if len(argv) < 4:
        print(__doc__)
        return 2
    old, new, paths = argv[1], argv[2], argv[3:]
    ok = True
    for path in paths:
        before, after = git_show(old, path), git_show(new, path)
        if before is None or after is None:
            print("  %s: MISSING in one of the revisions" % path)
            ok = False
            continue
        ok = compare(before, after, path) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
