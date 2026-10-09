#!/usr/bin/env bash
# Remove x10-control. The enrolled MOK is left in place (it may sign other
# DKMS modules); remove it with: mokutil --delete /var/lib/dkms/mok.pub
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
systemctl disable --now x10ctld.service 2>/dev/null || true
modprobe -r x10_ec 2>/dev/null || true
for old in $(dkms status x10-ec 2>/dev/null | sed -n 's#^x10-ec/\([^,:]*\).*#\1#p' | sort -u); do
    dkms remove "x10-ec/$old" --all || true
    rm -rf "/usr/src/x10-ec-$old"
done
rm -f /etc/systemd/system/x10ctld.service /etc/modules-load.d/x10-ec.conf \
      /etc/modprobe.d/x10-ec.conf /etc/udev/rules.d/70-x10-ec.rules \
      /etc/initramfs-tools/hooks/x10-edid /usr/share/applications/x10-control.desktop \
      /usr/share/icons/hicolor/scalable/apps/x10-control.svg
rm -rf /opt/x10-control
systemctl daemon-reload
echo "Removed. Kept: /etc/x10ctld.conf, /var/lib/x10ctld, x10ctl group, EDID overrides in /usr/lib/firmware/edid/x10-*.bin"
