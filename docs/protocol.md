# Protocol reference

[Back to the project overview](../README.md)

This reference documents the operations implemented by the Linux application.
It is not an invitation to probe unknown commands or use this transport on
other hardware. The [hardware allowlist](../README.md#supported-hardware)
applies to every supported hardware session.

## Provenance

The protocol is derived from Medion Control Center 2.7.4, distributed in Medion
download ID `22751` as `17.ControlCenter_MajorX10.zip`. The installer was
extracted without executing it.

| Source artifact | Value |
| --- | --- |
| Installer | `Medion CC_2_7_4.exe` |
| Format | 32-bit Windows PE, Inno Setup 6.1.0 |
| Installer size | 20,229,264 bytes |
| SHA-256 | `963d90a6aea5f5ae3787bd3fcbdc775dcf40eb13bddf59f61b0b21ab2c16b224` |

The managed application's `ECManager` calls `rwport.dll`, which sends byte-I/O
requests to `WTIOportDrv.sys`. The read IOCTL is `0x9c402004` (`in al, dx`);
the write IOCTL is `0x9c402008` (`out dx, al`). This is direct I/O, not WMI or a
conventional fan-control API.

Chassis lighting is implemented separately by the native `mcucontrol.dll`
USB HID transport. Vendor installers, binaries, firmware captures, and
disassembly output are not distributed with this project.

## EC transport

| Role | Port |
| --- | --- |
| Vendor command | `0x6c` |
| Vendor data | `0x68` |

These are distinct from the normal ACPI embedded controller's `0x66` and
`0x62` ports.

A read transaction is:

```text
write_byte(0x6c, command)
sleep(10 ms)
write_byte(0x68, selector)
sleep(10 ms)
value = read_byte(0x68)
```

An action is:

```text
write_byte(0x6c, command)
sleep(10 ms)
write_byte(0x68, action)
```

A selector is the second byte of this command protocol. It is not an I/O port
or a demonstrated EC RAM offset. Complete transactions must not interleave.
The vendor uses `Global\IO_Mutex`; the Linux app uses a single EC worker and
an exclusive process lock at `/run/lock/medion-fan-control.lock`.

## Supported commands

| Command | Selector or action | Operation |
| --- | --- | --- |
| `0xd5` | `0x16`, `0x17` | Read GPU fan RPM, low byte then high byte |
| `0xd5` | `0x18`, `0x19` | Read CPU fan RPM, low byte then high byte |
| `0xdd` | `0x20` | Read CPU temperature in Celsius |
| `0xdd` | `0x23` | Read GPU temperature in Celsius |
| `0xde` | `0x01`, `0x02`, `0x03` | Select Office, Gaming, or Turbo |
| `0xde` | `0x11` | Read profile: `1` Office, `2` Gaming, `3` Turbo |
| `0xde` | `0x0e`, repeated twice | Enable global full speed |
| `0xde` | `0x0f`, repeated twice | Disable full speed and resume the selected profile |
| `0xde` | `0x10` | Read full-speed state: `1` enabled, `2` disabled |
| `0xde` | `0x05` | Read raw firmware power-state code, `0` through `2` |

Each tachometer byte requires a full read transaction:

```text
gpu_rpm = read(0xd5, 0x16) | (read(0xd5, 0x17) << 8)
cpu_rpm = read(0xd5, 0x18) | (read(0xd5, 0x19) << 8)
```

The tachometer selectors are read-only measurements. No target RPM or duty
write through `0xd5` has been established.

### Timing, readback, and bounds

Profile actions wait 30 ms before reading back `0xde:0x11`. The backend retries
up to five times until the requested profile is confirmed. Full-speed actions
are sent twice with 10 ms delays, then verified through `0xde:0x10`.

Profile, full-speed, power-state, and temperature reads also use bounded retries,
with a default limit of five. Temperatures must be between 10 and 200 inclusive,
matching the vendor's input-validation rule. **This is not a safe operating
temperature range.** Invalid values or exhausted readbacks raise `ProtocolError`;
there are no unbounded firmware retry loops.

The physical meanings of the raw `0xde:0x05` power codes are not established.
Turbo eligibility therefore comes from Linux `AC0`, whose type must be `Mains`
and whose `online` attribute must be exactly `0` or `1`. Every Turbo write
attempt checks this source again. Without a power-status provider, the protocol
does not authorize Turbo.

## Static lighting transport

Lighting requires USB HID identity `0003:00001A2C:00001512`, interface `03`,
and this exact report descriptor:

```text
06 00 ff 09 02 a1 01 19 01 29 40 15 00 26 ff 00
75 08 95 40 81 00 19 01 29 40 91 00 75 08 96 08
02 15 00 26 ff 00 09 02 b1 02 c0
```

The vendor's `SetLightRGB` operation sends a 64-byte output report. Linux
hidraw requires a leading zero report-number byte, so each write is 65 bytes.

| Write byte | Value | Meaning |
| --- | --- | --- |
| `0` | `0x00` | Unnumbered report prefix |
| `1..2` | `0x03 0xc0` | Lighting command |
| `3` | `0..3` | Keyboard, left side, right side, lid logo |
| `4` | `0x00` | Reserved |
| `5` | `0x80` | Static effect with enable bit |
| `6..7` | `0x80 0x80` | Repeated vendor-default brightness |
| `8` | `0x22` | Vendor-default orientation and speed |
| `9..11` | `R G B` | Selected color |
| `12..64` | zero | Unused for a single static color |

The backend validates the whole requested color set before writing, sends
three complete passes with 50 ms between passes, and treats a short write as
an error. It holds `/run/lock/medion-fan-control-lighting.lock` throughout the
operation. It does not claim physical color readback.

## Scope and limitations

The application exposes only firmware profiles, the global fan override, and
static colors for the four verified zones. Animated lighting and independent
keyboard subzones are not implemented.

There is no verified arbitrary fan RPM, PWM, fan-off, or temperature-curve
command. Vendor WPF fan-speed property setters only update displayed strings;
they do not issue hardware commands. Unrelated NBFC offsets and the standard
ACPI cooling-device states are not interchangeable with this protocol.

Protocol changes require attributable vendor or firmware evidence, preserving
the supported identity checks, timing, serialization, and bounded failure
behavior. See [Contributing](../CONTRIBUTING.md).
