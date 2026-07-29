# What it can find

**For:** anyone deciding whether this tells them more than a cheap dongle would.
**You need:** nothing. This page is background.
**Time:** about eight minutes.

**On this page**
- On any car since about 2008
- On modules that OBD-II cannot reach
- Why a cheap dongle misses most of this

## On any car since about 2008

Generic OBD-II is standardised and mandatory. It needs no authentication, and there is
nothing here a seller can refuse.

**Permanent fault codes.** No scan tool can clear these. Only the car's own module clears
them, and only after its self-tests pass again. They survive a wipe.

**A recent code wipe.** Three things together give it away:

1. Self-tests that have not finished running.
2. No stored faults at all.
3. A low distance since the codes were cleared.

A car that has genuinely been fine for months does not look like that. This is the single
most useful thing car-pi checks, because clearing codes before a viewing is common and
easy.

**Self-test results with numbers.** Mode `06` gives numeric headroom on catalyst
efficiency, misfire counters and the evaporative system. You see failures that are coming,
not only the ones that have arrived.

**Freeze frames.** The exact conditions recorded at the moment a fault was stored. Useful
for telling a one-off from a pattern.

**Fuel trims, at idle and at cruise.** Large corrections mean the engine is compensating
for something. Air leaks, a tired mass airflow sensor and worn injectors all show up here
before they store a fault code.

**A swapped or remapped engine module.** Mode `09` reports calibration IDs and calibration
verification numbers. A mismatch is worth a question.

## On modules that OBD-II cannot reach

OBD-II reaches eight modules, chosen by emissions regulators. The instrument cluster
holding the odometer is not one of them. Neither is the ABS controller or the body
electronics.

car-pi also speaks UDS, read-only, to reach the rest.

**Odometer, compared across modules.** This is the important one. Mileage tampering is
nearly always done by rewriting the instrument cluster. Every other module keeps the true
figure. Modules do not drift apart on their own, so a disagreement is close to proof.

> **Careful:** this one needs a definition file for your car, and the shipped database is
> nearly empty. No standard identifier holds the odometer. See
> [contribute vehicle data](contribute-vehicle-data.md).

**VIN, compared across modules.** A module holding a different car's VIN came out of a
different car. This needs no definition file, because the VIN identifier is standardised.
Add `--discover` to a scan and every module that answers is asked for it.

**Manufacturer fault codes.** Every module's own faults, which generic OBD-II never shows.

**Module identity and dates.** Serial numbers, part numbers, and the programming and
calibration dates. A cluster reprogrammed last month, on a car with high mileage, is worth
a question.

## Why a cheap dongle misses most of this

A €10 dongle reads generic OBD-II and stops there. That is a real limit, not a software
one: the modules worth asking about are not on the generic address range, and there is no
standard map of where they are.

car-pi finds them by probing:

```bash
carpi uds discover
```

You should see a list of modules that answered, including ones outside the OBD-II range.

Some modules ignore the default probe but answer a data request. To catch those:

```bash
carpi uds discover --probe read-vin
```

You should see any module the first sweep missed. Both probes are reads.

**Fault codes are explained without an internet connection.** Every code is shown with what
the standard says about it: which part of the car it concerns, and whether it is a code every
manufacturer uses identically or one only your make defines. A car park has no signal, so
looking it up later is not a plan.

## What this does not tell you

Plenty. Rust, accident repair, suspension wear and clutch condition are not on the bus. A
scan is one input to a decision, not the decision.

See [limits and safety](limits-and-safety.md) for what car-pi will not do, and what has
not been proven yet.

## Next

- Ready to do it? → [inspect a car](inspect-a-car.md)
- Want the exact commands? → [command reference](commands.md)
- Curious why the vehicle database is nearly empty? → [contribute vehicle data](contribute-vehicle-data.md)

## Words used here

DTC, PID, freeze frame, readiness monitor, Mode 06, UDS — see the [glossary](glossary.md).
