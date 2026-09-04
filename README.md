# Medion Erazer Major X10 fan control for Linux

This repository contains a Linux GUI and the reverse-engineered firmware
protocol used by Medion Control Center 2.7.4 on the Medion Erazer Major X10.
The application can:

- read CPU- and GPU-fan tachometer values;
- read the CPU and GPU temperatures reported by the EC;
- select the firmware's Office, Gaming, or Turbo performance profile;
- enable or disable the firmware's global full-speed fan override; and
- plot fan speed and temperature over a selectable time window; and
- set static colors for the keyboard, lid logo, and left/right side lights.

There is still no verified command for setting an individual fan to an
arbitrary RPM, duty cycle, or temperature curve. The graph is telemetry, not
an editable hardware curve. Do not write to the tachometer selectors in an
attempt to turn them into target-speed registers.

## Safety warning

The protocol uses direct byte I/O through `/dev/port`. This bypasses normal
kernel device ownership and can damage hardware state if the wrong port or
value is used. The implementation refuses to run unless every recorded DMI,
BIOS, and EC identity field matches the tested machine exactly. There is no
unsupported-hardware override.

Only the operations attributable to the official vendor binary are
implemented. Do not reuse values from unrelated NBFC profiles, probe unknown
selectors, or replay these writes on another laptop model or firmware version.

The terms *selector*, *index*, and *action* are important in this document.
Values such as `0x16` and `0x20` are second bytes in a vendor command protocol;
they are not I/O ports and have not been shown to be ordinary EC RAM offsets.

## Supported machine

The hardware backend currently accepts only this identity:

| Item | Required value |
| --- | --- |
| System vendor | `MEDION` |
| Product | `Major X10` |
| Product SKU | `ML-210015 30034642` |
| Mainboard | `N68630`, revision `1.0` |
| BIOS | `M1IB008`, dated 2022-09-01 |
| EC firmware | `0.8` |
| CPU | Intel Core i7-12700H |
| Integrated GPU | Intel Iris Xe, `8086:46a6` |
| Discrete GPU | Intel Arc A730M, `8086:5691` |
| GPU subsystem | MEDION `1e39:a991` |

The live observations in this document were refreshed on 2026-07-27 with
kernel `7.1.3+deb14-amd64`.

## Running the application

Python 3.11 or newer and [`uv`](https://docs.astral.sh/uv/) are required.
The desktop interface uses PySide6 and automatically reflows its controls and
telemetry cards to fit the available window. Hovering over a tab activates it;
click and keyboard navigation remain available.

Create the virtual environment and install the locked dependencies:

```bash
uv sync
```

Run the GUI with simulated telemetry first:

```bash
uv run medion-fan-control --demo
```

Run the test suite:

```bash
uv run python -m unittest discover -s tests -v
```

The current prototype opens `/dev/port` itself, so the hardware GUI requires
administrator access. Kernel lockdown also blocks `/dev/port`, including for
root. Before opening the EC transport, the application checks the UEFI Secure
Boot variable and the kernel lockdown mode. If either blocks EC access, it
shows setup instructions and does not attempt any port reads or writes.

Secure Boot must be disabled in the laptop's UEFI/BIOS setup. Restart, use the
setup key shown during startup (commonly F2), find **Secure Boot** under the
Security or Boot settings, disable it, then save and reboot. Ensure any disk
encryption recovery key is available before changing firmware settings.
After rebooting, verify that `none` is selected:

```bash
cat /sys/kernel/security/lockdown
# Expected: [none] integrity confidentiality
```

Then run:

```bash
sudo -E "$(pwd)/.venv/bin/medion-fan-control"
```

Build the source distribution and wheel locally with Hatchling:

```bash
uv build
```

Pushing a version tag such as `v0.1.0` runs the GitHub release workflow. It
installs the locked `uv` environment, runs the tests, builds both package
formats with Hatchling, and attaches them to a GitHub release for that tag.

The hardware session:

1. validates the complete machine identity above;
2. takes an exclusive lock at `/run/lock/medion-fan-control.lock`;
3. opens `/dev/port` for byte reads and writes;
4. reads telemetry, firmware state, and the Linux `AC0` online state; and
5. performs a write only after the user confirms a profile or full-speed
   change.

Before every Turbo write, the backend checks
`/sys/class/power_supply/AC0/online`. When `AC0` is offline, the GUI leaves the
current mode unchanged and explains that Turbo requires the AC barrel adapter
rather than USB-C PD.

Lighting uses a separate USB HID interface. Choosing colors only stages them
in the GUI. Each zone has a labeled color control, and **All zones** can set a
single color across the laptop. **Apply colors** writes the selected zones in
three complete passes after confirmation, then stores a successful selection
under `${XDG_CONFIG_HOME:-~/.config}/medion-fan-control/lighting.json`. The
hardware GUI automatically reapplies those saved colors when it next starts,
including after a reboot.

Closing the GUI closes `/dev/port` but leaves the firmware in the last selected
profile and full-speed state. Disabling full speed with the GUI returns fan
behavior to the selected firmware profile.

## Source of the protocol

Medion download ID `22751` provides `17.ControlCenter_MajorX10.zip`. The
investigated archive was 19,760,727 bytes and contained this installer:

| Item | Value |
| --- | --- |
| File | `Medion CC_2_7_4.exe` |
| Product | Medion Control Center 2.7.4 |
| Format | 32-bit Windows PE, Inno Setup 6.1.0 |
| Size | 20,229,264 bytes |
| SHA-256 | `963d90a6aea5f5ae3787bd3fcbdc775dcf40eb13bddf59f61b0b21ab2c16b224` |

The installer was extracted without executing it. Its relevant payload is:

- `Medion Control Center.exe`: managed WPF application containing
  `ECManager`;
- `mcucontrol.dll`: native USB HID lighting transport;
- `rwport.dll`: native byte-I/O wrapper;
- `WTIOportDrv.sys`: Windows kernel port-I/O driver;
- `install.bat`: installs the driver as the automatic `ioportdrv` service;
- `MedionOSD.exe`: OSD process that also imports `rwport.dll`; and
- `SysmanToolCmd.exe`: Intel graphics Sysman utility discussed below.

The managed declarations are effectively `byte Read(byte port)` and
`void Write(byte port, byte value)`. `rwport.dll` opens
`\\.\ReadAndWritePort` and sends these driver requests:

| Operation | Windows IOCTL | Driver operation |
| --- | --- | --- |
| Read | `0x9c402004` | `in al, dx` |
| Write | `0x9c402008` | `out dx, al` |

The driver's kernel object is `\Device\ReadAndWritePort`. This proves that the
Control Center performs direct one-byte port I/O. The fan/profile path does
not use WMI, an ACPI control method, or a conventional Windows fan API.

## Vendor EC transport

The official application uses this pair of ports:

| Role | I/O port |
| --- | --- |
| Vendor command port | `0x6c` |
| Vendor data port | `0x68` |

These are not the ACPI `EC0` ports. The loaded ACPI tables describe the normal
embedded controller at command port `0x66` and data port `0x62`, while the
Control Center independently uses `0x6c` and `0x68` for the protocol below.

For a read, the vendor application performs:

```text
write_byte(0x6c, command)
sleep(10 ms)
write_byte(0x68, selector)
sleep(10 ms)
value = read_byte(0x68)
```

For an action, it performs:

```text
write_byte(0x6c, command)
sleep(10 ms)
write_byte(0x68, action)
```

The vendor serializes complete operations with the Windows mutex
`Global\IO_Mutex`. The Linux backend holds an exclusive process lock for the
hardware session. Transactions must not be interleaved with another program.

## Vendor lighting transport

The official application controls the chassis lighting through USB HID device
`1a2c:1512`, interface `03`, not through the EC ports. That interface has a
64-byte vendor output report. The Linux backend additionally verifies its full
HID report descriptor before opening the corresponding `/dev/hidraw*` node.

For a static color, the vendor `SetLightRGB` function sends a 64-byte payload.
Linux hidraw requires a leading zero report-number byte, making the userspace
write 65 bytes:

| Write byte | Static-color value | Meaning |
| --- | --- | --- |
| `0` | `0x00` | Unnumbered HID report prefix |
| `1..2` | `0x03 0xc0` | Lighting command |
| `3` | `0..3` | Keyboard, left side, right side, lid logo |
| `4` | `0x00` | Reserved |
| `5` | `0x80` | Static/AlwaysOn effect `0`, with enable bit |
| `6..7` | `0x80 0x80` | Vendor-default brightness, repeated |
| `8` | `0x22` | Vendor-default orientation/speed fields |
| `9..11` | `R G B` | Selected color |
| `12..64` | zero | Unused for a single static color |

The package also contains animated effects and four independent keyboard
areas. They are not exposed yet; the current GUI deliberately implements only
the smallest fully traced operation requested here: one static color for each
of the keyboard, lid, left-side, and right-side zones.

## Complete managed EC method map

An exhaustive scan of every managed call to `DllClass.Read` and
`DllClass.Write` found raw port I/O only in the following `ECManager` methods.
`ChangeCurrentModeToEC` is a higher-level wrapper around two of them.

| Vendor method | Protocol operation | Meaning |
| --- | --- | --- |
| `NotifyECChangeThermal(mode)` | action `0xde`, then `0x01`/`0x02`/`0x03` | Set Office/Gaming/Turbo |
| `ReadECThermal()` | read `0xde:0x11` | Current profile, `1` through `3` |
| `NotifyECChangeWirelessCharge(on)` | action `0xde`, then `0x40`/`0x41`, twice | Enable/disable wireless charging |
| `NotifyECChangeFanFullSpeed(on)` | action `0xde`, then `0x0e`/`0x0f`, twice | Enable/disable global full speed |
| `NotifyECChangeAOU(on)` | action `0xde`, then `0x0a`/`0x0b`, twice | Enable/disable always-on USB charging |
| `ReadECStatusFanFullSpeed()` | read `0xde:0x10` | `1` enabled, `2` disabled |
| `ReadECGpuFanSpeed()` | read `0xd5:0x16` and `0xd5:0x17` | GPU-fan RPM, little-endian |
| `ReadECCpuFanSpeed()` | read `0xd5:0x18` and `0xd5:0x19` | CPU-fan RPM, little-endian |
| `ReadECGPUTemperature()` | read `0xdd:0x23` | GPU temperature in C |
| `ReadECCPUTemperature()` | read `0xdd:0x20` | CPU temperature in C |
| `ReadECPower()` | read `0xde:0x05` | Firmware power-state code `0` through `2` |
| `ReadECAOUStatus()` | read `0xde:0x0c` | `0x11` enabled, `0x10` disabled |
| `ChangeCurrentModeToEC()` | set profile, wait 30 ms, read profile, repeat | Apply and verify selected mode |

`GetSystemPowerStatus`, which also appears in the managed assembly, is a
Windows API call and is not an EC operation.

### Fan tachometers: command `0xd5`

| Selector written to `0x68` | Access | Meaning |
| --- | --- | --- |
| `0x16` | Read | GPU-fan RPM low byte |
| `0x17` | Read | GPU-fan RPM high byte |
| `0x18` | Read | CPU-fan RPM low byte |
| `0x19` | Read | CPU-fan RPM high byte |

Each byte requires a complete `0xd5` read transaction. The values are combined
as follows:

```text
gpu_rpm = read(0xd5, 0x16) | (read(0xd5, 0x17) << 8)
cpu_rpm = read(0xd5, 0x18) | (read(0xd5, 0x19) << 8)
```

These selectors are tachometers only. The vendor binary never writes a target
RPM, PWM percentage, or duty cycle through command `0xd5`.

### Temperatures: command `0xdd`

| Selector written to `0x68` | Access | Meaning |
| --- | --- | --- |
| `0x20` | Read | CPU temperature, one byte in C |
| `0x23` | Read | GPU temperature, one byte in C |

The vendor retries until a result is between 10 and 200. That range is an
input-validation rule from the application, not a safe operating range.

### Control and status: command `0xde`

| Second byte written to `0x68` | Access | Meaning or result |
| --- | --- | --- |
| `0x01` | Write action | Select Office profile |
| `0x02` | Write action | Select Gaming profile |
| `0x03` | Write action | Select Turbo profile |
| `0x05` | Read selector | Power-state code `0`, `1`, or `2` |
| `0x0a` | Write action, twice | Enable always-on USB charging |
| `0x0b` | Write action, twice | Disable always-on USB charging |
| `0x0c` | Read selector | AOU status: `0x11` enabled, `0x10` disabled |
| `0x0e` | Write action, twice | Enable global full-speed fan override |
| `0x0f` | Write action, twice | Disable full speed and resume profile control |
| `0x10` | Read selector | Full-speed status: `1` enabled, `2` disabled |
| `0x11` | Read selector | Profile: `1` Office, `2` Gaming, `3` Turbo |
| `0x40` | Write action, twice | Enable wireless charging |
| `0x41` | Write action, twice | Disable wireless charging |

The exact physical meaning of the three `ReadECPower()` codes has not been
established. In particular, the EC returned `2` repeatedly while the AC barrel
adapter was connected and Linux reported `AC0` online. The Linux application
therefore keeps this value as raw firmware data and uses the kernel's `AC0`
online state for the Turbo availability check.

The vendor has no wireless-charging status read. It stores the requested
wireless state in `CConly.config.xml` and sends the corresponding action when
the user clicks the switch. AOU and full-speed state, in contrast, have EC
readback selectors.

## Vendor retry behavior

The Control Center uses 10 ms sleeps between port operations. It also:

- repeats wireless-charge, AOU, and full-speed actions exactly twice;
- waits 30 ms after a profile action and repeatedly reads `0xde:0x11` until
  the requested mode appears;
- retries profile reads until the result is `1` through `3`;
- retries full-speed reads until the result is `1` or `2`;
- retries AOU reads until the result is `0x10` or `0x11`;
- retries power reads until the result is `0` through `2`; and
- retries temperature reads until the result is `10` through `200`.

Several vendor loops are unbounded. The Linux implementation uses bounded
retries and raises `ProtocolError` instead of hanging forever.

## Why the fan-speed property setters do not control fans

The managed assembly also contains:

```text
get_CpuFanSpeed
set_CpuFanSpeed
get_GpuFanSpeed
set_GpuFanSpeed
```

These are WPF model-property accessors, not EC methods. Each setter only stores
a formatted string and raises `PropertyChanged`. The vendor polling loop does
the equivalent of:

```text
GpuFanSpeed = ReadECGpuFanSpeed().ToString() + "RPM"
CpuFanSpeed = ReadECCpuFanSpeed().ToString() + "RPM"
sleep(5000 ms)
```

Calling one of these setters changes displayed text only. It never reaches
`rwport.dll` or `WTIOportDrv.sys`.

## What can and cannot be controlled

Verified controls:

- Office, Gaming, and Turbo firmware profiles;
- a global full-speed override; and
- unrelated AOU and wireless-charging switches documented for completeness.

Not verified and therefore not implemented:

- CPU-fan target RPM;
- GPU-fan target RPM;
- PWM or duty percentage;
- minimum or maximum fan limits;
- per-fan enable/disable;
- firmware temperature-curve points; and
- a command that restores a separately named `automatic` mode.

Disabling full speed with `0x0f` resumes whatever automatic policy belongs to
the selected Office, Gaming, or Turbo profile. A custom controller can safely
be built only as a coarse policy over those verified states, for example by
selecting profiles at temperature thresholds and reserving full speed for a
critical threshold. Such a policy needs hysteresis, minimum dwell times,
bounded failures, and a critical-temperature fallback. It still would not set
a precise RPM.

## Fan stop and lower noise

There is no separate fan-off action in the exhaustive managed EC method map.
The Windows application selects a firmware profile and displays the resulting
tachometer value; a fan reaching zero at low load is therefore firmware policy,
not a direct stop command. Office mode is the only verified low-noise EC
control available to this application.

Reducing idle heat may let that policy reach its zero-RPM state. On the tested
Linux installation, the internal panel is connected to the Iris Xe and every
Arc A730M connector is disconnected, but the Arc device at `0000:03:00.0` has
`power/control=on`, remains `active`, and has accumulated no runtime-suspended
time. A reversible first experiment is:

```bash
echo auto | sudo tee /sys/bus/pci/devices/0000:03:00.0/power/control
watch -n 1 cat /sys/bus/pci/devices/0000:03:00.0/power/runtime_status
```

`suspended` indicates that runtime power management succeeded. Restore the
current behavior with `echo on` to the same file. This setting resets on boot.
Do not force runtime suspend while an external display or workload uses the
Arc GPU.

CPU RAPL support is loaded, but this installation exposes no power-limit files
under `/sys/class/powercap`; the Arc hwmon node likewise exposes no writable
power cap. The application therefore does not invent a raw MSR or GPU power
limit path. The standard `intel_pstate` energy preference and turbo switch are
safer CPU experiments:

```bash
echo power | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference
echo 1 | sudo tee /sys/devices/system/cpu/intel_pstate/no_turbo
```

Try the energy preference first; disabling turbo has a larger performance
cost. Restore the observed defaults with `balance_performance` and `0`,
respectively. These settings also reset on boot.

## Intel Arc Sysman lead

The vendor package includes `SysmanToolCmd.exe`, whose command strings include
`set_fan_speed <percentage>`. This is an Intel graphics Sysman utility, not an
EC method, and it is not evidence that the `0xd5` tachometer selectors are
writable. No managed Control Center call to that percentage command has been
established.

On the tested Linux installation:

- the Arc A730M uses the `i915` driver;
- its hwmon node exposes a read-only `fan1_input` value;
- no `pwm*`, `fan*_target`, or other writable fan attribute is exposed;
- one idle sample reported `0 RPM` at a GPU temperature of 51 C; and
- a read-only Level Zero Sysman probe enumerated both Intel GPUs but reported
  zero fan handles for both.

The packaged Windows utility therefore does not currently provide a portable
Linux control path. `scripts/probe-level-zero-sysman.c` can be rerun after
future kernel or Compute Runtime upgrades to detect whether a supported fan
handle becomes available.

## ACPI and Linux findings

The loaded firmware tables establish the following facts:

- the active ACPI embedded controller is `EC0`, UID 1, GPE `0x6e`;
- `EC0` uses the standard physical ports `0x66` and `0x62`;
- its `EmbeddedControl` operation region `EC4A` covers offsets `0x00` through
  `0xfe`;
- visible named `EC4A` fields are predominantly battery-related;
- no loaded AML table names an obvious RPM, PWM, tachometer, duty, or fan
  target field;
- `FANM` is only bit 5 of a one-byte MMIO field at `0xfe0b030a`, so it cannot
  represent RPM or duty;
- `CFAN` is only an external declaration in the DSDT; and
- `SSDT1` is a Medion DPTF policy table.

Linux exposes five binary ACPI cooling devices:

| ACPI path | Observed cooling device | State range | ACPI trip |
| --- | --- | --- | --- |
| `\_TZ.FAN0` | `cooling_device20` | `0-1` | 100 C |
| `\_TZ.FAN1` | `cooling_device21` | `0-1` | 55 C |
| `\_TZ.FAN2` | `cooling_device22` | `0-1` | 50 C |
| `\_TZ.FAN3` | `cooling_device23` | `0-1` | 45 C |
| `\_TZ.FAN4` | `cooling_device24` | `0-1` | 40 C |

All five reported `cur_state=0`, `max_state=1`, and ACPI power state `D3hot`
while physical fans were audibly running. The machine has separate CPU and GPU
fans, so these five objects are not five one-to-one physical fan controls.
Their sysfs state is not reliable physical-fan telemetry and this project does
not write their `cur_state` files.

The old NBFC Medion Akoya P6612 and P6630 profiles use EC offsets `0x93`,
`0x94`, and `0x95`. There is no evidence that those offsets have the same
meaning on the Major X10. They must not be selected or copied.

## WMI inventory

These WMI devices are present, but none is used by the verified fan/profile
path:

| GUID/instance | ACPI object or notification |
| --- | --- |
| `05901221-D566-11D1-B2F0-00A0C9062910-2` | object `CC`, BMOF data |
| `05901221-D566-11D1-B2F0-00A0C9062910-4` | object `CC`, BMOF data |
| `05901221-D566-11D1-B2F0-00A0C9062910-7` | object `BA`, BMOF data |
| `1F13AB7F-6220-4210-8F8E-8BB5E71EE969-3` | object `TE` |
| `2BC49DEF-7B15-4F05-8BB7-EE37B9547C0B-0` | object `DE` |
| `A6FEA33E-DABF-46F5-BFC8-460D961BEC9F-1` | notification `D0` |
| `ABBC0FB8-8EA1-11D1-A000-C90629100000-5` | object `AA` |
| `ABBC0FC8-8EA1-11D1-A000-C90629100000-6` | notification `A0` |

Enumeration alone is not evidence that a GUID controls a fan. Do not invoke
these objects without first decoding their BMOF and AML contracts.

## Repository layout

| Path | Purpose |
| --- | --- |
| `medion_fan_control/protocol.py` | Verified command protocol and bounded readback |
| `medion_fan_control/hardware.py` | DMI allowlist, exclusive lock, and `/dev/port` backend |
| `medion_fan_control/lighting.py` | Verified USB HID discovery and static zone colors |
| `medion_fan_control/gui.py` | Tkinter telemetry, firmware-mode, and lighting GUI |
| `tests/` | Protocol, hardware guard, lighting, and GUI-model tests |
| `scripts/collect-firmware.sh` | Read-only ACPI, WMI, thermal, DMI, and log collector |
| `scripts/dump-managed-il.py` | Read-only selected-method .NET IL dumper |
| `scripts/probe-level-zero-sysman.c` | Read-only Intel Sysman fan-capability probe |

`scripts/collect-firmware.sh` requests `sudo` because Linux restricts access to
ACPI tables and kernel logs. It performs no hardware writes. Its output is mode
0700 by default; review DMI and logs before publishing them.

## Current conclusion

The official Control Center establishes a small, coherent EC protocol for two
tachometers, two temperatures, three firmware profiles, and one global
full-speed override, plus a separate USB HID protocol for chassis lighting.
That is enough for useful Linux telemetry, safe selection among vendor-defined
fan policies, and static control of the four verified light zones.

It does not establish an EC command for arbitrary per-fan speed or curve
control. Until such a command is attributable to firmware or vendor code, the
correct control surface is Office/Gaming/Turbo plus full speed, not writes to
read-only tachometer selectors or guessed EC offsets.