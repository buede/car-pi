# car-pi

Plug a Raspberry Pi into a car's OBD-II port. Get a report on the car's real condition and
history — including whether someone cleared the fault codes shortly before you arrived to
look at it.

An open alternative to the proprietary tools (VCDS, BimmerCode, Carly, Autel), with the
definition database open and community-editable.

> **Pre-alpha.** Reading works. The write path exists but has never run on a real vehicle.
> See [limits and safety](docs/limits-and-safety.md) before you trust it with anything.

## Start here

Pick the row that matches you. The full list of documents is in [`docs/`](docs/README.md).

| I want to… | Read |
|---|---|
| See it work now, with no car and no hardware | [try it without a car](docs/try-it-without-a-car.md) |
| Check a car I am thinking of buying | [inspect a car](docs/inspect-a-car.md) |
| Know what it can and cannot find | [what it can find](docs/what-it-can-find.md) |
| Know what is unfinished, unsafe, or illegal | [limits and safety](docs/limits-and-safety.md) |
| Buy or build the hardware | [what to buy](docs/what-to-buy.md) |
| Add my car's data, or send a pull request | [CONTRIBUTING.md](CONTRIBUTING.md) |

## See it work in one minute

You need podman or docker. Nothing else — no car, no Raspberry Pi.

```bash
git clone https://github.com/buede/car-pi
cd car-pi
./dev demo
```

You should see an inspection report for a simulated car, worst finding first. The first run
builds a container image, so it takes a few minutes.

At a real car, `carpi guide` asks questions instead of taking flags, and checks each step.

## What it can tell you

- **Permanent fault codes** that no scan tool can erase, so they survive a wipe.
- **Whether the codes were cleared recently** — the signature of a seller tidying up.
- **The odometer, compared across modules.** Tampering usually rewrites only the cluster.
- **Self-test results with numbers**, so you see failures that are coming, not just arrived.
- **Fuel trims**, which reveal air leaks and worn sensors before they store a code.

More detail, and what it cannot see: [what it can find](docs/what-it-can-find.md).

## Why this exists

The interesting part of a diagnostics tool is not the protocol code — that is a few thousand
lines of well-documented standards work. It is the **definition database**: which data
identifier exists on which module, what the bytes mean, and which coding values are safe.
Proprietary vendors keep that database closed, and that is the entire product.

car-pi is therefore a thin generic engine plus a large declarative database in
[`src/carpi/defs/`](src/carpi/defs/). Adding support for a new car means writing YAML, not
Python.

The vehicle database ships nearly empty on purpose. A wrong odometer identifier returns
plausible bytes that decode to a plausible mileage, and somebody buys a car on the strength
of it. Nothing enters it unconfirmed — see
[contribute vehicle data](docs/contribute-vehicle-data.md).

## What it will not do

- **It will not clear your fault codes.** That destroys the evidence an inspection depends on.
- **It cannot write to a car at all**, except through `carpi coding`, which is quarantined,
  refuses safety-critical modules, and is not reachable over the network.
- **Emissions-defeat modifications are out of scope** and will not be added.

## All the documentation

**Start using it**

- [try it without a car](docs/try-it-without-a-car.md) — see it work in five minutes
- [inspect a car](docs/inspect-a-car.md) — the procedure at the car, in order
- [what it can find](docs/what-it-can-find.md) — what you learn, and why it matters
- [troubleshooting](docs/troubleshooting.md) — symptom, cause, fix

**Hardware**

- [what to buy](docs/what-to-buy.md) — buy a HAT, or build one
- [build the CAN interface](docs/build-the-can-interface.md) — the hand-built board
- [bring up a new board](docs/bring-up-a-new-board.md) — prove it works before a car sees it
- [build the field unit](docs/build-the-field-unit.md) — the Raspberry Pi setup

**Going further**

- [older VW and Audi cars](docs/older-vw-audi.md) — roughly 2001–2010, which work differently
- [coding](docs/coding.md) — the one path that writes to a car
- [limits and safety](docs/limits-and-safety.md) — what is unproven, and the law

**Reference**

- [command reference](docs/commands.md) — all 26 commands
- [glossary](docs/glossary.md) — DTC, PID, DID and the rest
- [definition files](docs/definition-files.md) — the database formats
- [contribute vehicle data](docs/contribute-vehicle-data.md) — turning a car into a definition

## Contributing

The most valuable contribution is data, not code: a scan from a car whose behaviour you can
verify. You do not need to write any Python.

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Licence

Code is **GPL-3.0-or-later**. The definition database is **CC-BY-SA-4.0**.

Copyleft is deliberate — a community-built definition database should not be absorbable into
a closed product.
