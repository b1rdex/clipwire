#!/usr/bin/env python3
"""Prove a split moved every test and broke none of them.

v3.2.1 splits three large test files while production stays frozen. Frozen
production is a witness to nothing on the test side: a split can drop a
test, rename it out of discovery, skip it, or gut an assertion, and
`git diff Sources/` stays empty throughout. This is leg two of the proof
that none of that happened -- the manifest checker (split_manifest.py) is
leg one, proving the TEXT moved intact; this proves the RUNNER still sees
the same tests, still executes all of them, and still passes all of them.

Two subcommands:

  baseline <rev>   Check out `rev` into a detached worktree, enumerate every
                   (class, method) pair BOTH suites' own runners report, and
                   print them sorted, one per line, prefixed `swift ` or
                   `python `. Pure enumeration -- nothing is executed.

  compare <rev>    Do that for `rev`, enumerate the same way for the current
                   working tree (no worktree needed -- it is already
                   checked out), and require the two pair MULTISETS to be
                   identical. Also runs both suites for real in the working
                   tree and requires failures/errors/skipped to all be zero
                   -- enumeration alone cannot tell an executed test from a
                   skipped one, so a manifest-perfect, skip-riddled split
                   would otherwise pass. Exits 0 only if every leg holds.

Pairs come from the runners -- `swift test list` and unittest's own loader
-- never from a grep of the sources, so a test that stopped being
discoverable is exactly as visible as one that was deleted outright. Do NOT
parse `unittest -v` output: the suites under test write their own log lines
(clipwire agent output) onto the same streams a verbose run would use, and
one landing mid-record corrupts a regex parse SILENTLY -- a confident wrong
number, this project's most frequent failure mode. The loader walk below
fails loudly instead of guessing.

Getting failures/errors/skipped without that same risk needs one adaptation
per language, since neither runner hands them back as cleanly as the pair
list:

  Python: run through unittest's own TextTestRunner with its per-test
  progress output redirected to a throwaway stream, then read testsRun /
  failures / errors / skipped straight off the TestResult object -- again
  structured data from the runner, not text.

  Swift: `swift test --xunit-output` was tried first and rejected -- on
  this toolchain it reports only the (empty, on this project) Swift Testing
  target and silently omits XCTest results entirely, which is exactly the
  "measurement, not the system, was wrong" trap this project's ledger keeps
  finding. XCTest has no structured alternative, so this falls back to
  reading the single fixed-format aggregate line `swift test` prints once,
  for the 'All tests' suite, after every individual class has reported --
  anchored to that suite name rather than taken as "the last number seen",
  and cross-checked against an independent count of the per-test
  `... skipped (...)` lines. If the two disagree, that is treated as a
  parse fault, not a tie-break -- refuse rather than guess which is right.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter


def fail(message):
    """Refuse rather than report: a diagnostic naming what went wrong, then
    exit 1. Never a bare traceback, never a confident wrong count."""
    sys.stderr.write("FAIL: %s\n" % message)
    sys.exit(1)


def note(message):
    """Progress/diagnostic text goes to stderr so stdout stays exactly the
    pair list (baseline) or the report (compare) -- pipeable, countable."""
    sys.stderr.write(message + "\n")


def run(cmd, **kwargs):
    """subprocess.run, but a timeout refuses through fail() like everything
    else here -- an uncaught TimeoutExpired would print a bare traceback,
    which is exactly the failure mode this whole file exists to avoid."""
    try:
        return subprocess.run(cmd, **kwargs)
    except subprocess.TimeoutExpired:
        fail("timed out running: %s" % " ".join(cmd))


def repo_root():
    r = run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if r.returncode != 0:
        fail("not inside a git repository -- %s" % r.stderr.strip())
    return r.stdout.strip()


# ---------------------------------------------------------------- worktrees

def worktree_at(root, rev):
    """A detached worktree at `rev`, under tempfile.mkdtemp() ($TMPDIR).
    Caller must remove_worktree() it, including on failure."""
    d = tempfile.mkdtemp(prefix="test_pairs_")
    note("creating a worktree at %s for %s (a fresh Swift build there takes "
         "1-2 minutes -- expected, not a hang)" % (d, rev))
    r = run(["git", "worktree", "add", "--detach", d, rev],
            cwd=root, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        # rmtree, not rmdir: a failed `git worktree add` can still have
        # partially populated `d` (e.g. it fails partway through checkout),
        # and rmdir raises on a non-empty directory -- an uncaught OSError
        # here would be exactly the bare traceback fail()'s own docstring
        # promises never to emit. ignore_errors=True because this is already
        # the failure path; a cleanup problem must not mask the real one.
        shutil.rmtree(d, ignore_errors=True)
        fail("could not create a worktree at %s -- %s" % (rev, r.stderr.strip()))
    return d


def remove_worktree(root, d):
    r = run(["git", "worktree", "remove", "--force", d],
            cwd=root, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        # Still try to reclaim the directory -- a leftover /tmp worktree is a
        # nuisance, but hiding the removal failure would be worse.
        note("warning: git worktree remove failed for %s -- %s" % (d, r.stderr.strip()))
        run(["git", "worktree", "prune"], cwd=root, capture_output=True, timeout=120)


# -------------------------------------------------------------- enumeration

# python: enumerate through unittest's OWN loader, in a subprocess rooted at
# agent/tests. This IS discover's mechanism -- not a grep of the sources --
# so it still cannot miss a module that stopped being importable.
ENUMERATE = r"""
import unittest, json
out = []
def walk(s):
    for t in s:
        walk(t) if isinstance(t, unittest.TestSuite) else out.append(t.id())
walk(unittest.TestLoader().discover("."))
# keep Class.method, drop the module: a split changes the module by design.
print(json.dumps(sorted(".".join(i.split(".")[-2:]) for i in out)))
"""


def python_pairs(root):
    tests_dir = os.path.join(root, "agent", "tests")
    r = run([sys.executable, "-c", ENUMERATE],
            cwd=tests_dir, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        fail("could not enumerate python tests in %s:\n%s" % (tests_dir, r.stderr[-2000:]))
    try:
        pairs = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        fail("python enumeration did not print JSON -- %s\nstdout tail:\n%s" %
             (e, r.stdout[-500:]))
    if not pairs:
        # Never legitimate for this repo -- more likely the wrong directory
        # or a collection that silently found nothing than a real 0-test
        # suite. Refuse rather than print a baseline of zero.
        fail("python discovery in %s found 0 tests" % tests_dir)
    return ["python " + p for p in pairs]


# swift: `swift test list` prints clipwireTests.Class/method
def swift_pairs(root):
    r = run(["swift", "test", "list"], cwd=root,
            capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        fail("could not list swift tests in %s:\n%s" % (root, r.stderr[-2000:]))
    pairs = sorted("swift " + l.split(".", 1)[1].replace("/", ".")
                    for l in r.stdout.splitlines()
                    if l.startswith("clipwireTests."))
    if not pairs:
        fail("swift test list in %s found 0 clipwireTests.* entries" % root)
    return pairs


# ---------------------------------------------------------------- run stats

RUN_STATS = r"""
import unittest, json, io, sys
suite = unittest.TestLoader().discover(".")
result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
stats = {"tests": result.testsRun, "failures": len(result.failures),
         "errors": len(result.errors), "skipped": len(result.skipped)}
with open(sys.argv[1], "w") as f:
    json.dump(stats, f)
"""


def python_run_stats(root):
    """Run the real suite once and return {tests, failures, errors, skipped}
    straight off unittest's own TestResult -- written to a file the
    subprocess controls, never parsed from its stdout/stderr, so an agent
    log line the tests print has nowhere to land that this reads."""
    tests_dir = os.path.join(root, "agent", "tests")
    fd, out_path = tempfile.mkstemp(prefix="test_pairs_pystats_", suffix=".json")
    os.close(fd)
    try:
        r = run([sys.executable, "-c", RUN_STATS, out_path],
                cwd=tests_dir, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            fail("python test run crashed in %s:\n%s" % (tests_dir, r.stderr[-2000:]))
        try:
            with open(out_path) as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            fail("could not read python run-stats output -- %s" % e)
    finally:
        try:
            os.remove(out_path)
        except OSError:
            pass


_SWIFT_SUMMARY_WITH_SKIP = re.compile(
    r"Executed (\d+) tests?, with (\d+) tests? skipped and (\d+) failures? "
    r"\((\d+) unexpected\) in")
_SWIFT_SUMMARY_NO_SKIP = re.compile(
    r"Executed (\d+) tests?, with (\d+) failures? \((\d+) unexpected\) in")
_SWIFT_SKIP_LINE = re.compile(r"^Test Case '.*' skipped \(", re.MULTILINE)


def swift_run_stats(root):
    """Run the real suite once and return {tests, failures, skipped}.

    `swift test --xunit-output` was measured against this project and
    rejected (module docstring). Falls back to the ONE aggregate line
    `swift test` prints for the 'All tests' suite -- searched from the END
    of output and anchored to that suite name, not "whichever Executed line
    comes last" blindly, since each individual test class prints its own
    such line first. Swift/XCTest has no separate errors bucket the way
    Python's unittest does (an uncaught error in a test also reports as a
    failure), so this returns only tests/failures/skipped.
    """
    r = run(["swift", "test"], cwd=root, capture_output=True, text=True, timeout=600)
    output = r.stdout + "\n" + r.stderr
    idx = output.rfind("Test Suite 'All tests'")
    if idx == -1:
        fail("could not find a \"Test Suite 'All tests'\" summary in swift test "
             "output (exit %d); last 2000 chars:\n%s" % (r.returncode, output[-2000:]))
    tail = output[idx:]
    m = _SWIFT_SUMMARY_WITH_SKIP.search(tail)
    if m:
        tests, skipped, failures, _unexpected = (int(x) for x in m.groups())
    else:
        m = _SWIFT_SUMMARY_NO_SKIP.search(tail)
        if not m:
            fail("could not parse the 'All tests' summary line:\n%s" % tail[:500])
        tests, failures, _unexpected = (int(x) for x in m.groups())
        skipped = 0
    # Cross-check against an independently derived count: every skipped test
    # prints its own "... skipped (N seconds)." line, regardless of the
    # aggregate. Disagreement means the parse itself is wrong -- refuse
    # rather than pick one of the two numbers to trust.
    counted_skips = len(_SWIFT_SKIP_LINE.findall(output))
    if counted_skips != skipped:
        fail("swift skip count disagrees: summary line says %d, counted %d "
             "individual '... skipped' lines -- refusing to guess which is right" %
             (skipped, counted_skips))
    return {"tests": tests, "failures": failures, "skipped": skipped}


# --------------------------------------------------------------- reporting

def report_pair_diff(rev, rev_pairs, current_pairs):
    """Multiset diff, not set diff, per pair: a duplicate lost on one side
    and kept on the other must not cancel out and vanish from the report."""
    missing = Counter(rev_pairs) - Counter(current_pairs)
    extra = Counter(current_pairs) - Counter(rev_pairs)
    for pair, n in sorted(missing.items()):
        print("missing (in %s, not in the working tree): %s  x%d" % (rev, pair, n))
    for pair, n in sorted(extra.items()):
        print("extra (in the working tree, not in %s): %s  x%d" % (rev, pair, n))


# -------------------------------------------------------------- subcommands

def cmd_baseline(argv):
    if len(argv) != 1:
        raise SystemExit("usage: test_pairs.py baseline <rev>")
    rev, = argv
    root = repo_root()
    wt = worktree_at(root, rev)
    try:
        pairs = swift_pairs(wt) + python_pairs(wt)
    finally:
        remove_worktree(root, wt)
    for p in sorted(pairs):
        print(p)
    return 0


def cmd_compare(argv):
    if len(argv) != 1:
        raise SystemExit("usage: test_pairs.py compare <rev>")
    rev, = argv
    root = repo_root()

    wt = worktree_at(root, rev)
    try:
        rev_pairs = swift_pairs(wt) + python_pairs(wt)
    finally:
        remove_worktree(root, wt)

    note("enumerating the working tree...")
    current_pairs = swift_pairs(root) + python_pairs(root)
    current_swift = [p for p in current_pairs if p.startswith("swift ")]
    current_python = [p for p in current_pairs if p.startswith("python ")]

    ok = True
    if sorted(rev_pairs) != sorted(current_pairs):
        ok = False
        report_pair_diff(rev, rev_pairs, current_pairs)

    note("running the python suite for real...")
    py_stats = python_run_stats(root)
    note("running the swift suite for real...")
    swift_stats = swift_run_stats(root)

    # Tie enumeration to execution in the SAME working tree: the count each
    # runner actually executed must equal the count its own lister/discover
    # enumerated moments earlier. Disagreement means the two calls saw a
    # different tree (or one instrument is lying), not something a manifest
    # comparison could ever explain.
    if swift_stats["tests"] != len(current_swift):
        ok = False
        print("swift: `swift test` executed %d tests but `swift test list` "
              "enumerated %d" % (swift_stats["tests"], len(current_swift)))
    if py_stats["tests"] != len(current_python):
        ok = False
        print("python: the real run executed %d tests but discovery enumerated %d" %
              (py_stats["tests"], len(current_python)))

    if py_stats["failures"] or py_stats["errors"] or py_stats["skipped"]:
        ok = False
        print("python: failures=%d errors=%d skipped=%d (all must be 0)" %
              (py_stats["failures"], py_stats["errors"], py_stats["skipped"]))
    if swift_stats["failures"] or swift_stats["skipped"]:
        ok = False
        print("swift: failures=%d skipped=%d (both must be 0)" %
              (swift_stats["failures"], swift_stats["skipped"]))

    if ok:
        print("OK  %d swift + %d python pairs match %s; "
              "python failures=%d errors=%d skipped=%d, swift failures=%d skipped=%d" %
              (len(current_swift), len(current_python), rev,
               py_stats["failures"], py_stats["errors"], py_stats["skipped"],
               swift_stats["failures"], swift_stats["skipped"]))
        return 0
    return 1


def main(argv):
    if not argv or argv[0] not in ("baseline", "compare"):
        raise SystemExit("usage: test_pairs.py baseline <rev> | test_pairs.py compare <rev>")
    cmd, rest = argv[0], argv[1:]
    return cmd_baseline(rest) if cmd == "baseline" else cmd_compare(rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
