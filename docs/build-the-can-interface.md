# Build the CAN interface

**For:** anyone building their own interface instead of buying a HAT.
**You need:** soldering equipment, the parts listed at the bottom, and about €20.
**Time:** an evening to build, an hour to bring up.

> **Careful:** nothing here has been built and tested yet. It is drawn from the MCP2515 and
> SN65HVD230 datasheets and the Raspberry Pi overlay documentation. Check the wiring against
> the datasheets before you solder. Corrections are very welcome.

A hand-built interface costs about €5 and is fully adequate for any car older than about
2019. Those cars do not use CAN FD, so a CAN FD HAT would be paying for a feature the car
cannot use.

**On this page**
- Why these parts
- Two traps that catch most boards
- Wiring
- The harness
- Configure the Pi
- Test it before it touches a car
- The netlist
- Isolation
- A Pico as an alternative
- Bill of materials

## Why these parts

| Part | Why this one |
|---|---|
| **MCP2515** | CAN 2.0B controller over SPI. Available in DIP-18, so it breadboards and hand-solders easily. The mainline `mcp251x` kernel driver makes it appear as a normal SocketCAN `can0` with no custom software |
| **SN65HVD230** | A transceiver that runs at 3.3 V. Controller and transceiver both at 3.3 V means no level shifting anywhere |
| **16 MHz crystal, 2 × 22 pF** | The MCP2515's clock. See the traps below |
| **PESD1CAN** | Transient protection across CAN_H and CAN_L. One part, and it is going into a car |
| **No 120 Ω terminator** | The car's bus is already terminated at both ends |

## Two traps that catch most boards

### Do not buy the €2 blue MCP2515 module

The common cheap module carries an **MCP2551** transceiver, which needs at least 4.5 V. That
leaves two bad options:

- Power it at 5 V, and its SPI lines put 5 V into the Pi's 3.3 V pins.
- Power it at 3.3 V, and the transceiver drives the bus marginally instead of failing
  outright. This is worse, because it half-works and you chase it for an evening.

Building it with a 3.3 V transceiver from the start is less work than fixing one of those.

### The crystal frequency must match your overlay

**The crystal frequency must match the `oscillator=` value in your overlay.** Get this wrong
and the interface comes up perfectly, then silently misreads every frame on the bus, because
every bit time is scaled wrong.

Cheap boards frequently ship an 8 MHz crystal while most tutorials assume 16 MHz. Check what
is actually fitted.

This is the first thing to suspect if `carpi bench loopback` fails on a board that looks
correctly wired.

## Wiring

SPI0, plus one pin for the interrupt. Pin numbers are physical positions on the 40-pin
header.

| MCP2515 | Pi header | Pi signal |
|---|---|---|
| VDD (18) | 1 | 3V3 |
| VSS (9) | 6 | GND |
| SI (14) | 19 | SPI0 MOSI (GPIO 10) |
| SO (15) | 21 | SPI0 MISO (GPIO 9) |
| SCK (13) | 23 | SPI0 SCLK (GPIO 11) |
| CS (16) | 24 | SPI0 CE0 (GPIO 8) |
| INT (12) | 22 | GPIO 25 |
| RESET (17) | — | 3V3 via 10 kΩ, or tie to 3V3 |
| OSC1 (8) / OSC2 (7) | — | 16 MHz crystal across both, 22 pF from each leg to GND |
| TXCAN (1) | — | SN65HVD230 D (1) |
| RXCAN (2) | — | SN65HVD230 R (4) |

Then the transceiver:

| SN65HVD230 | Connects to |
|---|---|
| D (1) | MCP2515 TXCAN |
| GND (2) | Ground |
| VCC (3) | 3V3 |
| R (4) | MCP2515 RXCAN |
| Vref (5) | Leave open |
| CANL (6) | OBD-II pin 14, via PESD1CAN |
| CANH (7) | OBD-II pin 6, via PESD1CAN |
| RS (8) | GND through 10 kΩ, which selects high-speed mode |

Add 100 nF from supply to ground at **each** chip, as close to the pins as you can get.
Skipping these is how a board works on the bench and fails in a car.

## The harness

Buy the J1962 male connector as a **pre-made pigtail**, about €5 to €8. Moulding your own is
the one part of this not worth the effort.

| OBD-II pin | Signal | Goes to |
|---|---|---|
| 6 | CAN_H | SN65HVD230 CANH |
| 14 | CAN_L | SN65HVD230 CANL |
| 4 or 5 | Ground | Board ground |

> **Do not:** connect pin 16, the +12 V line. With the Pi on a battery bank it is an
> unnecessary path from the car into your electronics, and it is what turns a wiring mistake
> into a dead Pi.

Use a **twisted pair** for CAN_H and CAN_L. A pair pulled out of Cat5 cable is fine. CAN is
differential, and the twist is most of why it tolerates a noisy engine bay.

## Configure the Pi

Add these two lines to `/boot/firmware/config.txt`, or let `deploy/setup-can.sh` do it:

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

Change `oscillator=16000000` if your crystal is not 16 MHz. Reboot, then check the interface
exists:

```bash
ip -details link show can0
```

You should see a `can0` interface listed.

## Test it before it touches a car

Do not plug a newly built board into a vehicle. There is a four-step sequence that finds a
mistake on a bench instead, starting with a loopback test that needs no second node.

Go to [bring up a new board](bring-up-a-new-board.md).

## The netlist

`hardware/carpi-can.net` is a machine-readable netlist of the design above. Open it in KiCad
to lay out a board.

It is checked by `tests/test_hardware_netlist.py`, pin by pin, against the datasheets. That
test also asserts the netlist says it has never been built, and that OBD-II pin 16 is never
wired.

## Isolation

Nothing above is galvanically isolated. With the Pi on a battery bank, its only connection
to the car is the transceiver's ground, which avoids most trouble.

Two cases where isolation is worth more:

- Plugging the Pi into a laptop's USB **while** connected to the car creates a ground path
  through both. Avoid it, or isolate.
- If you eventually power the Pi from the car, isolate properly.

An **ISO1050** isolated transceiver drops into the SN65HVD230's place, at the cost of a
separate isolated 5 V supply. Not needed for a battery-powered tool.

## A Pico as an alternative

A Raspberry Pi Pico can be a complete CAN interface with **no controller chip**.
[`can2040`](https://github.com/KevinOConnor/can2040) implements CAN 2.0B in software on the
RP2040's programmable IO, so a Pico plus a transceiver is the whole bill of materials.

Flash firmware that speaks **gs_usb**, which the Linux kernel and car-pi already support, and
it appears as a normal `can0`:

| Firmware | Notes |
|---|---|
| **Klipper**, in USB-to-CAN-bridge mode | The most trodden path, because 3D printing relies on it |
| [**CANnectivity**](https://cannectivity.org/) | Purpose-built USB-to-CAN firmware, gs_usb compatible |

Two others exist but are poor fits:
[candleLight_fw](https://github.com/candle-usb/candleLight_fw) targets STM32, and
[rp2040-can-mcp2515](https://github.com/trnila/rp2040-can-mcp2515) drives an external
MCP2515, which defeats the point.

Wiring is power, ground, and two pins to the transceiver. Trade-offs against the MCP2515
route: fewer parts, and it works from a laptop over USB. Against that, you depend on somebody
else's firmware and you have a USB connector in a car rather than a soldered header.

## Bill of materials

| Item | About € |
|---|---|
| MCP2515, DIP-18 | 2 |
| SN65HVD230, or a breakout | 2 |
| 16 MHz crystal, 2 × 22 pF | 1 |
| PESD1CAN | 1 |
| 100 nF × 2, 10 kΩ × 2 | under 1 |
| Perfboard, header, wire | 3 |
| OBD-II J1962 male pigtail | 6 |
| Second node for the bench, a Pico plus transceiver | 5 |
| **Total** | **about 20** |

Compare that with €30 to €65 for a CAN FD HAT whose CAN FD an older car cannot use.

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Board soldered? → [bring up a new board](bring-up-a-new-board.md)
- Board working? → [build the field unit](build-the-field-unit.md)

## Words used here

CAN, CAN FD, transceiver, SPI, bitrate, terminator, J1962 — see the [glossary](glossary.md).
