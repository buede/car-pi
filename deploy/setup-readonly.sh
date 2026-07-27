#!/usr/bin/env bash
#
# Make the root filesystem read-only, so pulling the power cannot corrupt the SD card.
#
# NOT YET VERIFIED ON HARDWARE. Run with --dry-run first, and be aware this is the
# most invasive script here: it changes how the system boots. Have a way to reflash
# the card before you run it.
#
# Uses overlayroot, which Raspberry Pi OS provides via raspi-config. Writes go to an
# overlay held in RAM and are discarded at reboot. That is exactly what a unit powered
# from a battery bank in a bag needs, since it will be switched off by unplugging it.

set -euo pipefail

DRY_RUN=0
ACTION="enable"

usage() {
    sed -n '3,13p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --dry-run     print what would happen, change nothing
  --disable     turn the overlay off again, so changes persist
  -h, --help    this message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --disable) ACTION="disable" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup-readonly.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { echo "setup-readonly.sh: $*" >&2; }

do_or_show() {
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  would run: $*" >&2
    else
        "$@"
    fi
}

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    say "must run as root (or pass --dry-run)"
    exit 1
fi

if ! command -v raspi-config >/dev/null 2>&1; then
    say "raspi-config not found. This script is for Raspberry Pi OS; on another"
    say "distribution, configure overlayroot or systemd's volatile root directly."
    exit 1
fi

if [ "$ACTION" = "disable" ]; then
    say "disabling the read-only overlay"
    # do_overlayfs takes 0 to enable and 1 to disable, matching raspi-config's own
    # convention where 0 is success/yes. Easy to get backwards.
    do_or_show raspi-config nonint do_overlayfs 1
    cat >&2 <<EOF

setup-readonly.sh: overlay disabled. Reboot for it to take effect, after which changes
to the system will persist again.
EOF
    exit 0
fi

say "checks before enabling"

# Enabling the overlay while the service is broken leaves a unit that boots into a
# read-only system and does not work, and fixing it means another reboot cycle. Cheap
# to check now.
if systemctl is-enabled carpi.service >/dev/null 2>&1; then
    say "  carpi.service is enabled"
else
    say "  WARNING: carpi.service is not enabled. Run setup-service.sh first, or the"
    say "  unit will boot read-only without the thing it exists to run."
fi

if [ "$DRY_RUN" -eq 0 ]; then
    say "flushing pending writes before switching to a read-only root"
    sync
fi

do_or_show raspi-config nonint do_overlayfs 0

cat >&2 <<EOF

setup-readonly.sh: done. Reboot to switch over, then confirm:

    findmnt / --output SOURCE,FSTYPE,OPTIONS

The root filesystem should show as an overlay, mounted read-only underneath.

What this changes:

- Writes now go to RAM and are lost at reboot. Pulling the power is safe.
- System changes no longer persist. To make one:

      sudo ./deploy/setup-readonly.sh --disable
      sudo reboot
      # ... make the change ...
      sudo ./deploy/setup-readonly.sh
      sudo reboot

- Scan history was already in memory only, so nothing is lost that was not going to
  be. Download reports from the UI if you want to keep them.
EOF
