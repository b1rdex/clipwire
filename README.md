# clipwire

One clipboard across a Mac and a Linux box on the same network, over a single SSH channel.

No server, no accounts, no sessions, no listening ports, no certificates. The Mac dials
out; `sshd` on the other end spawns a single-file Python agent. Authentication and
encryption come from SSH, so there is no state that a reboot can invalidate.

Built for a specific pair of machines — macOS Sequoia and Ubuntu 25.10 on GNOME/Wayland,
where `wl-paste --watch` does not work because Mutter has no wlroots data-control protocol.

**Status:** design stage. See
[the design doc](docs/superpowers/specs/2026-07-30-clipwire-design.md) for architecture,
protocol, and the constraints that shaped both.
