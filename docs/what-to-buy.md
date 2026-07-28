# What to buy

**For:** anyone about to order parts.
**You need:** to know roughly how old the car is.
**Time:** about five minutes to decide.

## Decide first: buy a HAT, or build one

The answer depends on one thing — whether the car uses CAN FD. Cars from roughly 2019
onward do. Older cars do not.

| Your car | What to get | Cost |
|---|---|---|
| Roughly 2019 or newer | The ready-made CAN FD HAT below | about €100 total |
| Older than that | Build the interface instead | about €20 total |

On an older car, a CAN FD HAT is paying for a feature the car cannot use. A hand-built
interface is fully adequate and needs no custom software.

If you want to build one, go to [build the CAN interface](build-the-can-interface.md).
Everything below is the ready-made route.

## The ready-made unit

| Item | Choice | Notes |
|---|---|---|
| Single-board computer | Raspberry Pi 4 | |
| CAN interface | Waveshare 2-CH CAN FD HAT | Two MCP2518FD chips. The `mcp251xfd` driver is mainline |
| Power | USB-C power bank | Not the car. See below |
| Cable | OBD-II (J1962) male pigtail | CAN_H is pin 6, CAN_L is pin 14, ground is pin 4 or 5 |

The Waveshare HAT uses screw terminals rather than a DB9 socket, which removes a whole
class of pinout mistakes.

## Power it from a battery bank, not the car

This matters more than it sounds.

- It removes the need for automotive transient protection.
- It removes SD-card corruption when the ignition is cycled.
- It avoids draining a stranger's battery during a scan that takes several minutes.

The transceiver still needs a ground reference to the car, which it gets through OBD-II pin
4 or 5. The Pi never needs pin 16.

> **Do not:** connect OBD-II pin 16, the +12 V line. It is an unnecessary path from the car
> into your electronics, and it is what turns a wiring mistake into a dead Pi.

## Consider a second CAN node

A second cheap interface, about €5, lets you test the whole stack on a bench before it
touches a car. `carpi bench` runs the simulator on one interface and the client on the
other.

This is worth it. See [build the CAN interface](build-the-can-interface.md).

## Next

- Building your own interface? → [build the CAN interface](build-the-can-interface.md)
- Parts arrived? → [build the field unit](build-the-field-unit.md)
- Just want to try the software? → [try it without a car](try-it-without-a-car.md)

## Words used here

CAN FD, transceiver, J1962, HAT — see the [glossary](glossary.md).
