# Secure Boot and MOK

[Back to the project overview](../README.md)

## Why a kernel module

With UEFI Secure Boot enabled, the kernel enters *lockdown* mode. Lockdown
blocks raw port I/O (`/dev/port`, `ioperm`, `iopl`) and writes through
`/dev/cpu/*/msr`, **even for root**. Disabling Secure Boot is no longer needed:
`x10_ec` moves the two operations the app needs into the kernel and narrows
them.

| Interface | What it allows |
|---|---|
| `/dev/x10-ec` (`root` 0600, one opener) | One-byte `pread`/`pwrite` at offset `0x6c` (command) and `0x68` (data), the same semantics as `/dev/port`. A command byte must be `0xd5`, `0xdd` or `0xde`. The next data byte must be one of that command's documented selectors or actions. Everything else returns `EPERM`. |
| `/sys/class/misc/x10-ec/undervolt_*` | Voltage offsets for core, cache, gpu, uncore and analogio through MSR 0x150. Off unless loaded with `allow_undervolt=1`. Values are limited to `[undervolt_min_mv, 0]` (default -150 mV, hard floor -250 mV). Each write is read back, and a mismatch (BIOS undervolt protection) returns `EPERM`. |

The module refuses to load unless the DMI vendor, product, board and BIOS
version match the supported machine.

The desktop user never touches these files. `x10ctld` (root) uses them and
serves the GUI on a socket that only `x10ctl` members can open. That's how the
GUI runs without privilege escalation.

## Signing and enrolment (automatic)

`scripts/install.sh` installs the module through DKMS. Current Debian and
Ubuntu DKMS creates `/var/lib/dkms/mok.key` and `mok.pub` on first use and
signs every module it builds with them, including after kernel updates. The
installer then runs `scripts/mok-sign.sh`, which queues `mok.pub` with
`mokutil --import` if it isn't enrolled yet.

Reboot. In MokManager choose *Enroll MOK -> Continue -> Yes*, enter the one-time
password, and reboot again. From then on, `x10_ec` loads automatically via
`/etc/modules-load.d/x10-ec.conf`.

## Manual build and signing

```bash
make -C kernel
sudo scripts/mok-sign.sh kernel/x10_ec.ko      # signs; queues the MOK if needed
sudo insmod kernel/x10_ec.ko allow_undervolt=1
```

`mok-sign.sh` uses the same key pair as DKMS, so you only enrol one MOK. Set
`MOK_DIR=/path` to use a different key directory.

## Verify

```bash
mokutil --sb-state                       # SecureBoot enabled
mokutil --test-key /var/lib/dkms/mok.pub # ... is already enrolled
modinfo -F signer x10_ec                 # matches the MOK common name
cat /sys/kernel/security/lockdown        # [integrity] is expected and fine
ls -l /dev/x10-ec                        # crw------- root root
cat /sys/class/misc/x10-ec/undervolt_enabled
```

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Key was rejected by service` on `modprobe` | The MOK is not enrolled. Run `sudo scripts/mok-sign.sh`, reboot and complete MokManager. |
| MokManager never appeared | Some firmware skips it after a fast reboot. Run `sudo mokutil --import /var/lib/dkms/mok.pub` again and do a full power cycle. |
| `x10_ec: unsupported machine` in `dmesg` | DMI identity mismatch; the module intentionally does not load. |
| `undervolt_enabled` is 0 | Reinstall with `--enable-undervolt`, or set `allow_undervolt=1` in `/etc/modprobe.d/x10-ec.conf` and reload the module. |
| Offsets fail with *firmware ignored the voltage offset* | The BIOS locks the OC mailbox (undervolt protection). Nothing the OS can change. |
| Module missing after a kernel update | `sudo dkms autoinstall`, and check `dkms status`. |

## Removing the key

`scripts/uninstall.sh` keeps the MOK because other DKMS modules may be signed
with it. To remove it anyway, run `sudo mokutil --delete /var/lib/dkms/mok.pub`,
reboot, and confirm in MokManager.
