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
import ast
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
        moved = scanlines(fake.substitute_png(original))
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
                    stored = fake.substitute_png(original)
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
        stored = fake.substitute_png(original)
        self.assertEqual(scanlines(stored), scanlines(original))
        self.assertEqual(palette_of(stored), fake._perturb(palette_of(original)))
        self.assertTrue(all(a != b for a, b in zip(palette_of(stored), palette_of(original))))
        self.assertEqual(header_of(stored), header_of(original))
        with self.assertRaises(fake.NotAPNG):
            fake.substitute_png(make_png(colour=3, depth=8, palette=False))

    def test_the_density_is_gone(self):
        self.assertIn("pHYs", kinds_of(make_png()))
        self.assertNotIn("pHYs", kinds_of(fake.substitute_png(make_png())))

    def test_the_colour_profile_is_gone_too(self):
        """Faithful: GPaste drops the ICC profile as well. It used to carry a
        rule with it -- that a fixture must not be tagged Display P3, because a
        profile difference would fail the pixel comparison for a real reason
        that looked like the fix failing. That rule is gone with the premise it
        protected: the comparison fails by design now."""
        self.assertNotIn("sRGB", kinds_of(fake.substitute_png(make_png())))

    def test_the_geometry_is_untouched(self):
        original = make_png(width=13, height=7)
        self.assertEqual(header_of(fake.substitute_png(original)), header_of(original))

    def test_the_bytes_actually_differ(self):
        """A substitution that returned its input would leave the harness
        green and blind."""
        original = make_png()
        self.assertNotEqual(fake.substitute_png(original), original)

    def test_substituting_twice_never_walks_back_to_the_original(self):
        """The deltas are odd for this: an involution -- XOR, or an even delta
        -- would make a second substitution a way BACK to the original picture,
        and a fake with a route back to the bytes it was given is a fake with a
        way to be kind by accident."""
        original = make_png()
        once = fake.substitute_png(original)
        twice = fake.substitute_png(once)
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
                fake.substitute_png(data)

    def test_interlaced_is_refused_rather_than_mangled(self):
        header = struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 1)
        data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" * 68)) + chunk(b"IEND", b""))
        with self.assertRaises(fake.NotAPNG):
            fake.substitute_png(data)


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

    def top_uuid(self):
        """The uuid `history_uuid(state)` derives for the CURRENT state file
        -- the same function gpaste-client imports directly to answer `add`
        and `--raw get`, and that the fake gdbus's GetElementKind/Select
        check their uuid argument against. Read straight from the state
        file rather than by spawning `gdbus call GetElementAtIndex`, so a
        test of "did the uuid move" does not also depend on a second
        fake's argv parsing and text-truncation being correct."""
        return load("fake_gdbus", HERE / "gdbus").history_uuid(fake.load(self.state))

    def log_lines(self):
        """Every line a fake has appended to the invocation log so far (see
        fake_clipboard.note()). Some behaviour -- the monitor's baseline
        note -- has no other observable trace: it is never printed to
        stdout, only logged."""
        try:
            with open(self.state + ".log") as handle:
                return handle.read().splitlines()
        except FileNotFoundError:
            return []


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

    # v3.5's LockMonitor (agent/clipwire-agent.py): every call it makes --
    # session resolution, LockedHint reads, its own pump -- goes out on the
    # SYSTEM bus against org.freedesktop.login1, which this fake does not
    # model. No --object-path here: LockMonitor's own `gdbus monitor
    # --system --dest ...` (unlike its `call`s) never passes one either.
    LOGIN1 = ("--system", "--dest", "org.freedesktop.login1")

    def gdbus_call(self):
        """(uuid, text) for the top of history, parsed as the Python tuple
        literal `call_get_element_at_index` prints. Safe to parse with
        `ast.literal_eval` because that function strips every quote from the
        text first, so the two quoted fields can never contain one and the
        line is always well-formed Python syntax.

        Asserts the shape (exactly two fields) as well as returning them: a
        fake that printed one field, or three, would otherwise fail with a
        bare ValueError from the tuple-unpack at some call site far from
        here, instead of a named assertion at the point that actually saw
        the malformed output.
        """
        result = self.run_fake("gdbus", "call", *self.DEST,
                               "--method", "org.gnome.GPaste2.GetElementAtIndex", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        parsed = ast.literal_eval(result.stdout.decode())
        self.assertEqual(len(parsed), 2, result.stdout)
        return parsed

    def gdbus_uuid(self):
        return self.gdbus_call()[0]

    def start_gdbus_monitor(self):
        """A `gdbus monitor` subprocess of our own, stdout made non-blocking
        so `drain()` below can prove a NEGATIVE -- that nothing arrived in a
        window -- which a blocking `readline()` (as the other monitor tests
        use) cannot do without hanging forever on silence.

        The 0.2s sleep is generous next to POLL_SECONDS (0.05s in the fake):
        it has to outlast both process startup and the fake taking its
        baseline digest, or the first write a test makes could land before
        the fake has anything to compare it against, and be missed as "no
        change yet" rather than caught as a change.
        """
        process = subprocess.Popen([str(HERE / "gdbus"), "monitor"] + list(self.DEST),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, env=self.env())
        # Registered in the order that makes cleanup -- LIFO -- terminate,
        # then reap, then release the pipe: the same order
        # test_monitor_emits_on_a_change_nobody_asked_it_to_watch uses below.
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        os.set_blocking(process.stdout.fileno(), False)
        # 0.2s is generous next to POLL_SECONDS (0.05s), but this file's own
        # measured numbers (fake_clipboard.py, "WHAT THEY COST") say a cold
        # first invocation can take 0.2-0.8s -- so this drain, not a bare
        # read, is what keeps a slow-starting fake from raising instead of
        # just returning "nothing yet". Its return value (Update lines, of
        # which there cannot be any yet) is discarded along with the banner.
        self.drain(process, seconds=0.2)
        return process

    def drain(self, process, seconds):
        """Every Update line the monitor emitted within `seconds`.

        `TextIOWrapper.read()` -- what `text=True` on Popen wraps stdout in
        -- does NOT return None the way the raw/buffered binary layers do
        when a non-blocking read has nothing available: it RAISES
        BlockingIOError instead (message "read() returned None", confirmed
        empirically against this Python; a bare `read() or ""` crashes on
        the very first call, before anything has been written). When SOME
        text is already available it still returns that text normally --
        only the zero-bytes-available case raises -- so this catches
        exactly that one case rather than swallowing a real error.
        """
        time.sleep(seconds)
        try:
            text = process.stdout.read() or ""
        except BlockingIOError:
            text = ""
        return [line for line in text.splitlines() if "Update" in line]

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

    def test_introspect_refuses_a_stray_positional(self):
        """Positional arguments belong to `call` alone -- GetElementAtIndex's
        index. Scoping their acceptance to `call` must not reopen the door for
        introspect: a stray word after its flags is exactly the unmodelled
        input the bare `else` used to catch before `call` needed positionals
        at all."""
        self.assertEqual(self.run_fake("gdbus", "introspect", *self.DEST,
                                       "stray").returncode, 2)

    def test_call_refuses_a_name_this_fake_does_not_own(self):
        """The same wrong-name guard as introspect (see
        test_introspect_refuses_a_name_this_fake_does_not_own), on `call`'s
        route through refuse_unowned_name(). Unexercised until now: the other
        `call` tests all pass *self.DEST, so nothing had ever run this path,
        let alone proven its exit code. Both halves of the compound check are
        tried -- wrong dest, then wrong object-path -- since
        `dest == BUS_NAME and object_path == OBJECT_PATH` is one condition
        that a test hitting only one half cannot fully pin."""
        for wrong in (("--session", "--dest", "org.gnome.GPaste2",
                       "--object-path", "/org/gnome/GPaste"),
                      ("--session", "--dest", "org.gnome.GPaste",
                       "--object-path", "/org/gnome/GPaste2")):
            with self.subTest(wrong=wrong):
                self.assertEqual(self.run_fake("gdbus", "call", *wrong, "--method",
                                               "org.gnome.GPaste2.GetElementAtIndex",
                                               "0").returncode, 1)

    def test_the_system_bus_and_login1_are_refused_cleanly_not_a_crash(self):
        """F1: before this fake learned `--system`, LockMonitor's every call
        died in parse() as an unmodelled argument (exit 2) -- the agent's
        callers read that the same as any other non-zero exit (fail-open,
        None), so it never crashed, but the FAILURE SHAPE was wrong: a real
        machine without a resolvable login1 session gives a clean
        name-not-owned response, not an argv-parsing death. `--system` is
        now accepted the same way `--session` always was, so a login1 `call`
        reaches refuse_unowned_name() and is refused through the exact same
        door a wrong GPaste name is (dest != BUS_NAME, unconditionally --
        this fake owns no login1 session table to answer from, regardless of
        object-path or method)."""
        result = self.run_fake(
            "gdbus", "call", *self.LOGIN1,
            "--object-path", "/org/freedesktop/login1/session/_31",
            "--method", "org.freedesktop.DBus.Properties.Get",
            "org.freedesktop.login1.Session", "LockedHint")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn(b"ServiceUnknown", result.stderr)
        self.assertNotIn(b"unmodelled argument", result.stderr)

    def test_the_login1_monitor_watches_silently_instead_of_dying(self):
        """`monitor` (unlike `call`/`introspect`) never exits for a wrong
        name -- refuse_unowned_name's own docstring says so, and main()
        routes it around that function on purpose -- so LockMonitor's
        `gdbus monitor --system --dest org.freedesktop.login1` pump must
        start and keep running, silent, exactly like a GPaste monitor
        pointed at a name nobody owns (test_monitor_emits_on_a_change_
        nobody_asked_it_to_watch's own wrong-dest sibling). Before --system
        was accepted this died in parse() before ever reaching monitor() at
        all -- indistinguishable from here by exit code alone (both leave no
        process), which is exactly why the LockMonitor pump used to
        respawn it forever instead of settling into one long-lived watch."""
        process = subprocess.Popen(
            [str(HERE / "gdbus"), "monitor"] + list(self.LOGIN1),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env())
        self.addCleanup(process.stdout.close)
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        time.sleep(0.3)
        self.assertIsNone(process.poll(),
                          "the login1 monitor exited (rc=%s) instead of watching silently"
                          % process.poll())

    def test_gdbus_call_returns_a_uuid_and_the_top_text(self):
        """The fast tier's whole input. Shape is byte-compatible with real gdbus:
        a tuple literal, uuid first. Checks BOTH fields: a fake that returned
        the uuid with an empty or truncated text would satisfy a uuid-only
        check and hide exactly the payload-logging regression spec 5.2 warns
        about -- which is why the body below is built to a specific value
        rather than merely a non-empty one."""
        state = {"generation": "17", "types": list(fake.TEXT_ALIASES),
                 "body": base64.b64encode(b"hello").decode("ascii")}
        fake.save(self.state, state)
        uuid, text = self.gdbus_call()
        # history_uuid()'s fixed 8-4-4-4-12 shape, not just "some hex and
        # dashes" -- the loose form would also accept a bare "-".
        self.assertRegex(uuid, r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                               r"[0-9a-f]{4}-[0-9a-f]{12}$")
        self.assertEqual(text, "hello")

    def test_gdbus_call_uuid_changes_only_when_the_history_changes(self):
        """The property the whole design rests on: our own re-offer of the SAME
        content must not move the uuid, while a new copy must."""
        fake.save(self.state, {"generation": "1", "types": ["image/png"], "body": ""})
        first = self.gdbus_uuid()
        fake.save(self.state, {"generation": "1", "types": ["image/png", "image/webp"],
                               "body": ""})
        self.assertEqual(self.gdbus_uuid(), first,
                         "a type-list change with no new history entry moved the uuid")
        fake.save(self.state, {"generation": "2", "types": ["image/png"], "body": ""})
        self.assertNotEqual(self.gdbus_uuid(), first, "a new copy did not move the uuid")

    def test_get_element_kind_answers_text_for_a_text_state(self):
        """The gate GPasteTextTier.read_text applies before ever calling
        `--raw get` (agent/clipwire-agent.py): without it, an item that is
        not Text would travel through the text tier as-is instead of being
        left for the wl-clipboard path to handle."""
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"hello").decode())
        result = self.run_fake("gdbus", "call", *self.DEST, "--method",
                               "org.gnome.GPaste2.GetElementKind", self.top_uuid())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ast.literal_eval(result.stdout.decode()), ("Text",))

    def test_get_element_kind_answers_image_for_an_image_state(self):
        self.write_state(types=["image/png"],
                         body=base64.b64encode(b"\x89PNG\r\n\x1a\n").decode())
        result = self.run_fake("gdbus", "call", *self.DEST, "--method",
                               "org.gnome.GPaste2.GetElementKind", self.top_uuid())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(ast.literal_eval(result.stdout.decode()), ("Image",))

    def test_get_element_kind_an_unknown_uuid_is_exit_one(self):
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"hello").decode())
        result = self.run_fake("gdbus", "call", *self.DEST, "--method",
                               "org.gnome.GPaste2.GetElementKind",
                               "00000000-0000-0000-0000-000000000000")
        self.assertEqual(result.returncode, 1)

    def test_select_the_top_uuid_exits_zero(self):
        """v3.4's write path: a confirmed Add already moved the selection
        for real, so Select on the TOP uuid is a no-op there -- accepted,
        not refused."""
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"hello").decode())
        result = self.run_fake("gdbus", "call", *self.DEST, "--method",
                               "org.gnome.GPaste2.Select", self.top_uuid())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.decode().strip(), "()")

    def test_select_an_unknown_uuid_is_exit_one(self):
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"hello").decode())
        result = self.run_fake("gdbus", "call", *self.DEST, "--method",
                               "org.gnome.GPaste2.Select",
                               "00000000-0000-0000-0000-000000000000")
        self.assertEqual(result.returncode, 1)

    def test_the_update_line_is_the_one_the_agent_parses(self):
        line = load("fake_gdbus", HERE / "gdbus").UPDATE_LINE
        self.assertEqual(line, "/org/gnome/GPaste: org.gnome.GPaste2.Update "
                               "('REPLACE', 'ALL', uint64 0)")
        parse = load("clipwire_agent_for_fakes", AGENT).parse_gpaste_line
        self.assertTrue(parse(line))
        self.assertTrue(parse(line + "\n"))     # how the pump actually sees it

    def test_monitor_refuses_a_method_flag(self):
        """--method belongs to `call` alone. A monitor that silently accepted
        it would run forever having ignored an argument it does not model --
        the one shape of bug this file's loudness rule exists to make
        impossible."""
        self.assertEqual(self.run_fake("gdbus", "monitor", *self.DEST, "--method",
                                       "org.gnome.GPaste2.Whatever").returncode, 2)

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

    def test_a_silent_state_change_emits_no_update(self):
        """GPaste's re-offer, which is the whole v3.3 defect: the offered
        types change and no signal is emitted.

        The third write below (reverting to the exact pre-silent bytes) is
        the assertion that actually proves the silent change MOVED the
        monitor's baseline, rather than merely proving it swallowed one
        announcement -- the two are not the same claim. The digest check is
        a boolean inequality against a stored reference, so an ORDINARY
        different change after the silent one (a new generation, new types,
        as below) differs from a stale never-updated baseline exactly as
        surely as it differs from a correctly-advanced one, and would be
        announced exactly once either way; it cannot tell the two
        implementations apart, which is why it is not the only write here.
        A revert to BYTE-IDENTICAL content is the one write that can: it
        differs from an advanced baseline (so it must fire) and is IDENTICAL
        to a frozen one (so a frozen baseline would wrongly see no change at
        all, and stay silent a second time) -- confirmed by running this
        test's original three assertions (no revert step) against a build
        with the baseline-advance deliberately broken: all three still pass,
        which is why the revert step is not optional here.
        """
        baseline = {"generation": "1", "types": ["image/png"], "body": ""}
        fake.save(self.state, dict(baseline))
        monitor = self.start_gdbus_monitor()
        self.assertEqual(self.drain(monitor, seconds=0.3), [],
                         "the monitor spoke before anything changed")
        fake.save(self.state, {"generation": "1", "types": ["image/png", "image/webp"],
                               "body": "", "silent": True})
        self.assertEqual(self.drain(monitor, seconds=0.5), [],
                         "a silent takeover emitted an Update")
        fake.save(self.state, dict(baseline))
        self.assertEqual(len(self.drain(monitor, seconds=0.5)), 1,
                         "reverting to the pre-silent bytes emitted no Update -- "
                         "the silent change never advanced the monitor's baseline")
        fake.save(self.state, {"generation": "2", "types": ["image/png"], "body": ""})
        self.assertEqual(len(self.drain(monitor, seconds=0.5)), 1,
                         "a real copy after a silent one emitted no Update")
        monitor.terminate()

    def test_monitor_logs_the_baseline_digest_before_any_update(self):
        """The line Task 6's harness settle wait blocks on, so it knows the
        monitor has taken its baseline (and so is ready to notice the
        harness's own next write) rather than still starting up. Nothing
        about the baseline is ever printed to stdout -- only logged -- so
        this test waits for the line with NO state change in flight yet: if
        the note only fired on (or after) the first observed change, this
        would time out rather than pass. The Update that follows a real
        change afterward is the proof the log line did not come at the cost
        of the monitor still working."""
        self.write_state(types=["image/png"], body="")
        monitor = subprocess.Popen([str(HERE / "gdbus"), "monitor"] + list(self.DEST),
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   text=True, env=self.env())
        self.addCleanup(monitor.stdout.close)
        self.addCleanup(monitor.wait)
        self.addCleanup(monitor.kill)
        self.assertIn("Monitoring signals", monitor.stdout.readline())
        deadline = time.time() + 5
        while not any("monitor baseline digested" in line for line in self.log_lines()):
            if time.time() > deadline:
                self.fail("the baseline note never appeared in the log")
            time.sleep(0.02)
        self.write_state(body=base64.b64encode(b"a change after the baseline").decode())
        update = monitor.stdout.readline()
        self.assertEqual(update.rstrip("\n"),
                         load("fake_gdbus", HERE / "gdbus").UPDATE_LINE)

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


class GPasteClientTests(ToolTestCase):
    """`gpaste-client add` / `--raw get` -- the v3.4 text tier's transport
    (GPasteTextTier in agent/clipwire-agent.py). Run as subprocesses exactly
    the way it invokes them: `add` through Popen with stdin=PIPE,
    stdout/stderr=DEVNULL, its stdin closed by communicate(); `--raw get`
    through run() with stdin=DEVNULL, capture_output=True. Every call here
    passes an explicit `stdin=`, even `b""`, for the same reason the agent
    always passes stdin=DEVNULL to this tool: it drains stdin to EOF before
    dispatch regardless of subcommand, so a bare run_fake() call without one
    would inherit this test process's own stdin instead of a controlled,
    already-closed pipe."""

    def test_add_stores_stdin_and_bumps_the_uuid(self):
        """The whole point of routing a write through GPaste instead of
        wl-copy: set_body() bumps `generation`, so the top of history moves
        exactly like a real Add (v3.4 spec 1.3)."""
        before = self.top_uuid()
        result = self.run_fake("gpaste-client", "add", stdin=b"a fresh clip")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.top_uuid(), before)
        self.assertEqual(base64.b64decode(self.state_now()["body"]), b"a fresh clip")

    def test_add_over_the_cap_drops_silently(self):
        """Measured on the real daemon: a body over max-text-item-size
        returns exit 0 WITHOUT storing. A harness opts in with
        "gpaste_max_text_bytes"; without honouring the cap this fake would
        store anything, and write_text's byte-equal read-back check -- the
        only thing standing between a silent drop and a false confirmation
        -- would never see a mismatch to catch."""
        self.write_state(gpaste_max_text_bytes=5, types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"orig").decode())
        before = self.top_uuid()
        result = self.run_fake("gpaste-client", "add", stdin=b"way too long for the cap")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.top_uuid(), before)
        self.assertEqual(base64.b64decode(self.state_now()["body"]), b"orig")

    def test_add_does_not_complete_until_stdin_is_closed(self):
        """The real client drains stdin to EOF before it even looks at the
        verb (v3.4 spec 4.1) -- EOF is what STARTS the work, not the verb name.
        Pinned here because this is exactly the shape of hang
        GPASTE_ADD_TIMEOUT_SECONDS exists to survive; a fake that began
        before EOF would never reproduce it, and an agent bug that left the
        pipe open would look fine against this fake and hang for real."""
        process = subprocess.Popen(
            [str(HERE / "gpaste-client"), "add"], stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env())
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        process.stdin.write(b"stuck behind an open pipe")
        process.stdin.flush()
        with self.assertRaises(subprocess.TimeoutExpired):
            process.wait(timeout=1)
        process.stdin.close()
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)

    def test_raw_get_returns_the_exact_bytes_no_added_newline(self):
        """`--raw` is the whole reason the agent uses this tool over the
        display-formatted `get`: no escaping, no newline. A trailing-newline
        body and a no-newline body must come back byte-identical to what was
        stored, which a naive `print()` here would break for both."""
        for body in (b"trailing newline\n", b"no trailing newline"):
            with self.subTest(body=body):
                self.write_state(types=list(fake.TEXT_ALIASES),
                                 body=base64.b64encode(body).decode())
                result = self.run_fake("gpaste-client", "--raw", "get", self.top_uuid(),
                                       stdin=b"")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, body)

    def test_raw_get_an_unknown_uuid_is_exit_one(self):
        """Only the top of history is modelled, like the fake gdbus's
        GetElementAtIndex -- an agent asking for anything else is asking
        for something this fake does not represent."""
        self.write_state(types=list(fake.TEXT_ALIASES),
                         body=base64.b64encode(b"top").decode())
        result = self.run_fake("gpaste-client", "--raw", "get",
                               "00000000-0000-0000-0000-000000000000", stdin=b"")
        self.assertEqual(result.returncode, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
