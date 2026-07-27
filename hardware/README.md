# Building the interface and the harness

> **Nothing here has been built and tested yet.** It is drawn from the MCP2515 and
> SN65HVD230 datasheets and the Raspberry Pi `mcp251x` overlay documentation. The
> component choices and the reasoning are sound; the specific wiring should be checked
> against the datasheets before you solder, and the bring-up sequence below is designed to
> catch a mistake before it reaches a car. Corrections very welcome.

A hand-built CAN interface costs about €5 and is *fully adequate* for a 2006 Passat.
CAN FD did not exist in 2006, so the CAN FD HAT that would otherwise be worth buying is
paying for a feature that car cannot use.

## Why these parts

| Part | Why this one |
|---|---|
| **MCP2515** | CAN 2.0B controller over SPI. Available in **DIP-18**, so it breadboards and hand-solders easily. Driven by the mainline `mcp251x` kernel driver, so the interface appears as a normal SocketCAN `can0` with no custom software. |
| **SN65HVD230** | CAN transceiver that runs at **3.3 V**, which is what makes this clean: controller and transceiver both at 3.3 V means no level shifting anywhere. SOIC-8, or buy a breakout. |
| 16 MHz crystal + 2 × 22 pF | MCP2515 clock. See the warning below — this is the single most common way a hand-built board fails. |
| PESD1CAN | Transient protection across CAN_H/CAN_L. One part, and it is in a car. |
| — | **No 120 Ω terminator.** The vehicle bus is already terminated at both ends. |

### Do not buy the €2 blue MCP2515 module

The ubiquitous cheap module carries an **MCP2551** transceiver, which needs at least
4.5 V. That leaves two bad options:

- Power it at 5 V, and its SPI lines put 5 V into the Pi's 3.3 V GPIO.
- Power it at 3.3 V, and the transceiver drives the bus marginally instead of failing
  outright — which is worse, because it half-works and you chase it for an evening.

Building it with a 3.3 V transceiver from the start is genuinely less work than fixing
one of those.

### The crystal will bite you

**The crystal frequency must match the `oscillator=` value in your overlay.** Get it
wrong and the interface comes up perfectly, then silently misreads every frame on the
bus, because every bit time is scaled wrong. Cheap boards frequently ship 8 MHz while
most tutorials assume 16 MHz.

If `carpi bench loopback` fails on a board that looks correctly wired, check this first.

## Wiring: MCP2515 to the Pi

SPI0, plus one GPIO for the interrupt. Pin numbers are the 40-pin header's physical
positions.

| MCP2515 | Pi header | Pi signal |
|---|---|---|
| VDD (18) | 1 | 3V3 |
| VSS (9) | 6 | GND |
| SI (14) | 19 | SPI0 MOSI (GPIO 10) |
| SO (15) | 21 | SPI0 MISO (GPIO 9) |
| SCK (13) | 23 | SPI0 SCLK (GPIO 11) |
| CS (16) | 24 | SPI0 CE0 (GPIO 8) |
| INT (12) | 22 | GPIO 25 |
| RESET (17) | — | 3V3 via 10 kΩ (or tie to 3V3) |
| OSC2 (7) / OSC1 (8) | — | 16 MHz crystal across both, 22 pF from each leg to GND |
| TXCAN (1) | — | SN65HVD230 D (1) |
| RXCAN (2) | — | SN65HVD230 R (4) |

## Wiring: SN65HVD230

| SN65HVD230 | Connects to |
|---|---|
| D (1) | MCP2515 TXCAN |
| GND (2) | Ground |
| VCC (3) | 3V3 |
| R (4) | MCP2515 RXCAN |
| Vref (5) | leave open |
| CANL (6) | OBD-II pin 14, via PESD1CAN |
| CANH (7) | OBD-II pin 6, via PESD1CAN |
| RS (8) | GND through 10 kΩ — selects high-speed mode |

Decoupling: 100 nF from VDD to GND at **each** chip, as close to the pins as you can get.
Skipping these is how a board works on the bench and fails in a car.

## The harness

For a 2006 Passat you need three wires. Buy the J1962 male connector as a **pre-made
pigtail** (€5–8) — moulding your own is the one part of this not worth the effort.

| OBD-II pin | Signal | Goes to |
|---|---|---|
| 6 | CAN_H | SN65HVD230 CANH |
| 14 | CAN_L | SN65HVD230 CANL |
| 4 or 5 | Ground | board ground |

**Do not connect pin 16 (+12 V).** With the Pi on a USB battery bank it is an
unnecessary path from the car into your electronics, and it is what turns a wiring
mistake into a dead Pi.

Use a **twisted pair** for CAN_H and CAN_L — a pair pulled out of Cat5 is fine. CAN is
differential and the twist is most of why it tolerates a noisy engine bay.

## Configure the Pi

Add to `/boot/firmware/config.txt`, or let `deploy/setup-can.sh` do it:

```
dtparam=spi=on
dtoverlay=mcp2515-can0,oscillator=16000000,interrupt=25
```

Reboot, then:

```bash
ip -details link show can0
```

## Bring-up, in this order

Each step narrows down where a fault is, so do not skip ahead.

**1. Loopback — does the controller and the SPI wiring work?**

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 loopback on
sudo ip link set can0 up
carpi bench loopback --interface can0
```

Nothing comes back → the crystal frequency or the SPI wiring. In loopback nothing
reaches the transceiver, so a pass here says nothing about the transceiver end.

**2. Two-node bench — does the whole stack work over real silicon?**

This is worth the €5 for a second node. Wire two interfaces together, CAN_H to CAN_H and
CAN_L to CAN_L, with **a 120 Ω resistor across the pair at each end** — the one place a
terminator does belong.

```bash
carpi bench obd  --responder can1 --tester can0
carpi bench tp20 --responder can1 --tester can0
```

**The `tp20` bench is the most valuable test in this project.** TP2.0 is
connection-oriented, negotiates timing parameters and needs a keepalive, and both sides of
car-pi's implementation were written from the same specification by the same author. The
existing test suite proves the two agree; only real controllers at real bit timings prove
the timing is right. If TP2.0 has a timing bug, this is where it surfaces — on a bench,
not in a car park.

**3. Listen-only on the car — is the bus what you think it is?**

Listen-only cannot transmit, so it cannot disturb a vehicle. Do this before anything else
touches the Passat.

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
candump can0          # ignition ON, not accessory
```

- Nothing at all → wrong bitrate (try 250000), CAN_H and CAN_L swapped, or ignition not
  fully on.
- A flood of error frames → you fitted a terminator. Remove it.

**4. Then, and only then**, bring the interface up normally and scan:

```bash
sudo systemctl restart carpi-can
carpi scan --transport socketcan --channel can0
carpi vag modules --transport socketcan --channel can0
```

## Isolation, if you want it

Nothing above is galvanically isolated. With the Pi on a battery bank its only connection
to the car is through the transceiver's ground, which avoids most trouble. Two cases where
it is worth more:

- Plugging the Pi into a laptop's USB *while* connected to the car creates a ground path
  through both. Avoid it, or isolate.
- If you eventually power the Pi from the car, isolation becomes worth doing properly.

An **ISO1050** isolated transceiver drops into the SN65HVD230's place at the cost of a
separate isolated 5 V supply. Not needed for a battery-powered bench tool.

## The alternative: a Pico as a USB-CAN adapter

You already own a Pico, and it can be a complete CAN interface with **no controller
chip** — [`can2040`](https://github.com/KevinOConnor/can2040) implements CAN 2.0B in
software on the RP2040's PIO, so a Pico plus a transceiver is the whole bill of
materials.

To have it appear as a normal SocketCAN `can0` on the Pi, flash firmware that speaks the
**gs_usb** protocol, which the Linux kernel, python-can and car-pi all already support:

| Option | Notes |
|---|---|
| **Klipper in USB-to-CAN-bridge mode** | Compiles for RP2040/RP2350 and presents a standard `gs_usb` adapter. The most trodden path, since the 3D-printing world relies on it. |
| [**CANnectivity**](https://cannectivity.org/) | Zephyr-based, purpose-built USB-to-CAN firmware, gs_usb compatible. |
| [**candleLight_fw**](https://github.com/candle-usb/candleLight_fw) | The original gs_usb firmware. Targets STM32, so it is the reference rather than a drop-in for RP2040. |
| [**rp2040-can-mcp2515**](https://github.com/trnila/rp2040-can-mcp2515) | RP2040 gs_usb firmware that drives an *external* MCP2515 rather than using PIO. |

Wiring is just power, ground, and two pins to the transceiver — one GPIO for CAN TX and
one for CAN RX.

```bash
# after flashing, on the Pi:
ip link show can0          # gs_usb should have created it
sudo ip link set can0 up type can bitrate 500000
```

Trade-offs against the MCP2515 route: fewer parts and it works from a laptop over USB
too, but you depend on somebody else's firmware and you have a USB connector in a car
rather than a soldered header. `can2040` is CAN 2.0 only, which for this car does not
matter.

The Pico is also the natural home for the ignition-sense and clean-shutdown watchdog if
you ever move off the battery bank.

## Bill of materials

| Item | ~€ |
|---|---|
| MCP2515, DIP-18 | 2 |
| SN65HVD230 (or breakout) | 2 |
| 16 MHz crystal + 2 × 22 pF | 1 |
| PESD1CAN | 1 |
| 100 nF × 2, 10 kΩ × 2 | <1 |
| Perfboard, header, wire | 3 |
| OBD-II J1962 male pigtail | 6 |
| **Second node for the bench** (Pico + transceiver) | 5 |
| **Total** | **~€20** |

Against €30–65 for a CAN FD HAT whose CAN FD you cannot use on this car.
