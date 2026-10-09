# Contributing

Changes should improve the supported application without weakening its hardware
guards or introducing unverified commands. Start with the [user guide](docs/user-guide.md)
and [protocol reference](docs/protocol.md).

## Development

Setup, tests and package builds are described in
[Building from source](docs/building.md). In short:

```bash
git submodule update --init
uv sync --locked --group build
uv run --no-sync x10-control --demo
uv run --no-sync python -m unittest discover -s tests -v
```

The test suite uses Python's standard-library `unittest`. GUI tests use Qt's
offscreen platform, a simulated controller, and mocked hardware boundaries.
Transport tests use temporary files and fake byte I/O. No test requires root
or opens real EC/HID devices.

For a focused change, select the relevant test module:

```bash
uv run --no-sync python -m unittest discover -s tests -p test_protocol.py -v
```

Do not use live hardware as an automated test fixture. Demo mode must not read
or write hardware or persisted user settings.

## Source organization

| Path | Responsibility |
|---|---|
| `kernel/x10_ec.c` | Signed module: EC byte allowlist and bounded voltage offsets (GPL-2.0-only) |
| `daemon.py` | x10ctld: socket server, peer authorization, dispatch, persistence, startup restore |
| `client.py` | GUI-side socket client and the in-process demo backend |
| `cli.py`, `__main__.py` | Launch arguments, version output, application startup |
| `gui.py` | Window, sidebar, the six pages, confirmations, asynchronous calls |
| `widgets.py`, `theme.py` | Graph, toggle switch, value slider, Erazer palette and stylesheet |
| `icons.py`, `assets/` | Navigation icons, app logo (`x10-control.svg`) and the bundled Outfit font |
| `controller.py`, `protocol.py` | EC session lifecycle, vendor commands, timing, verified readback |
| `hardware.py` | DMI/security/power guards, EC device selection, byte-I/O transport |
| `lighting.py` | RGB/zone model, on/off and brightness resolution, HID transport |
| `cpu.py`, `gpu.py`, `display.py`, `audio.py` | sysfs/tool backends for each tab |
| `sysfs.py`, `settings.py` | Rooted sysfs access for tests; atomic state persistence |
| `packaging/`, `scripts/` | systemd, udev, modprobe, initramfs hook, installer, MOK signing |
| `tools/release.py` | Release tag, changelog notes, distribution-content, and checksum validation |
| `ext/arc-dgpu-ctl` | Pinned submodule for dGPU power gating; changes go to [its own repository](https://github.com/Dane64/arc-dgpu-ctl) |

Keep Qt out of everything except `gui.py`, `widgets.py`, `icons.py` and `theme.py`. The
daemon is stdlib-only. All backends take a `Sysfs(root)` or an injectable
command runner, so tests build fake `/sys` trees (`tests/fakesys.py`) instead of
touching hardware.

Treat a failed readback as unknown state, not success. Validate a whole request
before the first write. Never widen the kernel allowlist without attributable
vendor or firmware evidence, documented in [protocol](docs/protocol.md).

### UI conventions

- Use the colors and fonts from `theme.py`; don't hard-code new ones in pages.
- Every page subclasses `Page`, sets `title`, `subtitle` and `glyph` (an icon
  name from `icons.GLYPHS`), and groups controls with `section()`.
- New icons are 24x24 single-stroke SVG paths added to `icons.GLYPHS`.
- Hardware changes go through `Page.confirm()` with *No* as the default.

## Dependencies and packaging

Commit `pyproject.toml` and `uv.lock` together after intentional dependency
changes. `uv lock` refreshes the lock file; ordinary development and CI use
`--locked` to reject drift. The `build` group locks the backend and its
transitive dependencies for release builds.
The local build cache also tracks Git commits and tags so switching revisions
or adding a version tag refreshes installed version metadata.

```bash
uv sync --locked --group build
uv build --no-build-isolation
uv run --no-sync python tools/release.py check dist
```

Start with an empty output directory, or use `uv build --out-dir` and pass that
same directory to the package checker. Versioning comes from Git tags. A source
checkout without Git metadata is not a substitute for a published sdist.

The sdist is the complete installer source: application, tests, documentation,
kernel module, packaging, scripts, the bundled `ext/arc-dgpu-ctl`, the lock file
and the release checker. The wheel includes only the application, its assets
and package metadata.
Generated files, local captures, obsolete research utilities, and logs do not
belong in either distribution.

## Pull requests

Explain the user-visible outcome and why the change is needed. Include the
relevant regression coverage and update documentation when behavior changes.
Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything
users will notice; it becomes the release notes.
Do not include vendor binaries, firmware dumps, serial numbers, local build
artifacts, or unrelated formatting churn.

CI runs the tests on Python 3.11 through 3.14, builds the kernel module with
`-Werror`, lints shell scripts, validates the desktop entry, systemd unit and
udev rules, runs the submodule's checks, builds a wheel from the sdist, checks
package contents, and exercises the installed wheel outside the checkout. The
**Quality gate** job is the required aggregate result. Maintainer publishing
instructions are in [Releases](docs/releases.md).
