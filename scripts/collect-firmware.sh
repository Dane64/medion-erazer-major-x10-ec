#!/usr/bin/env bash

set -euo pipefail

readonly repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
readonly timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
readonly output_dir="${1:-$repo_root/artifacts/firmware-$timestamp}"

for command in find iasl sha256sum tar; do
    if ! command -v "$command" >/dev/null; then
        printf 'Required command is missing: %s\n' "$command" >&2
        exit 1
    fi
done

if [[ -e "$output_dir" ]]; then
    printf 'Refusing to overwrite existing path: %s\n' "$output_dir" >&2
    exit 1
fi

mkdir -m 0700 -p -- "$output_dir/acpi" "$output_dir/dsl" "$output_dir/wmi"

if (( EUID == 0 )); then
    privileged=()
else
    if ! command -v sudo >/dev/null; then
        printf 'Run this script as root or install sudo.\n' >&2
        exit 1
    fi

    printf '%s\n' \
        'This capture only reads firmware tables, kernel logs, and sysfs metadata.' \
        'It does not write EC registers, MMIO, WMI methods, or cooling states.'
    sudo -v
    privileged=(sudo)
fi

"${privileged[@]}" tar -C /sys/firmware/acpi -cf - tables \
    | tar -C "$output_dir/acpi" -xf -

while IFS= read -r -d '' bmof_file; do
    wmi_device="$(basename -- "$(dirname -- "$bmof_file")")"
    "${privileged[@]}" cat -- "$bmof_file" >"$output_dir/wmi/$wmi_device.bmof"
done < <(find /sys/bus/wmi/devices -mindepth 2 -maxdepth 2 -name bmof -print0)

{
    printf '%s\n' '--- kernel ---'
    uname -a
    printf '%s\n' '--- operating system ---'
    cat /etc/os-release
    printf '%s\n' '--- DMI identity ---'
    for attribute in \
        sys_vendor product_name product_version board_vendor board_name \
        board_version bios_vendor bios_version bios_date ec_firmware_release; do
        printf '%-20s' "$attribute:"
        cat "/sys/class/dmi/id/$attribute" 2>/dev/null || printf '<unavailable>\n'
    done
} >"$output_dir/system.txt"

{
    for device in /sys/bus/wmi/devices/*; do
        [[ -e "$device" ]] || continue
        printf '\n[%s]\n' "$(basename -- "$device")"
        for attribute in guid instance_count object_id notify_id setable modalias; do
            [[ -r "$device/$attribute" ]] || continue
            printf '%s=' "$attribute"
            cat "$device/$attribute"
        done
    done
} >"$output_dir/wmi/devices.txt"

{
    for zone in /sys/class/thermal/thermal_zone*; do
        [[ -e "$zone" ]] || continue
        printf '\n[%s]\n' "$(basename -- "$zone")"
        for attribute in "$zone"/type "$zone"/temp "$zone"/mode \
            "$zone"/policy "$zone"/trip_point_* "$zone"/cdev*; do
            [[ -r "$attribute" ]] || continue
            printf '%s=' "$(basename -- "$attribute")"
            cat "$attribute"
        done
    done

    for device in /sys/class/thermal/cooling_device*; do
        [[ -e "$device" ]] || continue
        printf '\n[%s]\n' "$(basename -- "$device")"
        for attribute in type cur_state max_state; do
            printf '%s=' "$attribute"
            cat "$device/$attribute"
        done
    done
} >"$output_dir/thermal.txt"

"${privileged[@]}" journalctl -k -b --no-pager >"$output_dir/kernel.log"
"${privileged[@]}" acpidump -s >"$output_dir/acpi/table-summary.txt"
"${privileged[@]}" dmidecode --type 0,1,2,3 >"$output_dir/dmidecode.txt"

iasl_status=0
iasl -d -p "$output_dir/dsl/DSDT" "$output_dir/acpi/tables/DSDT" \
    >"$output_dir/dsl/iasl.log" 2>&1 || iasl_status=$?
printf '%s\n' "$iasl_status" >"$output_dir/dsl/iasl.status"

(
    cd -- "$output_dir"
    find . -type f ! -name SHA256SUMS -print0 \
        | sort -z \
        | xargs -0 sha256sum
) >"$output_dir/SHA256SUMS"

printf 'Firmware capture written to %s\n' "$output_dir"
printf 'DSDT decompiler exit status: %s\n' "$iasl_status"