# User guide

[Back to the project overview](../README.md)

## Getting started

1. Install the app with `sudo scripts/install.sh` (see the [README](../README.md#install)).
2. Log out and back in once, so your user is a member of the `x10ctl` group.
3. Start **Erazer Control** from the application menu, or run `x10-control`.
   No `sudo` is needed.

The window has three areas:

| Area | Purpose |
|---|---|
| **Sidebar** | One icon per page: Dashboard, CPU, GPU, Display, Audio, LED. The highlighted entry is the open page. At the bottom it shows whether Secure Boot is on and the signed module is loaded. |
| **Page** | A header with the page icon, title and a **Reload** button, then panels with the controls. Changes that touch hardware ask for confirmation first; the default answer is always *No*. |
| **Status bar** | Connection state (`EC CONNECTED`, `DEMO DATA`, `OFFLINE`), the last message from the daemon, and **Retry connection**. |

The badge in the top-right corner always shows the current firmware
performance mode as read back from the hardware.

The CPU, GPU, Audio and LED pages have a **Re-apply ... at startup** switch.
When it's on, `x10ctld` re-applies that page's last saved settings at boot.

## Safety and access model

- `x10ctld` runs as root and is the only process that touches hardware. The GUI
  talks to it over `/run/x10ctld/x10ctld.sock` (`root:x10ctl`, mode 0660). The
  daemon also checks each client's credentials, so only root and `x10ctl`
  members are served.
- The EC is reached through the signed `x10_ec` module, which only forwards the
  documented commands. Without the module, the daemon falls back to `/dev/port`,
  and only when the kernel is not locked down.
- Every hardware write is confirmed by readback where the interface allows it.
  If a readback fails, the state is reported as unknown, never as success.
- Settings are saved in `/var/lib/x10ctld/state.json` (root, 0600) after a
  successful apply. **Re-apply at startup** is a per-tab switch.
- Don't run another EC-writing tool at the same time. The exclusive open of
  `/dev/x10-ec` coordinates only with this app.

## Dashboard

**Performance mode** selects the vendor's Office, Gaming or Turbo profile, after
a confirmation that defaults to *No*. The shown mode is the firmware's readback.
Turbo needs the AC barrel adapter, and the daemon checks this again before every
Turbo write.

**Full-speed fans** is a global override. Closing the app leaves it unchanged;
switch it off to return to the profile's automatic fan behavior.

The graph shows fan RPM (solid lines, left axis) and temperature (dashed lines,
right axis). The window slider covers 30 seconds to 10 minutes. History is kept
in memory only.

## CPU

| Control | Interface |
|---|---|
| Turbo boost, min/max performance %, HWP dynamic boost | `intel_pstate` |
| Governor, energy-performance preference | every cpufreq policy |
| P-core / E-core frequency limits | cpufreq policies grouped by hardware maximum |
| PL1, PL1 time window, PL2 | `intel-rapl` and `intel-rapl-mmio` package zones (both are written) |
| Voltage offsets | `x10_ec` OC mailbox, opt-in |

The EC firmware profile can still enforce its own power limits on top of RAPL.
Check the result under load.

**Voltage offsets.** Lower in 10 mV steps and stress-test each step. Too much
undervolt freezes the system. If startup restore is on and a boot does not
survive 120 seconds, the next start skips the offsets once. You can change the
window with `undervolt_stability_s` in `/etc/x10ctld.conf`. If the module
reports that the firmware ignored the offset, the BIOS has undervolt protection
enabled.

## GPU

**Discrete GPU power** runs your `arc-dgpu-ctl` (`on` / `off` by default; set the
verbs in `[dgpu]` of `/etc/x10ctld.conf`). The state badge comes from sysfs:
*off* (not on the PCI bus), *unbound* (no driver), *suspended* or *active*
(runtime PM). Close applications that use the dGPU before you power it off.

**Frequency limits** use i915 `gt_*_freq_mhz` or xe `tile0/gt0/freq0/*`, bounded
to RPn..RP0. **Power limit** uses the driver's hwmon `power1_max`, bounded by
`power1_rated_max`. While the dGPU is powered off, its card isn't listed.

## Display

- **Backlight** writes the panel's backlight device.
- **Refresh rate and VRR** use `kscreen-doctor` in your Plasma session. On other
  desktops this panel explains that it isn't available.
- **Panel overclock** creates `/usr/lib/firmware/edid/x10-<connector>-<Hz>.bin`:
  a copy of the panel EDID with the fastest timing's pixel clock scaled up.
  Nothing changes until you add the shown parameter, for example
  `drm.edid_firmware=eDP-1:edid/x10-eDP-1-170hz.bin`, to
  `GRUB_CMDLINE_LINUX_DEFAULT`, then run `sudo update-initramfs -u && sudo update-grub`
  and reboot. The installed initramfs hook copies the override so it also works
  when i915 loads early.
  - The ceiling is the lower of +20 % and the EDID limit of 655.35 MHz.
  - The app refuses when the fastest mode is stored in a DisplayID or CTA
    extension block. Overriding the base timing would *lower* the refresh rate.
  - If the panel stays black, press `e` in GRUB, delete the parameter for that
    boot, then remove it from `/etc/default/grub` and select **Remove override**.

## Audio

The sound-card panel lists every writable mixer control that the codec driver
publishes (`amixer contents`): output and input volumes, switches, auto-mute,
loopback mixing, mic boost and routing. Stereo controls are kept balanced.
These run in your session, like any mixer.

**HDA power saving** sets `snd_hda_intel` `power_save` and
`power_save_controller`. Use 0 if you hear a click when audio starts.

## LED

Pick a color per zone, or for all zones at once. Each zone has an **on/off
switch**. *Off* sends black (R=G=B=0), which leaves the LEDs without current
and needs no unverified firmware command. The zone keeps its color for when you
switch it on again. **Brightness** scales the RGB values (0 % = all off).
**Apply lighting** asks for confirmation, writes three complete passes and saves
the selection. There's no physical readback of LED colors.

## Troubleshooting

| Message | Action |
|---|---|
| x10ctld is not running | `systemctl status x10ctld`, `journalctl -u x10ctld` |
| permission denied: add your user to the x10ctl group | `sudo usermod -aG x10ctl $USER`, then log out and in |
| Secure Boot ... x10_ec module is not loaded | See [Secure Boot and MOK](secure-boot.md) |
| Unsupported machine or firmware | The app only runs on the Erazer Major X10 it was built for. Don't bypass the check. |
| /dev/x10-ec is already open in another process | Another tool is using the EC; stop it |
| Telemetry or change readback failed | Controls are disabled because the state is unknown; fix the cause, then **Retry connection** |
| arc-dgpu-ctl not found | Install it or set `[dgpu] command` in `/etc/x10ctld.conf` |
| Qt cannot load the xcb or wayland plugin | Install the Qt platform plugin's system libraries |

When you report a problem, include the app version, distribution, session type
and the exact message. Don't upload serial numbers, full DMI dumps or firmware
binaries.
