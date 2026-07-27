#!/usr/bin/env bash
#
# Configure a CAN FD HAT on a Raspberry Pi and bring can0 up at boot.
#
# NOT YET VERIFIED ON HARDWARE. Written from the Raspberry Pi and mcp251xfd
# documentation. Run with --dry-run first and read what it intends to do.
#
# Assumes an MCP2518FD-based HAT (Waveshare 2-CH CAN FD, SK Pang PiCAN FD, and most
# others). MCP2515 boards are CAN 2.0 only and will not work for CAN FD vehicles;
# they need a different overlay in any case.

set -euo pipefail

DRY_RUN=0
INTERFACE="can0"
OSCILLATOR=40000000
INTERRUPT=25
CONFIG="/boot/firmware/config.txt"

usage() {
    sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --dry-run             print the changes without making them
  --interface NAME      interface name (default: $INTERFACE)
  --oscillator HZ       HAT crystal frequency (default: $OSCILLATOR)
  --interrupt PIN       BCM pin the HAT uses for interrupts (default: $INTERRUPT)
  --config PATH         firmware config file (default: $CONFIG)
  -h, --help            this message
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --interface) INTERFACE="$2"; shift ;;
        --oscillator) OSCILLATOR="$2"; shift ;;
        --interrupt) INTERRUPT="$2"; shift ;;
        --config) CONFIG="$2"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup-can.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { echo "setup-can.sh: $*" >&2; }

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

if [ ! -f "$CONFIG" ]; then
    say "no $CONFIG here. On Raspberry Pi OS before Bookworm it is /boot/config.txt;"
    say "pass --config if yours is elsewhere. Refusing to guess."
    exit 1
fi

# The crystal frequency has to match the board. Getting it wrong does not fail loudly:
# the interface comes up and then silently misreads every frame on the bus, because
# every bit time is scaled wrong. Waveshare and SK Pang both ship 40 MHz.
OVERLAY="dtoverlay=mcp251xfd,spi0-0,interrupt=${INTERRUPT},oscillator=${OSCILLATOR}"

say "interface:  $INTERFACE"
say "overlay:    $OVERLAY"
say "config:     $CONFIG"

if grep -qF "$OVERLAY" "$CONFIG"; then
    say "overlay already present, leaving $CONFIG alone"
else
    if grep -q '^dtoverlay=mcp251' "$CONFIG"; then
        say "WARNING: a different mcp251x overlay is already configured:"
        grep -n '^dtoverlay=mcp251' "$CONFIG" >&2
        say "Remove it by hand before adding another; two will fight over the SPI bus."
        exit 1
    fi
    say "appending the overlay to $CONFIG"
    if [ "$DRY_RUN" -eq 1 ]; then
        echo "  would append: dtparam=spi=on" >&2
        echo "  would append: $OVERLAY" >&2
    else
        cp -a "$CONFIG" "${CONFIG}.carpi.bak"
        say "backed up to ${CONFIG}.carpi.bak"
        {
            echo ""
            echo "# added by car-pi setup-can.sh"
            echo "dtparam=spi=on"
            echo "$OVERLAY"
        } >>"$CONFIG"
    fi
fi

UNIT_SRC="$(dirname "$0")/carpi-can.service"
if [ -f "$UNIT_SRC" ]; then
    say "installing carpi-can.service"
    do_or_show install -m 0644 "$UNIT_SRC" /etc/systemd/system/carpi-can.service
    do_or_show systemctl daemon-reload
    do_or_show systemctl enable carpi-can.service
else
    say "WARNING: $UNIT_SRC not found; skipping the boot-time unit"
fi

cat >&2 <<EOF

setup-can.sh: done. Reboot for the overlay to take effect, then check the interface:

    ip -details link show $INTERFACE

Before letting anything transmit, confirm the bus is healthy in listen-only mode.
Listen-only cannot transmit, so it cannot disturb a vehicle:

    sudo ip link set $INTERFACE down
    sudo ip link set $INTERFACE type can bitrate 500000 listen-only on
    sudo ip link set $INTERFACE up
    candump $INTERFACE          # expect error-free traffic, ignition ON

Nothing showing up usually means the wrong bitrate (try 250000), CAN_H and CAN_L
swapped, or the ignition only in accessory mode. A flood of error frames usually means
the HAT's 120 ohm termination jumper is fitted -- it should not be, because the vehicle
bus is already terminated at both ends.

Then restore the normal configuration:

    sudo systemctl restart carpi-can.service
EOF
