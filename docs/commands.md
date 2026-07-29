# Command reference

**For:** anyone who knows what they want to do and needs the exact command.
**You need:** car-pi installed. See [try it without a car](try-it-without-a-car.md).
**Time:** a lookup.

Every command is listed here with what it does and one example. Options are **not** listed,
because they change. Run `carpi <command> --help` for those — that is always correct.

Add `-h` to anything. Add `-v` for more detail, repeated for more still.

**On this page**
- Start here if you are not sure
- Try it with no car
- Inspect a real car
- Reach more modules (UDS)
- Older VW and Audi (KWP2000)
- Change a setting
- Test your hardware
- The definition database

## Start here if you are not sure

One command asks questions instead of taking flags.

| Command | What it does |
|---|---|
| `carpi guide` | Walks through an inspection, checking each step |

```bash
carpi guide
```

You should see a short menu, then one question at a time.

It runs the commands below on your behalf, and prints each one as it goes. Nothing in it
writes to a vehicle.

## Try it with no car

None of these need a vehicle or any hardware.

| Command | What it does |
|---|---|
| `carpi scenarios` | Lists the simulated cars you can scan |
| `carpi demo` | Scans a simulated car and prints the report |
| `carpi serve --transport sim` | Serves the web interface against a simulated car |
| `carpi sim` | Runs a simulated car until you stop it, for another terminal to scan |

Scan a simulated car that had its codes wiped before the viewing:

```bash
carpi demo --scenario recently-cleared
```

You should see a report with a permanent fault code and a recent-code-clear finding.

## Inspect a real car

| Command | What it does |
|---|---|
| `carpi scan` | Scans the car and reports on it |
| `carpi serve` | Serves the report to a phone over the network |

Scan a car on `can0`, telling it the advertised mileage in kilometres so it can be
cross-checked:

```bash
carpi scan --transport socketcan --channel can0 --odometer 145000
```

You should see a report ordered worst finding first. Add `--format json -o car.json` to keep it.

Generic OBD-II reaches eight modules, and the instrument cluster is not one of them. Add
`--discover` to sweep for the rest and read each one's standardised identification:

```bash
carpi scan --channel can0 --discover
```

You should see the modules a generic tool never speaks to, with their part numbers and the
VIN each one holds. A module holding a different VIN came out of a different car. This
needs no definition file, and it adds most of a minute.

> **Careful:** check the bus is healthy before you transmit. See
> [inspect a car](inspect-a-car.md).

## Reach more modules (UDS)

Generic OBD-II reaches eight modules. These commands reach the rest, including the
instrument cluster that holds the odometer. All read-only.

| Command | What it does |
|---|---|
| `carpi uds discover` | Probes for every module that answers, including outside the OBD-II range |
| `carpi uds identify` | Reads one module's part number, serial and software dates |
| `carpi uds read` | Reads specific data identifiers from one module |
| `carpi uds dtcs` | Reads manufacturer fault codes, which generic OBD-II never shows |
| `carpi uds scan-dids` | Sweeps a module's identifiers to find out what it holds |

Find the modules, then ask one what it is:

```bash
carpi uds discover --transport sim
carpi uds identify --transport sim --request-id 0x714 --response-id 0x77E
```

Both work with `--transport sim`, so you can learn the commands before you touch a car.

## Older VW and Audi (KWP2000)

For roughly 2001–2010 VW, Audi, Škoda and SEAT. See [older VW and Audi](older-vw-audi.md).

| Command | What it does |
|---|---|
| `carpi vag modules` | Finds which modules the car has. The equivalent of a VCDS auto-scan |
| `carpi vag blocks` | Reads measuring blocks, the live values VCDS shows |
| `carpi vag read` | Reads one local identifier's raw bytes |

```bash
carpi vag modules --transport sim
```

You should see a list of modules with their logical addresses.

## Change a setting

**This is the only part of car-pi that writes to a car.** Read [coding](coding.md) first.

| Command | What it does |
|---|---|
| `carpi coding plan` | Shows what a change would do. **Writes nothing.** Always start here |
| `carpi coding apply` | Writes the change, archiving the old value first |
| `carpi coding list-restore-points` | Lists archived values, newest first |
| `carpi coding restore` | Puts a module back to an archived value |

Check a change before making it:

```bash
carpi coding plan --module 0x46 --value 0A1B2D
```

You should see a decoded before-and-after, and nothing should be written.

## Test your hardware

For a hand-built interface, before it goes near a car. See
[build the CAN interface](build-the-can-interface.md).

| Command | What it does |
|---|---|
| `carpi bench loopback` | Checks one interface can transmit and receive, with no second node |
| `carpi bench obd` | Runs the simulator on one interface and scans from another |
| `carpi bench tp20` | Runs the VAG simulator on one interface and drives TP2.0 from another |

```bash
carpi bench loopback --interface can0
```

A pass means the controller and its wiring work. It says nothing about the transceiver.

## The definition database

| Command | What it does |
|---|---|
| `carpi defs check` | Validates every definition file against its schema |
| `carpi defs facts` | Lists every fact the rules reference, for writing new rules |
| `carpi defs compare` | Finds which identifiers changed between two sweeps of a module |
| `carpi defs draft` | Turns sweeps into a starting-point definition file |
| `carpi defs contribute` | Reduces scans to something shareable, and offers it to the project |

```bash
carpi defs check
```

You should see no errors. This runs in CI on every push.

Find which identifier holds the odometer, by sweeping before and after driving 1.2 km:

```bash
carpi defs compare before.json after.json --expect-delta 1.2
```

You should see a ranked list, best candidate first, naming the units each identifier would
have to be counting in.

Turn a sweep into a file you can start editing:

```bash
carpi defs draft cluster.json --id vw-mqb --make Volkswagen --platform MQB -o mqb.yaml
```

You should see every identifier the sweep found, each marked `TODO` until you have proven
what it holds. See [contribute vehicle data](contribute-vehicle-data.md).

Offer what you learned back to the project:

```bash
carpi defs contribute car.json
```

You should see a link that opens a prefilled issue. Nothing is uploaded, and the file it
writes contains no values, no serial numbers and no VIN.

## Next

- What do these words mean? → [glossary](glossary.md)
- Working on the code? → [CONTRIBUTING.md](../CONTRIBUTING.md)
