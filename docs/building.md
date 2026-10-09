# Building from source

[Back to the project overview](../README.md)

Everything except the final install runs as your normal user. Nothing in this
guide needs the laptop: the tests and demo mode work on any Linux machine.

## Requirements

| Tool | Used for |
|---|---|
| Python 3.11+ and [uv](https://docs.astral.sh/uv/) | App, daemon, tests, packages |
| `git` with submodules | Version numbers and the bundled `arc-dgpu-ctl` |
| `build-essential`, `linux-headers-$(uname -r)` | Kernel module |
| `shellcheck`, `desktop-file-utils` (optional) | The same lint checks CI runs |

```bash
git clone --recurse-submodules https://github.com/Dane64/medion-erazer-major-x10-ec.git
cd medion-erazer-major-x10-ec
# existing clone without the submodule:
git submodule update --init
```

## App and daemon

```bash
uv sync --locked                                  # runtime + dev dependencies in .venv
uv run --no-sync x10-control --demo               # GUI with a simulated daemon
uv run --no-sync python -m unittest discover -s tests -v
```

Run one test module with `-p`, for example
`uv run --no-sync python -m unittest discover -s tests -p test_gui.py -v`.
GUI tests use Qt's offscreen platform; set `QT_QPA_PLATFORM=offscreen` on a
machine without a display.

The tests never need root or real hardware. Backends read fake `/sys` trees
(`tests/fakesys.py`) and mocked command runners instead.

## Kernel module

```bash
make -C kernel                                    # builds kernel/x10_ec.ko
make -C kernel EXTRA_CFLAGS=-Werror               # what CI runs
```

To load a hand-built module under Secure Boot, sign it first; see
[Secure Boot and MOK](secure-boot.md#manual-build-and-signing). The installer
normally does all of this through DKMS.

## Packages

The version comes from Git tags through `hatch-vcs`; there is no version number
to edit by hand.

```bash
uv sync --locked --group build
uv build --no-build-isolation --out-dir dist
uv run --no-sync python tools/release.py check dist
```

This produces:

| File | Contents |
|---|---|
| `medion_erazer_major_x10_ec-<version>-py3-none-any.whl` | The app and daemon only (Python package, icon, font) |
| `medion_erazer_major_x10_ec-<version>.tar.gz` | Complete source: app, tests, docs, kernel module, packaging, scripts and `ext/arc-dgpu-ctl`. Extract it and run `sudo bash scripts/install.sh`. |
| `SHA256SUMS` | Checksums written by `tools/release.py check` |

Start with an empty `dist/`; the checker refuses stray files.

## Installing a local build

```bash
sudo scripts/install.sh [--enable-undervolt] [--without-arc-dgpu-ctl] [--user NAME]
```

The installer copies the source to a temporary directory before building, so
your checkout stays owned by you. It installs:

| Path | What |
|---|---|
| `/usr/src/x10-ec-<version>` | DKMS source of the kernel module |
| `/opt/x10-control/venv` | App and daemon |
| `/etc/systemd/system/x10ctld.service` | Daemon unit |
| `/etc/x10ctld.conf` | Daemon configuration (kept on reinstall) |
| `/etc/udev/rules.d/70-x10-ec.rules`, `/etc/modprobe.d/x10-ec.conf`, `/etc/modules-load.d/x10-ec.conf` | Module setup |
| `/usr/share/applications/x10-control.desktop`, `/usr/share/icons/hicolor/scalable/apps/x10-control.svg` | Menu entry and icon |
| `/usr/local/bin/arc-dgpu-ctl` | dGPU power gating, unless already installed |

Run it again after `git pull` to upgrade. `sudo scripts/uninstall.sh` removes
everything except your configuration, saved state and the enrolled MOK.
`arc-dgpu-ctl` is left installed; remove it with its own
`sudo ext/arc-dgpu-ctl/uninstall.sh`.

## Updating the bundled arc-dgpu-ctl

The submodule is pinned to a release tag. Dependabot proposes updates; to do it
by hand:

```bash
git -C ext/arc-dgpu-ctl fetch --tags
git -C ext/arc-dgpu-ctl checkout vX.Y.Z
git add ext/arc-dgpu-ctl && git commit -m "Update arc-dgpu-ctl to vX.Y.Z"
```
