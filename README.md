# Medion Major X10 Control

[![CI](https://github.com/Dane64/medion-erazer-major-x10-ec/actions/workflows/ci.yml/badge.svg)](https://github.com/Dane64/medion-erazer-major-x10-ec/actions/workflows/ci.yml)

An unofficial Linux desktop application for the **Medion Erazer Major X10**.
Monitor CPU and GPU temperatures and fan speeds, select firmware performance
profiles, and configure static chassis lighting from one PySide6 interface.

**Hardware support is experimental and restricted to the exact machine and
firmware listed below.** This project is not affiliated with or endorsed by
MEDION.

## Features

- **Dashboard:** CPU/GPU fan RPM and temperatures, with 30-second to 10-minute
  telemetry history.
- **Performance:** Office, Gaming, and Turbo firmware profiles, plus a global
  full-speed fan override.
- **Lighting:** independent static colors for the keyboard, lid logo, and
  left/right side lights, with saved selections restored on startup.
- **Demo mode:** explore the interface and lighting controls without hardware
  access, administrator privileges, or changes to saved settings.

There is no verified interface for arbitrary fan RPM, PWM, fan-off commands,
or custom temperature curves. The graph displays telemetry; it does not edit
the firmware's fan policy.

## Quick start

Use a Linux desktop with Python **3.11 or newer** and
[`uv`](https://docs.astral.sh/uv/). CI covers Python 3.11 through 3.14.
The locked Qt wheels require a compatible Linux distribution; on x86-64,
glibc 2.34 or newer is required.

```bash
git clone https://github.com/Dane64/medion-erazer-major-x10-ec.git
cd medion-erazer-major-x10-ec
uv sync --locked
uv run --no-sync medion-fan-control --demo
```

The desktop uses normal click and keyboard tab navigation. Controls and
telemetry cards reflow as the window is resized.

```bash
uv run --no-sync medion-fan-control --help
uv run --no-sync medion-fan-control --version
```

### Hardware mode

**Read the [user guide and safety notes](docs/user-guide.md) before proceeding.**
EC access uses direct I/O through `/dev/port`, including for telemetry requests.
It requires administrator privileges and is blocked by Secure Boot or kernel
lockdown. The application does not bypass these protections.

After reviewing those requirements, run the installed launcher rather than
running the package manager as root:

```bash
sudo -H --preserve-env=DISPLAY,XAUTHORITY,WAYLAND_DISPLAY,XDG_RUNTIME_DIR \
  "$(pwd)/.venv/bin/medion-fan-control"
```

Desktop authorization varies between X11 and Wayland; see
[troubleshooting](docs/user-guide.md#troubleshooting) if the privileged window
cannot connect to your desktop.

## Supported hardware

Every field below must match exactly before the hardware backend can open.
There is no force or unsupported-hardware option.

| DMI field | Required value |
| --- | --- |
| System vendor | `MEDION` |
| Product name | `Major X10` |
| Product SKU | `ML-210015 30034642` |
| Mainboard name | `N68630` |
| Mainboard revision | `1.0` |
| BIOS version | `M1IB008` |
| EC firmware release | `0.8` |

Lighting additionally requires USB HID device `1a2c:1512`, interface `03`,
with the exact report descriptor accepted by the backend.

Turbo requires the AC barrel adapter. USB-C PD is not sufficient. Power is
checked again before each Turbo write, not just when enabling the button.

**Closing the application leaves the last firmware profile and full-speed
setting in place.** Turn off **Full speed** to resume the selected profile's
automatic fan behavior.

## Documentation

| Guide | Contents |
| --- | --- |
| [User guide](docs/user-guide.md) | Hardware access, controls, saved lighting, and troubleshooting |
| [Protocol reference](docs/protocol.md) | Supported EC/HID operations, timing, validation, and provenance |
| [Contributing](CONTRIBUTING.md) | Source organization, development, and hardware-isolated tests |
| [Releases](docs/releases.md) | Builds, version tags, release safeguards, and maintainer setup |

## Development and releases

```bash
uv sync --locked --group build
uv run --no-sync python -m unittest discover -s tests -v
uv build --no-build-isolation
uv run --no-sync python tools/release.py check dist
```

Use an empty build output directory; the package check rejects mixed versions
and stale distributions. Pull requests and branch pushes run the same quality
pipeline used by version-tag releases. Release artifacts include a wheel,
source distribution, and SHA-256 checksums.

Download published packages from
[GitHub Releases](https://github.com/Dane64/medion-erazer-major-x10-ec/releases).
The project is licensed under [Apache-2.0](LICENSE).
