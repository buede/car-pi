# Contribute vehicle data

**For:** anyone with a car and some patience. No Python needed.
**You need:** car-pi working on a real car, and a way to verify what you find.
**Time:** an evening for a first sweep, longer to confirm it.

**This is the highest-value thing anyone can add to car-pi.**

**On this page**
- Why this database is nearly empty
- Where the file goes
- Step 1: find what a module holds
- Step 2: prove which identifier it is
- What is worth chasing first
- Before you share a scan

> **Careful:** nothing car-pi generates is a finding. A drafted definition is a list of
> identifiers that exist, with no claim about what any of them hold.

## Why this database is nearly empty

Manufacturer data identifiers cannot be verified without the car in front of you.

A wrong odometer identifier does not fail loudly. It returns four plausible bytes that decode
to a plausible mileage, and somebody buys a car on the strength of it.

Every proprietary tool's value is its database. The only way this one becomes worth trusting
is if nothing enters it that was not confirmed against a real car.

So car-pi ships the engine and the scanner, not a database of guesses. The shipped set
contains one profile describing the built-in simulator, marked `fictional: true` so it can
never match a real vehicle.

## Where the file goes

`src/carpi/defs/vehicles/<make>/<platform>.yaml`, validated against the schema in
`src/carpi/defs/schema/vehicle.schema.json`.

The file format is described in [definition files](definition-files.md).

To start from a sweep rather than an empty file:

```bash
carpi defs draft cluster.json --id vw-mqb --make Volkswagen --platform MQB -o mqb.yaml
```

You should see a file listing every identifier the sweep found, each marked `TODO`.

Every read it writes is `community` confidence with a `TODO` name, because a sweep proves an
identifier exists and nothing at all about what it holds. Naming one `odometer_km` is a
claim, and only you, with the car, can make it.

## Step 1: find what a module holds

Generic OBD-II only exposes the emissions modules. The odometer is elsewhere, so find the
other modules first.

```bash
carpi uds discover --channel can0
```

You should see modules that answered, including ones outside the OBD-II address range.

Then ask one what it holds. Start with a narrow range — a full sweep is 65,536 requests and
takes a long time.

```bash
carpi uds scan-dids --channel can0 --request-id 0x714 --response-id 0x77E --out cluster.json
```

**Three outcomes all matter:**

| Outcome | What it means |
|---|---|
| Data came back | The identifier exists and is readable |
| `protected` | The identifier exists and the manufacturer locked it. Often the interesting ones |
| Nothing there | No such identifier |

> **Careful:** some manufacturers log that a tester talked to a module. This is read-only, but
> it is not always invisible.

## Step 2: prove which identifier it is

**Confirming an identifier cannot be automated.** That is the part that makes a
contribution trustworthy. Comparing the sweeps can be, and is.

1. **Record a sweep.** Save the output.
2. **Change one thing about the car.** Drive a kilometre. Turn on the lights. Let the fuel
   level drop.
3. **Sweep again and compare.** The identifier that moved by the right amount is your
   candidate.
4. **Confirm it against a second car** of the same platform, whose true state you know
   independently.

Step 4 is not optional. One car can agree with a wrong guess by coincidence. Two rarely do.

For step 3, tell car-pi how much the thing you changed moved by:

```bash
carpi defs compare before.json after.json --expect-delta 1.2
```

You should see a ranked list, best candidate first, with the units each identifier would
have to be counting in for it to be the one you want.

That ranking is arithmetic, not a conclusion. An identifier that moved by the right amount
once is a candidate. Step 4 is what turns it into a fact.

To check a single candidate quickly without a full sweep:

```bash
carpi uds read --channel can0 --request-id 0x714 --response-id 0x77E --did 0xF190
```

Only after step 4 is a read `confidence: verified`. Before that it is `community`, and the
report tells the reader so. Marking something `community` is not a failure — it is how a
plausible guess gets into the tool without being passed off as fact.

## What is worth chasing first

Ordered by how much it matters when buying a used car.

1. **Odometer, from every module that holds one.** Tampering usually rewrites the instrument
   cluster only. Give the read the id `odometer_km` in each module and the cross-module
   comparison happens automatically.
2. **Diesel particulate filter soot load and regeneration count.** An expensive failure a
   test drive cannot reveal.
3. **Hybrid battery state of health, and per-block voltages.** On a used hybrid this
   dominates the car's value.
4. **Transmission adaptation values and clutch wear**, on dual-clutch gearboxes.
5. **Battery state of health**, where the module tracks it.

## Before you share a scan

> **Do not:** attach a raw sweep to a public issue. It contains the VIN, which identifies one
> physical car and, through it, a person.

The safe way is to share a **contribution**, which carries shapes and no contents:

```bash
carpi defs contribute cluster.json car.json
```

You should see a summary, a written file, and a link that opens a prefilled issue. Nothing is
uploaded by that command.

A contribution keeps the first eight characters of the VIN, which identify the platform, the
address of each module that answered, and which identifiers exist with their length and type.
It keeps **no values at all**.

Values are dropped rather than scrubbed. Removing a VIN from a sweep still leaves the part
numbers, the serial numbers and the programming dates, and those together identify one car.
Dropping contents needs no judgement about which values happen to be sensitive.

Read the file before you attach it. Sharing it contributes to the database under
**CC-BY-SA-4.0**, which cannot be withdrawn afterwards.

> **Do not:** share a contribution from a car you do not own without asking the owner. It
> describes their car, and the licence is permanent.

If you do need to share a raw sweep, `--anonymise` removes the VIN and redacts any value
containing it:

```bash
carpi uds scan-dids --channel can0 --request-id 714/77E --anonymise --out cluster.json
```

That is weaker than a contribution, because it removes only the VIN.

`.gitignore` already excludes `scans/`, `*.candump`, `*.asc` and `*.blf` so raw logs are not
committed by accident.

## Next

- How do I write the file? → [definition files](definition-files.md)
- Sending a pull request? → [CONTRIBUTING.md](../CONTRIBUTING.md)
- What do these commands do? → [command reference](commands.md)

## Words used here

DID, VIN, module, confidence, DPF — see the [glossary](glossary.md).
