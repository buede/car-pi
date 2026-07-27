# Deploying car-pi to a Raspberry Pi

> **None of this has been run on real hardware yet.** It is written from the
> documentation for Raspberry Pi OS Bookworm, NetworkManager and the `mcp251xfd`
> driver, and it is deliberately conservative — but treat every step as a hypothesis
> until you have confirmed it on a Pi. Errors here are cheap to fix and expensive to
> assume away. Corrections very welcome.

The target is a portable inspection unit: a Pi 4 in a case, powered from a USB battery
bank, that brings up its own WiFi hotspot and serves the UI to a phone. No internet, no
cloud, nothing to configure in a stranger's driveway.

## Why a battery bank rather than the car

The single most common way a Pi-in-car project dies is an unclean shutdown corrupting
the SD card, followed by the automotive power stage needed to prevent it. A USB-C power
bank removes both problems, and adds a third benefit: a thorough scan takes several
minutes with the engine off, and running it off the seller's battery is a poor way to
begin a negotiation.

The CAN transceiver still needs a ground reference to the vehicle, which it gets
through OBD-II pin 4 or 5. The Pi never needs pin 16.

## Order of work

```
1. sudo ./deploy/setup-can.sh          # CAN FD HAT overlay, then reboot
2. sudo ./deploy/setup-service.sh      # install and enable the carpi service
3. sudo ./deploy/setup-hotspot.sh      # bring up the access point
4. sudo ./deploy/setup-readonly.sh     # last: makes the root filesystem read-only
```

Every script takes `--dry-run`, which prints what it would do and changes nothing. Run
each that way first.

Do the read-only step **last**. It is the one that makes the unit robust against having
its power yanked, and also the one that makes further changes awkward.

## 1. CAN interface

`setup-can.sh` adds the `mcp251xfd` overlay to `/boot/firmware/config.txt` and installs
a systemd unit that brings `can0` up at boot at 500 kbit/s.

Two things worth knowing before you wire anything:

- **Leave the 120 Ω termination jumper OFF.** The vehicle bus is already terminated at
  both ends. A third terminator causes errors that look like a broken adapter.
- **Ignition ON, not accessory.** Many modules stay asleep in accessory mode and will
  not answer at all.

After rebooting, confirm the interface exists and the bus is healthy *before* letting
the tool transmit:

```bash
ip -details link show can0
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
candump can0          # expect error-free traffic from the vehicle
```

Listen-only cannot transmit, so it cannot disturb the bus. Only once you see clean
traffic should you bring the interface up normally and scan.

If `candump` shows nothing: wrong bitrate (try 250000), CAN_H and CAN_L swapped, or the
ignition is not fully on. If it shows a flood of error frames, suspect the terminator.

## 2. The service

`setup-service.sh` installs `carpi.service`, which runs the server as an unprivileged
`carpi` user. It does **not** run as root: nothing car-pi does needs it, since
SocketCAN access is granted by the interface already being up.

## 3. Hotspot

`setup-hotspot.sh` creates a NetworkManager access point on the built-in WiFi. Defaults:

| | |
|---|---|
| SSID | `carpi` |
| Password | set with `--password`, minimum 8 characters |
| Address | `10.42.0.1`, so the UI is at `http://10.42.0.1:8080/` |

Override with `--ssid` and `--password`. There is no default password on purpose: a
shipped default is a shipped vulnerability, and this one would sit in a car park with
the SSID broadcasting.

Note the tradeoff — using the built-in WiFi as an access point means the Pi cannot also
be on your home network. For development, skip this step and reach the unit over
Ethernet or your existing WiFi instead.

## 4. Read-only root

`setup-readonly.sh` switches the root filesystem to an overlay held in RAM, so writes go
to memory and are discarded at reboot. After this, pulling the power is safe, which is
the whole point for something powered by a battery bank in a bag.

Consequences to be aware of:

- Changes to the system do not survive a reboot. To make one, run
  `sudo ./deploy/setup-readonly.sh --disable`, reboot, change, and re-enable.
- Scan history lives in memory anyway (see `carpi.server.jobs`), so nothing is lost
  that was not already going to be. Download reports you want to keep.

## Security

The server has no authentication. It is read-only, on its own hotspot, and a login
would make a tool used one-handed in a driveway materially worse to use.

**That reasoning stops the moment writing to a vehicle becomes possible.** Whoever
implements coding must put authentication in front of it first. The failure mode changes
from "somebody on your hotspot reads your fuel trims" to "somebody on your hotspot
reconfigures your ABS".
