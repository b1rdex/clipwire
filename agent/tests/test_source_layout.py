# agent/tests/test_source_layout.py
"""clipwire-agent.py declares its own table of contents; this pins the file
to it.

The file is not split, because install scp's exactly that one path -- so the
thing that keeps 3221 lines reviewable is a declared order rather than a
directory. An order nothing enforces drifts, and drift is this project's
recorded defect shape: three production defects lived BETWEEN two changes
that were each correct on their own. The banners are the declaration; these
tests are the enforcement.

This module reads the agent as TEXT and never imports it -- unlike every
other file here, which goes through agent_under_test. That is deliberate,
and it is what lets this file exist at all: the refactor it guards is
accepted on the evidence that no existing test changed by a byte, so the one
added test must be incapable of hiding a behaviour change. A regex over
comments cannot execute a line of agent code, so it cannot mask one even in
principle. It also keeps the check working when the agent's runtime imports
would not resolve on the machine running the suite.

Overlaps TestModuleDefinitionOrder in test_watcher.py by design: that one
pins the single rule the file had (nothing defined below the __main__
guard), this one generalises it. The older check is frozen, not replaced.
"""
import pathlib
import re
import unittest

AGENT = pathlib.Path(__file__).resolve().parents[1] / "clipwire-agent.py"

# The order the file actually has, hardcoded rather than derived. Without
# this, the suite would only prove the banners agree with the table of
# contents -- and code, banner and contents moved together would stay green,
# which is precisely the move the check exists to catch.
#
# Selftest at 8 sits AHEAD of the watchers at 9. That is not a tidy order and
# it is not a typo: an entry point usually goes last and this one does not.
# If a section really does move, this tuple is the thing to edit, on purpose.
SECTIONS = (
    "Frame codec",
    "Clip payloads",
    "Freshness",
    "Agent runtime",
    "class Agent",
    "Clipboard state",
    "Wayland clipboard",
    "Selftest",
    "Watchers and entry",
)

# A banner is three lines: a rule, a numbered title, another rule. Matching
# the whole block, not a lone "# 1. ..." line, is what stops an ordinary
# numbered comment somewhere in the file from being read as a section.
BANNER = re.compile(r"^# ={10,}\n^# (?P<title>\d+\. .+)\n^# ={10,}$", re.MULTILINE)

# Anchored, because an unanchored search for "class" would match this file's
# own "# 5. class Agent" banner and report a definition that is a comment.
TOP_LEVEL_DEFINITION = re.compile(r"^(def|class)\b", re.MULTILINE)

# "  1. Frame codec", the contents lines in the module docstring.
CONTENTS_ENTRY = re.compile(r"^ +(?P<number>\d+)\. (?P<name>.+)$", re.MULTILINE)


def section_name(title):
    """"3. Freshness -- decisions, ..." -> "Freshness". The text after the
    em dash is prose about the section and is free to be reworded."""
    return title.split(". ", 1)[1].split(" — ", 1)[0].strip()


class SourceLayoutTestCase(unittest.TestCase):
    def setUp(self):
        self.source = AGENT.read_text()
        self.banners = list(BANNER.finditer(self.source))

    def docstring(self):
        # The module docstring is the first triple-quoted block, after the
        # shebang. Splitting beats a regex here: the docstring contains
        # quotes and em dashes, and later docstrings must not be picked up.
        return self.source.split('"""')[1]


class TestSectionBanners(SourceLayoutTestCase):
    def test_every_declared_section_has_a_banner_exactly_once(self):
        found = [section_name(m.group("title")) for m in self.banners]
        for name in SECTIONS:
            self.assertEqual(
                found.count(name), 1,
                "section %r should have exactly one banner, found %d in %r"
                % (name, found.count(name), found))

    def test_banners_appear_in_the_declared_order(self):
        found = [section_name(m.group("title")) for m in self.banners]
        self.assertEqual(found, list(SECTIONS))

    def test_banners_are_numbered_from_one_in_file_order(self):
        numbers = [int(m.group("title").split(".", 1)[0]) for m in self.banners]
        self.assertEqual(numbers, list(range(1, len(SECTIONS) + 1)))

    def test_no_top_level_definition_precedes_the_first_banner(self):
        self.assertTrue(self.banners, "the file has no section banners at all")
        above = self.source[: self.banners[0].start()]
        stray = TOP_LEVEL_DEFINITION.findall(above)
        self.assertEqual(
            stray, [],
            "a top-level %s sits above the first banner, so it belongs to no "
            "section" % (stray[0] if stray else ""))


class TestTableOfContents(SourceLayoutTestCase):
    """The docstring's contents list is what a reader sees first, so it is
    the declaration the banners have to match -- not a summary of them."""

    def test_the_docstring_lists_every_section_in_order(self):
        listed = [m.group("name").strip()
                  for m in CONTENTS_ENTRY.finditer(self.docstring())]
        self.assertEqual(listed, list(SECTIONS))

    def test_the_banners_match_the_order_the_docstring_declares(self):
        listed = [m.group("name").strip()
                  for m in CONTENTS_ENTRY.finditer(self.docstring())]
        found = [section_name(m.group("title")) for m in self.banners]
        self.assertEqual(found, listed)


if __name__ == "__main__":
    unittest.main()
