# It did not work

**For:** anyone whose scan, board or test suite is not behaving.
**You need:** to know which of the sections below matches what you saw.
**Time:** most of these are a two-minute check.

Find your symptom. Each one lists the likely causes in the order worth checking.

**On this page**
- At the car
- On a hand-built board
- Running the tests
- Using the interface

## At the car

### `candump` shows nothing at all

1. **The bitrate is wrong.** Try `250000` instead of `500000`.
2. **CAN_H and CAN_L are swapped.** Pin 6 is CAN_H, pin 14 is CAN_L.
3. **The ignition is not fully on.** Accessory mode is not enough. Many modules stay asleep.

### `candump` shows a flood of error frames

**You fitted a terminator.** Remove it, or move the 120 Ω jumper to off.

The car's bus is already terminated at both ends. A third terminator breaks it.

### The scan runs but finds only a few modules

This is normal. Generic OBD-II reaches eight modules by design.

To reach the rest:

```bash
carpi uds discover
```

You should see modules outside the OBD-II address range, including the instrument cluster.

### `carpi vag` commands find nothing on an older VW or Audi

Check the car's age. TP2.0 applies to roughly 2001–2010. Newer cars use UDS instead, so use
`carpi uds` commands. See [older VW and Audi](older-vw-audi.md).

### Modules answer, then stop answering partway through

The battery is probably sagging. A thorough scan takes several minutes with the engine off.

Run the unit from its own battery bank, never the car. See
[build the field unit](build-the-field-unit.md).

## On a hand-built board

### `carpi bench loopback` returns nothing

1. **The crystal frequency does not match your overlay.** This is the most common cause by a
   wide margin. Check the `oscillator=` value against the crystal actually fitted.
2. **The SPI wiring is wrong.** Recheck against the table in
   [build the CAN interface](build-the-can-interface.md).

A loopback pass tells you nothing about the transceiver, because in loopback no signal
reaches its pins.

### Loopback passes but `carpi bench obd` fails

The fault is at the transceiver end, not the controller. Check the transceiver's supply
voltage, its `RS` pin, and the wiring between the two chips.

### The board works on the bench and fails in the car

Check the decoupling capacitors. 100 nF from supply to ground at **each** chip, as close to
the pins as you can get. Missing decoupling behaves exactly like this.

## Running the tests

### `./dev lint` fails on formatting

Run the fixer and commit the result:

```bash
./dev fmt
```

Continuous integration reports formatting problems but does not fix them.

### The SocketCAN tests skip instead of running

The `vcan` kernel module is missing. On macOS or Windows it must exist inside your container
runtime's virtual machine, and usually does not.

```bash
./dev socketcan
```

That command prints the exact fix for podman's default machine. CI runs these tests on every
push against a Linux runner, so skipping them locally costs no coverage.

### The first `./dev` command takes minutes

It is building the container image. This happens once.

## Using the interface

### The interface refuses a second inspection

This is deliberate, not a bug. One conversation at a time. See
[limits and safety](limits-and-safety.md).

Wait for the running inspection to finish.

### The report says "not assessed" instead of pass or fail

The car did not answer that question, so car-pi will not guess.

A question the car would not answer has not been answered favourably. This is intended
behaviour and the most important rule in the report engine.

### My scan history disappeared

Scans are kept in memory only, and the unit's root filesystem is read-only by design.
Download anything you want to keep.

## Next

- Building the board? → [build the CAN interface](build-the-can-interface.md)
- Setting up the Pi? → [build the field unit](build-the-field-unit.md)
- Need a command? → [command reference](commands.md)

## Words used here

Bitrate, terminator, vcan, transceiver, SPI — see the [glossary](glossary.md).
