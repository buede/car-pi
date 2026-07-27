#!/usr/bin/env bash
#
# Turn the Pi's built-in WiFi into an access point, so a phone can reach the UI with
# no infrastructure at all.
#
# NOT YET VERIFIED ON HARDWARE. Run with --dry-run first.
#
# Uses NetworkManager, which is what Raspberry Pi OS Bookworm ships. Older guides
# describing hostapd and dnsmasq by hand do not apply and will fight with it.

set -euo pipefail

DRY_RUN=0
SSID="carpi"
PASSWORD=""
CONNECTION="carpi-hotspot"
IFACE="wlan0"
ADDRESS="10.42.0.1/24"

usage() {
    sed -n '3,11p' "$0" | sed 's/^# \{0,1\}//'
    cat <<EOF

Options:
  --dry-run              print what would happen, change nothing
  --ssid NAME            network name (default: $SSID)
  --password SECRET      WPA2 passphrase, at least 8 characters (required)
  --interface NAME       wireless interface (default: $IFACE)
  --address CIDR         the Pi's address on its own network (default: $ADDRESS)
  --remove               delete the hotspot connection
  -h, --help             this message

There is no default password on purpose. A shipped default is a shipped vulnerability,
and this one would be broadcasting its SSID from a car park.
EOF
}

REMOVE=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --ssid) SSID="$2"; shift ;;
        --password) PASSWORD="$2"; shift ;;
        --interface) IFACE="$2"; shift ;;
        --address) ADDRESS="$2"; shift ;;
        --remove) REMOVE=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "setup-hotspot.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
    esac
    shift
done

say() { echo "setup-hotspot.sh: $*" >&2; }

do_or_show() {
    if [ "$DRY_RUN" -eq 1 ]; then
        # The passphrase is redacted so it does not end up in a terminal scrollback
        # or a pasted bug report.
        printf '  would run:' >&2
        for arg in "$@"; do
            case "$arg" in
                *psk*) printf ' %s' "wifi-sec.psk=<redacted>" >&2 ;;
                *) printf ' %s' "$arg" >&2 ;;
            esac
        done
        printf '\n' >&2
    else
        "$@"
    fi
}

if [ "$DRY_RUN" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
    say "must run as root (or pass --dry-run)"
    exit 1
fi

if ! command -v nmcli >/dev/null 2>&1; then
    say "nmcli not found. This script needs NetworkManager, as shipped by Raspberry Pi"
    say "OS Bookworm. On an older image, configure hostapd by hand instead."
    exit 1
fi

if [ "$REMOVE" -eq 1 ]; then
    say "removing connection $CONNECTION"
    do_or_show nmcli connection delete "$CONNECTION"
    exit 0
fi

if [ "${#PASSWORD}" -lt 8 ]; then
    say "--password is required and must be at least 8 characters (WPA2 minimum)"
    exit 2
fi

say "ssid:       $SSID"
say "interface:  $IFACE"
say "address:    $ADDRESS"

if nmcli -t -f NAME connection show 2>/dev/null | grep -qx "$CONNECTION"; then
    say "connection $CONNECTION exists; deleting it first so settings are not merged"
    do_or_show nmcli connection delete "$CONNECTION"
fi

do_or_show nmcli connection add \
    type wifi \
    ifname "$IFACE" \
    con-name "$CONNECTION" \
    autoconnect yes \
    ssid "$SSID"

# ipv4.method shared makes NetworkManager run the DHCP server and hand out addresses,
# which is what lets a phone join and get an address without any further setup.
do_or_show nmcli connection modify "$CONNECTION" \
    802-11-wireless.mode ap \
    802-11-wireless.band bg \
    ipv4.method shared \
    ipv4.addresses "$ADDRESS" \
    ipv6.method disabled \
    wifi-sec.key-mgmt wpa-psk \
    wifi-sec.proto rsn \
    wifi-sec.pairwise ccmp \
    wifi-sec.group ccmp \
    wifi-sec.psk "$PASSWORD"

do_or_show nmcli connection up "$CONNECTION"

HOST="${ADDRESS%%/*}"
cat >&2 <<EOF

setup-hotspot.sh: done.

Join the WiFi network "$SSID" from your phone, then open:

    http://${HOST}:8080/

Two things worth knowing:

- Using the built-in WiFi as an access point means the Pi cannot also be a client on
  your home network. For development, remove the hotspot (--remove) or reach the unit
  over Ethernet.
- WPA2 only, and no WPA3: some phones will warn about weak security. Raising it would
  exclude older phones, which is the wrong trade for a tool whose whole job is to work
  with whatever is in your pocket in a car park.
EOF
