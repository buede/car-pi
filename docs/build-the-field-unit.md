# Build the field unit

**For:** anyone turning a Raspberry Pi into the portable inspection unit.
**You need:** a Pi 4, a CAN interface, a USB power bank, and Raspberry Pi OS Bookworm.
**Time:** about an hour, including two reboots.

> **Careful:** none of this has been run on real hardware yet. It is written from the
> Raspberry Pi OS, NetworkManager and driver documentation, and it is deliberately
> conservative. Treat every step as a hypothesis until you have confirmed it. Corrections
> are very welcome.

**On this page**
- What you are building
- Why a battery bank, not the car
- Run these four, in this order
- Step 1: the CAN interface
- Step 2: the service
- Step 3: the hotspot
- Step 4: read-only root

## What you are building

A Pi in a case, powered from a USB battery bank, that brings up its own WiFi network and
serves the inspection interface to your phone.

No internet. No cloud. Nothing to configure in a stranger's driveway.

## Why a battery bank, not the car

The most common way a Pi-in-car project dies is an unclean shutdown corrupting the SD card,
followed by the automotive power stage needed to prevent it. A USB power bank removes both
problems.

It adds a third benefit. A thorough scan takes several minutes with the engine off, and
running that off the seller's battery is a poor way to begin a negotiation.

The CAN transceiver still needs a ground reference to the car, which it gets through OBD-II
pin 4 or 5. The Pi never needs pin 16.

## Run these four, in this order

```bash
sudo ./deploy/setup-can.sh          # then reboot
sudo ./deploy/setup-service.sh
sudo ./deploy/setup-hotspot.sh --password YOUR_PASSWORD
sudo ./deploy/setup-readonly.sh     # last
```

**Every script takes `--dry-run`, which prints what it would do and changes nothing.** Run
each that way first.

**Do the read-only step last.** It is the step that makes the unit survive having its power
yanked, and also the step that makes further changes awkward.

## Step 1: the CAN interface

`setup-can.sh` adds the driver overlay to `/boot/firmware/config.txt` and installs a service
that brings `can0` up at boot at 500 kbit/s.

```bash
sudo ./deploy/setup-can.sh --dry-run     # read this output first
sudo ./deploy/setup-can.sh
sudo reboot
```

After the reboot, confirm the interface exists:

```bash
ip -details link show can0
```

You should see `can0` listed.

Before letting the tool transmit on a real car, check the bus is healthy with a listen-only
capture. That procedure is in [inspect a car](inspect-a-car.md), steps 1 and 2.

## Step 2: the service

`setup-service.sh` installs the unit that runs the server.

```bash
sudo ./deploy/setup-service.sh
```

It runs as an unprivileged `carpi` user, **not** root. Nothing car-pi does needs root, since
SocketCAN access comes from the interface already being up.

## Step 3: the hotspot

`setup-hotspot.sh` creates a WiFi access point on the built-in adapter.

```bash
sudo ./deploy/setup-hotspot.sh --password YOUR_PASSWORD
```

| Setting | Default |
|---|---|
| Network name | `carpi`, override with `--ssid` |
| Password | None. You must set `--password`, minimum 8 characters |
| Address | `10.42.0.1`, so the interface is at `http://10.42.0.1:8080/` |

**There is no default password on purpose.** A shipped default is a shipped vulnerability,
and this one would be broadcasting its network name from a car park.

Note the trade-off: using the built-in WiFi as an access point means the Pi cannot also be on
your home network. For development, skip this step and reach the unit over Ethernet instead.

## Step 4: read-only root

`setup-readonly.sh` switches the root filesystem to an overlay held in RAM. Writes go to
memory and are discarded at reboot.

```bash
sudo ./deploy/setup-readonly.sh
sudo reboot
```

After this, pulling the power is safe. That is the whole point for something powered by a
battery bank in a bag.

Two consequences:

- **System changes no longer survive a reboot.** To make one, run
  `sudo ./deploy/setup-readonly.sh --disable`, reboot, make the change, then re-enable it and
  reboot again.
- **Scan history was already in memory only**, so nothing is lost that was not already going
  to be. Download any report you want to keep.

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Ready for a car? → [inspect a car](inspect-a-car.md)
- Note there is no password on the interface → [limits and safety](limits-and-safety.md)

## Words used here

SocketCAN, transceiver, bitrate, overlay — see the [glossary](glossary.md).
