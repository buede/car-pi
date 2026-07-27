#!/usr/bin/env bash
#
# Bring up a virtual CAN interface, then exec whatever was asked for.
#
# Used by `./dev socketcan` inside a container, and usable directly on any Linux box.
# vcan behaves like a real CAN interface as far as SocketCAN is concerned: two sockets
# on it see each other's frames but not their own, which is the topology of a real bus.
#
# Requires CAP_NET_ADMIN, and the vcan module has to be loadable -- so a container
# needs --privileged and the host's /lib/modules.

set -euo pipefail

INTERFACE="${CARPI_TEST_SOCKETCAN:-vcan0}"

if ip link show "$INTERFACE" >/dev/null 2>&1; then
    echo "vcan.sh: $INTERFACE already exists" >&2
else
    if ! ip link add dev "$INTERFACE" type vcan 2>/dev/null; then
        # The type is provided by a module that is not always built in.
        if ! modprobe vcan 2>/dev/null; then
            cat >&2 <<EOF
vcan.sh: cannot load the 'vcan' kernel module.

The kernel running underneath does not have it. vcan is a virtual CAN driver, and it
lives in an add-on package on most distributions:

    Debian/Ubuntu:  sudo apt-get install linux-modules-extra-\$(uname -r)
    Fedora:         sudo dnf install kernel-modules-extra

On macOS or Windows the module has to be in the virtual machine your container runtime
uses, not on your host. For podman's default machine (Fedora CoreOS), that means:

    podman machine ssh sudo rpm-ostree install kernel-modules-extra
    podman machine stop && podman machine start

This is optional. The SocketCAN tests are run on every push by CI, on a Linux runner
where vcan is available, so skipping them locally costs coverage of nothing -- the rest
of the suite covers all the protocol logic over a virtual bus that needs no kernel
support at all.
EOF
            exit 1
        fi
        ip link add dev "$INTERFACE" type vcan
    fi
    ip link set up "$INTERFACE"
    echo "vcan.sh: $INTERFACE is up" >&2
fi

if [ "$#" -eq 0 ]; then
    exit 0
fi

exec "$@"
