# agent/tests/test_clipboard.py
import os
import unittest

from agent_under_test import clipboard_env, runtime_dir, wayland_socket_path


class TestEnvironment(unittest.TestCase):
    def test_uses_xdg_runtime_dir_when_present(self):
        env = dict(os.environ, XDG_RUNTIME_DIR="/run/user/4242")
        self.assertEqual(runtime_dir(env), "/run/user/4242")

    def test_falls_back_to_uid_when_absent(self):
        env = {k: v for k, v in os.environ.items() if k != "XDG_RUNTIME_DIR"}
        self.assertEqual(runtime_dir(env), "/run/user/%d" % os.getuid())

    def test_builds_wayland_display_and_bus_address(self):
        env = clipboard_env({"XDG_RUNTIME_DIR": "/run/user/4242"})
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(
            env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/4242/bus"
        )
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/4242")

    def test_socket_path(self):
        self.assertEqual(
            wayland_socket_path({"XDG_RUNTIME_DIR": "/run/user/4242"}),
            "/run/user/4242/wayland-0",
        )


if __name__ == "__main__":
    unittest.main()
