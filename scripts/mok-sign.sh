#!/usr/bin/env bash
# Sign x10_ec.ko with a Machine Owner Key and queue that key for enrolment.
# DKMS (>= 3.0 on Debian/Ubuntu) already signs with /var/lib/dkms/mok.key;
# this script reuses that key so there is only one MOK to enrol.
#
#   sudo scripts/mok-sign.sh [path/to/x10_ec.ko]
set -euo pipefail

KEY_DIR=${MOK_DIR:-/var/lib/dkms}
KEY="$KEY_DIR/mok.key"
CERT="$KEY_DIR/mok.pub"
KVER=${KVER:-$(uname -r)}
MODULE=${1:-}

[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if [[ ! -f $KEY || ! -f $CERT ]]; then
    echo "Creating MOK key pair in $KEY_DIR"
    install -d -m 0700 "$KEY_DIR"
    openssl req -new -x509 -newkey rsa:2048 -nodes -days 36500 -outform DER \
        -subj "/CN=$(hostname) x10-control module signing key/" \
        -keyout "$KEY" -out "$CERT"
    chmod 0600 "$KEY"
fi

if [[ -n $MODULE ]]; then
    SIGN_FILE=""
    for candidate in "/usr/src/linux-headers-$KVER/scripts/sign-file" \
                     "/lib/modules/$KVER/build/scripts/sign-file" \
                     "/usr/lib/linux-kbuild-${KVER%.*}/scripts/sign-file"; do
        [[ -x $candidate ]] && SIGN_FILE=$candidate && break
    done
    if [[ -z $SIGN_FILE ]]; then
        SIGN_FILE=$(compgen -G "/usr/lib/linux-kbuild-*/scripts/sign-file" | sort -V | tail -1 || true)
    fi
    [[ -x $SIGN_FILE ]] || { echo "sign-file not found; install linux-headers-$KVER" >&2; exit 1; }
    "$SIGN_FILE" sha256 "$KEY" "$CERT" "$MODULE"
    echo "Signed $MODULE"
fi

if mokutil --test-key "$CERT" 2>/dev/null | grep -q "is already enrolled"; then
    echo "MOK already enrolled."
    exit 0
fi

echo
echo "Queueing $CERT for enrolment. Choose a one-time password now; after the"
echo "reboot, MokManager asks for it: Enroll MOK -> Continue -> Yes -> password."
mokutil --import "$CERT"
echo "Reboot to finish enrolment."
