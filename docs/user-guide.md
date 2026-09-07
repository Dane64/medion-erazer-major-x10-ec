# User guide

[Back to the project overview](../README.md)

## Safety and hardware access

This application supports only the exact DMI, BIOS, and EC identity listed in
the [README](../README.md#supported-hardware). It refuses other identities rather
than guessing compatible firmware commands.

The EC transport uses `/dev/port`. Even a telemetry read sends command and
selector bytes to hardware. Incorrect raw I/O can corrupt hardware state.
Do not run another EC-writing tool alongside this application, reuse values
from unrelated laptop profiles, or write to tachometer selectors.

The application holds an exclusive lock for the EC session and a separate lock
while applying lighting. These locks coordinate instances of this application;
they cannot stop another program that ignores them.

The GUI currently runs with the privileges needed for hardware access; there
is no separate privileged service. Install dependencies as your normal user,
then elevate only the installed launcher as shown in the README. No device
permissions, boot settings, or services are changed automatically.

### Secure Boot and kernel lockdown

The application checks both the UEFI Secure Boot variable and the kernel
lockdown state before opening the EC transport. It reports malformed or
unreadable states instead of assuming access is safe.

If Secure Boot is enabled, use `--demo` unless you deliberately choose to
change the machine's boot policy. **Disabling Secure Boot reduces boot
protection** and may cause disk encryption or another operating system to ask
for a recovery key.

To change it, first have any recovery keys available. Restart into UEFI/BIOS
setup using the key shown at startup, commonly F2. Find Secure Boot under
Security or Boot, set it to Disabled, save, and reboot.

When the kernel exposes the lockdown interface, an unlocked system reports:

```text
$ cat /sys/kernel/security/lockdown
[none] integrity confidentiality
```

Lockdown can also be enabled independently of Secure Boot. If `integrity` or
`confidentiality` remains selected, consult your distribution's kernel and boot
policy. The app cannot bypass lockdown, even as root.

## Dashboard

**Performance mode** selects the vendor's Office, Gaming, or Turbo profile.
Changing it requires confirmation, with **No** selected by default. The
displayed mode comes from firmware readback, not from the requested button.

**Turbo** is disabled when Linux reports the AC barrel adapter offline. The
backend checks `/sys/class/power_supply/AC0/online` before every Turbo write.
Unplugging the adapter while a change is pending cannot bypass that check.

**Full speed** is a global override for both fans. Disable it to return to the
automatic policy of the selected performance profile. Closing the app does not
disable the override or restore an earlier profile.

The graph shows fan RPM on the left axis and temperatures in Celsius on the
right. Solid lines represent fan speed; dashed lines represent temperatures.
The window slider changes the visible history from 30 seconds to 10 minutes.
History is held in memory and is not logged to disk.

On a transport or readback error, current readings become unavailable and
hardware controls are disabled. Resolve the reported problem and select
**Retry connection**. The app opens a new guarded session instead of continuing
to display stale values as live measurements.

## Lighting

Choose a zone or **All zones** to stage a color. Swatches show your selection,
not a readback of the physical LEDs. **Apply colors** asks for confirmation,
applies the selected zones in three complete passes, and saves only a
successfully applied selection.

Saved colors are restored once after the hardware session first connects.
Unconfirmed edits are never included in that automatic restore. The restore
does not change the firmware performance profile or fan override.

Lighting has its own persistent status message so telemetry updates do not
hide write or save errors. If applying succeeds but saving fails, the message
explicitly distinguishes the two outcomes. Already-confirmed work finishes
when the window closes, including saving the applied colors.

Settings are stored in:

```text
${XDG_CONFIG_HOME:-~/.config}/medion-fan-control/lighting.json
```

`XDG_CONFIG_HOME`, when set, must be an absolute path. The settings belong to
the effective user running the GUI. With the documented `sudo -H` command
and no configured `XDG_CONFIG_HOME`, that is normally
`/root/.config/medion-fan-control/lighting.json`, not your desktop user's home.
Files are replaced atomically with owner-only permissions.

To stop restoring a saved selection, close the application and remove its
`lighting.json` file. This does not itself send any lighting command. A malformed
file is reported and is not automatically applied.

Lighting uses a separate, verified HID interface. Manual lighting changes can
still work when EC access is unavailable, provided the machine identity, HID
descriptor, and device permissions are valid.

## Demo mode

`medion-fan-control --demo` simulates profile changes, full-speed state, and
telemetry. It also lets you preview static lighting selections. It does not
open hardware, read saved lighting, or write settings. The window title and
connection label identify the simulation.

## Troubleshooting

| Message or symptom | Action |
| --- | --- |
| Administrator access required | Run the installed launcher with administrator privileges, not `sudo uv`. Lockdown can still block access. |
| Unsupported machine or firmware | Compare the reported fields with the allowlist. Use demo mode; do not bypass the guard. |
| Another process is using the device | Close the other instance. Do not delete an active lock file to bypass the lock. |
| AC0 status cannot be read | Resolve the missing or unreadable kernel power-supply interface. The backend will not assume Turbo is safe. |
| Lighting interface not found | The required USB identity, interface, and descriptor were not found. Do not substitute another hidraw node. |
| Telemetry or change readback failed | Controls are disabled because the current state is unknown. Resolve the device error, then retry. |
| Colors applied but not saved | Check the effective user's configuration directory and disk space, then apply again. |
| Qt cannot load the `xcb` or `wayland` plugin | Install the Qt platform plugin's system-library requirements using your distribution's package manager. |
| Privileged GUI cannot connect to the display | Check the desktop authorization and display variables preserved by `sudo`. Do not disable display access control globally with `xhost +`. |

Include the application version, Linux distribution, desktop session type,
and exact error message when reporting a problem. Share only the allowlisted
identity fields when necessary; do not upload serial numbers, full DMI dumps,
firmware binaries, or unreviewed system logs.
