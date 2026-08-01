#!/usr/bin/env python3
"""Tests for the fakes themselves. `python3 Tests/fakes/test_fakes.py`.

Deliberately NOT under agent/tests: that directory is what CI's Python leg
discovers, and wiring the fakes into CI belongs to the harness task, not to
this one.

What these pin is what a fake can get wrong SILENTLY -- an Update line the
agent's own parser rejects, a substitution that returns its input, a
substitution mode cleared by the first write, a monitor that only looks like
it is running. Every one of those leaves a harness green while it tests
nothing, which is the whole failure mode these fakes exist to remove.

The end-to-end proof is not here and cannot be: it is running the real agent
against these, watching its log say it is on the event path, and watching a
clip cross. That was done before they were handed over.
"""
import base64
import importlib.util
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import zlib

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fake_clipboard as fake                                   # noqa: E402

AGENT = HERE.parents[1] / "agent" / "clipwire-agent.py"


def load(name, path):
    """Import a file that has no .py on it -- the agent, and the fake gdbus.

    Both are executables with a __main__ guard, so importing them runs no
    command. Worth the four lines: it lets these tests check the Update line
    against the parser that has to accept it, and against the fake that
    actually emits it, instead of against two copies of it typed out here.
    """
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def chunk(kind, body):
    return (struct.pack(">I", len(body)) + kind + body
            + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))


def make_png(width=6, height=4, depth=8, colour=6, phys=True, palette=True,
             filters=(0, 1, 2, 3, 4)):
    """A PNG carrying pHYs and sRGB, and using every scanline filter -- so a
    substitution that skipped the unfiltering step could not pass: it would
    perturb the FILTERED bytes and the samples that came out would not be the
    original samples plus the keystream, which is what the tests below check.

    The scanline bytes are arbitrary rather than a picture. What a decoder
    gets out of them is still fully determined by the filters, which is all
    these tests need.

    Colour type 3 gets a full 256-entry palette, so that indices the loop above
    can generate (0-255) are all in range and the fixture is a valid PNG before
    anything is done to it.
    """
    stride = (width * fake.CHANNELS[colour] * depth + 7) // 8
    raw = bytearray()
    for y in range(height):
        raw.append(filters[y % len(filters)])
        raw += bytes((x * 13 + y * 7) & 0xFF for x in range(stride))
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(
        ">IIBBBBB", width, height, depth, colour, 0, 0, 0))
    if colour == 3 and palette:
        data += chunk(b"PLTE", bytes((index * 5) & 0xFF for index in range(256 * 3)))
    if phys:
        data += chunk(b"pHYs", struct.pack(">IIB", 5669, 5669, 1))   # 144 dpi
    data += chunk(b"sRGB", b"\x00")
    data += chunk(b"IDAT", zlib.compress(bytes(raw), 6)) + chunk(b"IEND", b"")
    return data


def kinds_of(data):
    found, at = [], 8
    while at + 8 <= len(data):
        (length,) = struct.unpack(">I", data[at:at + 4])
        found.append(data[at + 4:at + 8].decode("ascii", "replace"))
        at += 12 + length
    return found


def palette_of(data):
    return next(body for kind, body in fake._chunks(data) if kind == b"PLTE")


def header_of(data):
    return next(body for kind, body in fake._chunks(data) if kind == b"IHDR")


def scanlines(data):
    """What a decoder would see: raw scanlines, filters removed."""
    header, image = None, []
    for kind, body in fake._chunks(data):
        if kind == b"IHDR":
            header = body
        if kind == b"IDAT":
            image.append(body)
    width, height, depth, colour, _, _, _ = struct.unpack(">IIBBBBB", header)
    bits = fake.CHANNELS[colour] * depth
    return b"".join(fake._unfilter(zlib.decompress(b"".join(image)), height,
                                   (width * bits + 7) // 8, max(1, bits // 8)))


class ReencodeTests(unittest.TestCase):
    def test_every_pixel_moves(self):
        """The property that replaced "the pixels survive", and the reason the
        substitution was rewritten: the sample-preserving version let v3.1's
        density fix be tuned against a model kinder than the world, and ship
        inert. See SUBSTITUTION in fake_clipboard.py.

        Asserted as the EXACT keystream rather than as mere inequality, so this
        still pins the unfiltering: a substitution that perturbed the filtered
        bytes instead of the samples would differ from the original too, and
        only equality against `_perturb(original samples)` catches it.
        """
        original = make_png()
        moved = scanlines(fake.reencode_png(original))
        self.assertEqual(moved, fake._perturb(scanlines(original)))
        self.assertNotEqual(moved, scanlines(original))
        self.assertTrue(all(a != b for a, b in zip(moved, scanlines(original))),
                        "a sample that survived is a sample a comparison could match on")

    def test_the_pixels_move_for_every_colour_type_and_depth(self):
        """And the geometry survives every one of them, because the agent and
        the harness both decode what comes out: harsher than reality is the
        point, undecodable is not."""
        for colour in (0, 2, 4, 6):
            for depth in (8, 16):
                with self.subTest(colour=colour, depth=depth):
                    original = make_png(depth=depth, colour=colour)
                    stored = fake.reencode_png(original)
                    self.assertEqual(scanlines(stored), fake._perturb(scanlines(original)))
                    self.assertEqual(header_of(stored), header_of(original))

    def test_a_palette_image_moves_its_palette_or_is_refused(self):
        """Colour type 3's samples are INDICES, and moving an index points it
        past the end of PLTE -- an invalid PNG, which is the one thing this
        substitution must never emit. So the palette moves and the indices do
        not: every decoded pixel still changes, because every entry does.

        Which makes the palette the only lever there is, so an image that
        claims colour type 3 and carries no palette is REFUSED rather than
        handed back. Returning it would return the same picture, silently,
        which is the failure this substitution was rewritten to remove.
        """
        original = make_png(colour=3, depth=8)
        stored = fake.reencode_png(original)
        self.assertEqual(scanlines(stored), scanlines(original))
        self.assertEqual(palette_of(stored), fake._perturb(palette_of(original)))
        self.assertTrue(all(a != b for a, b in zip(palette_of(stored), palette_of(original))))
        self.assertEqual(header_of(stored), header_of(original))
        with self.assertRaises(fake.NotAPNG):
            fake.reencode_png(make_png(colour=3, depth=8, palette=False))

    def test_the_density_is_gone(self):
        self.assertIn("pHYs", kinds_of(make_png()))
        self.assertNotIn("pHYs", kinds_of(fake.reencode_png(make_png())))

    def test_the_colour_profile_is_gone_too(self):
        """Faithful: GPaste drops the ICC profile as well. It used to carry a
        rule with it -- that a fixture must not be tagged Display P3, because a
        profile difference would fail the pixel comparison for a real reason
        that looked like the fix failing. That rule is gone with the premise it
        protected: the comparison fails by design now."""
        self.assertNotIn("sRGB", kinds_of(fake.reencode_png(make_png())))

    def test_the_geometry_is_untouched(self):
        original = make_png(width=13, height=7)
        self.assertEqual(header_of(fake.reencode_png(original)), header_of(original))

    def test_the_bytes_actually_differ(self):
        """A substitution that returned its input would leave the harness
        green and blind."""
        original = make_png()
        self.assertNotEqual(fake.reencode_png(original), original)

    def test_substituting_twice_never_walks_back_to_the_original(self):
        """The deltas are odd for this: an involution -- XOR, or an even delta
        -- would make a second substitution a way BACK to the original picture,
        and a fake with a route back to the bytes it was given is a fake with a
        way to be kind by accident."""
        original = make_png()
        once = fake.reencode_png(original)
        twice = fake.reencode_png(once)
        self.assertNotEqual(twice, once)
        # Pinned before the `zip`s below, which truncate to the shorter side
        # and would all pass against an empty buffer.
        self.assertEqual(len(scanlines(twice)), len(scanlines(original)))
        for label, other in (("once", scanlines(once)), ("original", scanlines(original))):
            with self.subTest(against=label):
                self.assertTrue(all(a != b for a, b in zip(scanlines(twice), other)))

    def test_it_refuses_what_it_does_not_model(self):
        for data in (b"not a png at all", make_png()[:20], b""):
            with self.assertRaises(fake.NotAPNG):
                fake.reencode_png(data)

    def test_interlaced_is_refused_rather_than_mangled(self):
        header = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 1)
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" * 68)) + chunk(b"IEND", b""))
        with self.assertRaises(fake.NotAPNG):
            fake.reencode_png(data)


class ToolTestCase(unittest.TestCase):
    """Each test gets its own state file, and runs the fakes as PROCESSES --
    the way the agent reaches them."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.directory.name, "clipboard.json")
        self.addCleanup(self.directory.cleanup)

    def env(self, **extra):
        environment = dict(os.environ, CLIPWIRE_FAKE_CLIPBOARD_STATE=self.state)
        environment.update(extra)
        return environment

    def run_fake(self, name, *arguments, stdin=None, env=None):
        return subprocess.run([str(HERE / name)] + list(arguments), input=stdin,
                              capture_output=True, timeout=30,
                              env=self.env() if env is None else env)

    def state_now(self):
        with open(self.state) as handle:
            return json.load(handle)

    def write_state(self, **fields):
        state = dict(fake.EMPTY_STATE)
        try:
            state.update(self.state_now())
        except FileNotFoundError:
            pass
        state.update(fields)
        state["generation"] = str(time.time_ns())
        with open(self.state, "w") as handle:
            json.dump(state, handle)


class ClipboardTests(ToolTestCase):
    """The four invocations WaylandClipboard actually makes, verbatim."""

    def test_text_round_trip(self):
        self.assertEqual(self.run_fake("wl-copy", "--type", "text/plain;charset=utf-8",
                                       stdin="hello ✓".encode()).returncode, 0)
        listed = self.run_fake("wl-paste", "--list-types")
        self.assertEqual(listed.returncode, 0)
        self.assertEqual(listed.stdout.decode().splitlines(), list(fake.TEXT_ALIASES))
        body = self.run_fake("wl-paste", "-n", "--type", "text/plain;charset=utf-8")
        self.assertEqual((body.returncode, body.stdout), (0, "hello ✓".encode()))

    def test_image_round_trip(self):
        png = make_png()
        self.run_fake("wl-copy", "--type", "image/png", stdin=png)
        listed = self.run_fake("wl-paste", "--list-types")
        self.assertEqual(listed.stdout.decode().splitlines(), ["image/png"])
        body = self.run_fake("wl-paste", "--type", "image/png")
        self.assertEqual((body.returncode, body.stdout), (0, png))

    def test_stdin_is_drained_to_the_end(self):
        """The real one drains: 4 MiB returns in milliseconds even against a
        locked session. A fake that read part of it would hand the agent a
        BrokenPipeError the real tool never produces."""
        big = os.urandom(4 * 1024 * 1024)
        self.run_fake("wl-copy", "--type", "application/octet-stream", stdin=big)
        self.assertEqual(base64.b64decode(self.state_now()["body"]), big)

    def test_an_empty_selection_is_exit_one(self):
        self.write_state(types=[], body="")
        self.assertEqual(self.run_fake("wl-paste", "--list-types").returncode, 1)
        self.assertEqual(self.run_fake("wl-paste", "--type", "image/png").returncode, 1)

    def test_a_type_that_is_not_offered_is_exit_one(self):
        self.run_fake("wl-copy", "--type", "image/png", stdin=make_png())
        self.assertEqual(self.run_fake("wl-paste", "-n", "--type",
                                       "text/plain;charset=utf-8").returncode, 1)

    def test_a_missing_state_file_is_an_empty_clipboard(self):
        self.assertEqual(self.run_fake("wl-paste", "--list-types").returncode, 1)

    def test_a_broken_harness_is_exit_two_never_one(self):
        """1 means "nothing to paste", which the agent reads as a normal empty
        clipboard. A misconfigured fake reporting 1 would be a harness that
        passed while testing nothing.

        Each tool gets arguments it fully understands, so that what is being
        tested is the missing state file and not a rejected flag.
        """
        usual = {"wl-copy": ("--type", "text/plain;charset=utf-8"),
                 "wl-paste": ("--list-types",),
                 "gdbus": ("introspect",) + EventSourceTests.DEST}
        bare = dict(os.environ)
        bare.pop("CLIPWIRE_FAKE_CLIPBOARD_STATE", None)
        nowhere = dict(os.environ,
                       CLIPWIRE_FAKE_CLIPBOARD_STATE="/no/such/directory/state.json")
        for name, arguments in usual.items():
            for label, environment in (("unset", bare), ("nowhere", nowhere)):
                with self.subTest(name=name, environment=label):
                    self.assertEqual(
                        self.run_fake(name, *arguments, stdin=b"", env=environment).returncode, 2)

    def test_an_unmodelled_argument_is_loud(self):
        self.assertEqual(self.run_fake("wl-paste", "--primary", "--list-types").returncode, 2)
        self.assertEqual(self.run_fake("wl-copy", "--foreground", stdin=b"x").returncode, 2)

    def test_no_type_serves_the_first_one_offered(self):
        self.run_fake("wl-copy", "--type", "text/plain;charset=utf-8", stdin=b"preferred")
        self.assertEqual(self.run_fake("wl-paste", "-n").stdout, b"preferred")

    def test_the_newline_the_agent_suppresses(self):
        """-n is a modifier on --type, and the agent always passes it for
        text. Without it the real tool completes a text paste with a
        newline."""
        self.run_fake("wl-copy", "--type", "text/plain;charset=utf-8", stdin=b"no newline")
        self.assertEqual(self.run_fake("wl-paste", "--type",
                                       "text/plain;charset=utf-8").stdout, b"no newline\n")
        self.assertEqual(self.run_fake("wl-paste", "-n", "--type",
                                       "text/plain;charset=utf-8").stdout, b"no newline")


class SubstitutionTests(ToolTestCase):
    def test_a_write_comes_back_as_a_different_picture(self):
        """End to end through the processes, the contract the harness consumes:
        the bytes read back are not the bytes written, and neither are the
        pixels. Same geometry, because both sides decode it."""
        png = make_png()
        self.write_state(substitute=True)
        self.run_fake("wl-copy", "--type", "image/png", stdin=png)
        stored = self.run_fake("wl-paste", "--type", "image/png").stdout
        self.assertNotEqual(stored, png)
        self.assertNotIn("pHYs", kinds_of(stored))
        self.assertEqual(header_of(stored), header_of(png))
        # Equality against the keystream rather than `all(a != b for ... zip)`:
        # `zip` truncates to the shorter side, so the loose form would pass
        # against a `stored` that decoded to nothing at all.
        self.assertEqual(scanlines(stored), fake._perturb(scanlines(png)))

    def test_the_mode_survives_the_agent_writing(self):
        """A fresh state object per write would clear it, and the image half
        of the harness would then pass while proving nothing."""
        self.write_state(substitute=True, keep_me="a harness put this here")
        self.run_fake("wl-copy", "--type", "image/png", stdin=make_png())
        self.assertTrue(self.state_now()["substitute"])
        self.assertEqual(self.state_now()["keep_me"], "a harness put this here")

    def test_text_is_left_alone(self):
        self.write_state(substitute=True)
        self.run_fake("wl-copy", "--type", "text/plain;charset=utf-8", stdin=b"  spaced  ")
        self.assertEqual(self.run_fake("wl-paste", "-n", "--type",
                                       "text/plain;charset=utf-8").stdout, b"  spaced  ")

    def test_off_by_default(self):
        png = make_png()
        self.run_fake("wl-copy", "--type", "image/png", stdin=png)
        self.assertEqual(self.run_fake("wl-paste", "--type", "image/png").stdout, png)


class EventSourceTests(ToolTestCase):
    DEST = ("--session", "--dest", "org.gnome.GPaste",
            "--object-path", "/org/gnome/GPaste")

    def test_introspect_is_answered(self):
        """The decisive one. Serve only `monitor` and available() is false,
        make_watcher picks the plain poller, and every harness run passes
        while exercising the polling branch only."""
        self.assertEqual(self.run_fake("gdbus", "introspect", *self.DEST).returncode, 0)

    def test_introspect_refuses_a_name_this_fake_does_not_own(self):
        """org.gnome.GPaste2 is the INTERFACE, not the bus name, and probing
        it is a mistake this project has already made once."""
        wrong = ("--session", "--dest", "org.gnome.GPaste2",
                 "--object-path", "/org/gnome/GPaste")
        self.assertEqual(self.run_fake("gdbus", "introspect", *wrong).returncode, 1)

    def test_the_update_line_is_the_one_the_agent_parses(self):
        line = load("fake_gdbus", HERE / "gdbus").UPDATE_LINE
        self.assertEqual(line, "/org/gnome/GPaste: org.gnome.GPaste2.Update "
                               "('REPLACE', 'ALL', uint64 0)")
        parse = load("clipwire_agent_for_fakes", AGENT).parse_gpaste_line
        self.assertTrue(parse(line))
        self.assertTrue(parse(line + "\n"))     # how the pump actually sees it

    def test_monitor_emits_on_a_change_nobody_asked_it_to_watch(self):
        """Any change by anyone, not just the agent's own writes -- otherwise
        a person copying on the PC does not exist in the harness at all. And
        it arrives promptly, which is the flush: block-buffered output would
        hold this line indefinitely and the safety net would call the source
        dead from buffering."""
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"before").decode())
        monitor = subprocess.Popen([str(HERE / "gdbus"), "monitor"] + list(self.DEST),
                                   stdout=subprocess.PIPE, text=True, env=self.env())
        self.addCleanup(monitor.stdout.close)   # cleanups run last-registered-first
        self.addCleanup(monitor.wait)
        self.addCleanup(monitor.kill)
        self.assertIn("Monitoring signals", monitor.stdout.readline())
        time.sleep(0.2)
        self.write_state(body=base64.b64encode(b"a person copied this").decode())
        started = time.time()
        line = monitor.stdout.readline()
        self.assertEqual(line.rstrip("\n"),
                         load("fake_gdbus", HERE / "gdbus").UPDATE_LINE)
        self.assertLess(time.time() - started, 5, "the line was not flushed promptly")

    def test_monitor_leaves_when_its_parent_does(self):
        """The Mac has no PR_SET_PDEATHSIG and the agent's normal exit path
        never calls stop(), so without this every harness run leaks one."""
        parent = subprocess.Popen(
            [sys.executable, "-c",
             "import subprocess,sys,time\n"
             "p = subprocess.Popen([%r, 'monitor'] + %r, stdout=subprocess.DEVNULL)\n"
             "print(p.pid, flush=True)\n"
             "time.sleep(0.4)\n" % (str(HERE / "gdbus"), list(self.DEST))],
            stdout=subprocess.PIPE, text=True, env=self.env())
        self.addCleanup(parent.stdout.close)
        child = int(parent.stdout.readline())
        parent.wait(timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            if subprocess.run(["ps", "-p", str(child)], capture_output=True).returncode != 0:
                return
            time.sleep(0.1)
        self.fail("the monitor outlived its parent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
