#!/usr/bin/env bash
# Install the x10-control stack on Debian/Ubuntu:
#   signed x10_ec kernel module (DKMS + MOK), x10ctld root daemon, and the
#   unprivileged Erazer Control GUI for members of the x10ctl group.
#
#   sudo scripts/install.sh [--enable-undervolt] [--without-arc-dgpu-ctl] [--user NAME]
set -euo pipefail

REPO=$(cd "$(dirname "$0")/.." && pwd)
PREFIX=/opt/x10-control
ENABLE_UV=0
WITH_ARC=1
TARGET_USER=${SUDO_USER:-}

while [[ $# -gt 0 ]]; do
    case $1 in
        --enable-undervolt) ENABLE_UV=1 ;;
        --without-arc-dgpu-ctl) WITH_ARC=0 ;;
        --user) TARGET_USER=$2; shift ;;
        *) echo "unknown option $1" >&2; exit 2 ;;
    esac
    shift
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

as_owner() {
    if [[ -n ${SUDO_USER:-} ]]; then sudo -u "$SUDO_USER" "$@"; else "$@"; fi
}

if VERSION=$(as_owner git --no-optional-locks -C "$REPO" describe --tags --always 2>/dev/null); then
    VERSION=${VERSION#v}
elif [[ -f $REPO/PKG-INFO ]]; then
    VERSION=$(sed -n 's/^Version: //p' "$REPO/PKG-INFO")
else
    VERSION=0.0.0.dev0
fi
# hatch-vcs must not run git or write _version.py inside the user's checkout as root
PEP440_VERSION=$(sed -E 's/^([^-]+)-([0-9]+)-g([0-9a-f]+)$/\1.post\2+g\3/; t; s/^[0-9a-f]+$/0.0.0+g&/' <<<"$VERSION")
export SETUPTOOLS_SCM_PRETEND_VERSION=$PEP440_VERSION
VERSION=${VERSION//[^0-9A-Za-z.+~]/.}
BUILD_DIR=$(mktemp -d)
trap 'rm -rf "$BUILD_DIR"' EXIT

echo "==> Packages"
apt-get install -y --no-install-recommends dkms "linux-headers-$(uname -r)" mokutil openssl \
    python3-venv python3-pip alsa-utils

echo "==> Kernel module (DKMS $VERSION)"
SRC=/usr/src/x10-ec-$VERSION
for old in $(dkms status x10-ec 2>/dev/null | sed -n 's#^x10-ec/\([^,:]*\).*#\1#p' | sort -u); do
    dkms remove "x10-ec/$old" --all || true
done
rm -rf "$SRC"; install -d "$SRC"
install -m 0644 "$REPO/kernel/x10_ec.c" "$REPO/kernel/Makefile" "$SRC/"
sed "s/@VERSION@/$VERSION/" "$REPO/kernel/dkms.conf" > "$SRC/dkms.conf"
dkms add "x10-ec/$VERSION"
dkms install "x10-ec/$VERSION"

install -m 0644 "$REPO/packaging/modules-load/x10-ec.conf" /etc/modules-load.d/x10-ec.conf
install -m 0644 "$REPO/packaging/modprobe/x10-ec.conf" /etc/modprobe.d/x10-ec.conf
if [[ $ENABLE_UV -eq 1 ]]; then
    sed -i 's/allow_undervolt=0/allow_undervolt=1/' /etc/modprobe.d/x10-ec.conf
fi
install -m 0644 "$REPO/packaging/udev/70-x10-ec.rules" /etc/udev/rules.d/70-x10-ec.rules
udevadm control --reload-rules

if [[ $WITH_ARC -eq 1 ]]; then
    echo "==> arc-dgpu-ctl"
    if command -v arc-dgpu-ctl >/dev/null || dpkg -s arc-dgpu-ctl >/dev/null 2>&1; then
        echo "already installed: $(arc-dgpu-ctl version 2>/dev/null || echo 'Debian package')"
    elif [[ -f $REPO/ext/arc-dgpu-ctl/install.sh ]]; then
        bash "$REPO/ext/arc-dgpu-ctl/install.sh"
    else
        echo "ext/arc-dgpu-ctl is missing; run 'git submodule update --init' or install it separately." >&2
    fi
fi
echo "==> Daemon and GUI ($PREFIX)"
python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install --upgrade pip >/dev/null
tar -C "$REPO" --exclude=.git --exclude=.venv --exclude='*/_version.py' -cf - . | tar -C "$BUILD_DIR" -xf -
"$PREFIX/venv/bin/pip" install "$BUILD_DIR"
[[ -f /etc/x10ctld.conf ]] || install -m 0644 "$REPO/packaging/x10ctld.conf" /etc/x10ctld.conf
install -d -m 0755 /usr/lib/firmware/edid
install -m 0755 "$REPO/packaging/initramfs-tools/hooks/x10-edid" /etc/initramfs-tools/hooks/x10-edid
install -m 0644 "$REPO/packaging/systemd/x10ctld.service" /etc/systemd/system/x10ctld.service
install -m 0644 "$REPO/packaging/desktop/x10-control.desktop" /usr/share/applications/x10-control.desktop
install -D -m 0644 "$REPO/medion_fan_control/assets/x10-control.svg" \
    /usr/share/icons/hicolor/scalable/apps/x10-control.svg
gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true

getent group x10ctl >/dev/null || groupadd --system x10ctl
if [[ -n $TARGET_USER ]]; then
    usermod -aG x10ctl "$TARGET_USER"
    echo "Added $TARGET_USER to x10ctl (log out and back in to apply)."
fi

systemctl daemon-reload
systemctl enable x10ctld.service

echo "==> Secure Boot"
if mokutil --sb-state 2>/dev/null | grep -q "SecureBoot enabled"; then
    if ! mokutil --test-key /var/lib/dkms/mok.pub 2>/dev/null | grep -q "already enrolled"; then
        bash "$REPO/scripts/mok-sign.sh"
        echo "Reboot, enrol the key in MokManager, then x10_ec and x10ctld start automatically."
        exit 0
    fi
fi

modprobe -r x10_ec 2>/dev/null || true
modprobe x10_ec
systemctl restart x10ctld.service
echo "Done. Start 'Erazer Control' from the application menu (no sudo needed)."
