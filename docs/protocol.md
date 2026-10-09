# Protocol reference

[Back to the project overview](../README.md)

This reference documents the operations implemented by the Linux application.
It is not an invitation to probe unknown commands or use this transport on
other hardware. Every hardware session is gated by the identity checks in
`medion_fan_control/hardware.py` and `kernel/x10_ec.c`.

## Provenance

The protocol was derived from MEDION's own Control Center for this laptop. Its
fan manager talks to the embedded controller with direct byte I/O on two
vendor ports (not WMI or a standard fan-control API), and its lighting module
sends USB HID reports. Vendor installers, binaries, firmware captures and
disassembly output are not part of this project.

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
The Linux daemon uses a single EC lock, an
exclusive process lock at `/run/lock/medion-fan-control.lock`, and the
module's single-opener rule.

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

## Kernel allowlist (`x10_ec`)

Under Secure Boot the daemon talks to `/dev/x10-ec` instead of `/dev/port`.
The byte transport and the timing above are unchanged. The module additionally
enforces this state machine for each open file:

| After command | Accepted data byte |
|---|---|
| `0xd5` | `0x16`, `0x17`, `0x18`, `0x19` |
| `0xdd` | `0x20`, `0x23` |
| `0xde` | `0x01`, `0x02`, `0x03`, `0x05`, `0x0e`, `0x0f`, `0x10`, `0x11` |

Any other command byte, a data byte without a preceding command, or a selector
outside the list returns `EPERM` and is never sent to the hardware. Reads are
only allowed at `0x68`. The device allows a single opener (`EBUSY` otherwise).

## Voltage offsets (MSR 0x150)

Opt-in through the module parameter `allow_undervolt=1`. The module uses the
Intel OC mailbox on CPU 0:

| Operation | Request (`EDX:EAX`) |
|---|---|
| Read plane *p* | `0x80000010 \| p << 8 : 0x00000000`, then read MSR 0x150 |
| Write plane *p* | `0x80000011 \| p << 8 : (offset & 0x7ff) << 21` |

The offset is an 11-bit two's-complement value in 1/1.024 mV units. Planes:
0 core, 1 iGPU, 2 cache, 3 system agent (uncore), 4 analog I/O. A non-zero
status in response bits 39:32 returns `EIO`. A readback that differs from the
request by more than 1 mV returns `EPERM`; that happens when the firmware has
undervolt protection and ignores the write.

## Scope and limitations

The EC interface exposes only firmware profiles and the global fan override.
Lighting is limited to static colors for the four verified zones. "Off" is
static black, not a separate firmware command. Animated lighting and independent
keyboard subzones are not implemented.

There is no verified arbitrary fan RPM, PWM, fan-off, or temperature-curve
command. Vendor WPF fan-speed property setters only update displayed strings;
they do not issue hardware commands. Unrelated NBFC offsets and the standard
ACPI cooling-device states are not interchangeable with this protocol.

Protocol changes require attributable vendor or firmware evidence, preserving
the supported identity checks, timing, serialization, and bounded failure
behavior. See [Contributing](../CONTRIBUTING.md).
