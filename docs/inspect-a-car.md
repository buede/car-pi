# Inspect a car

**For:** someone checking a used car before buying it.
**You need:** the portable unit built, and the seller's permission.
**Time:** about 20 minutes at the car, plus the test drive.

> **Careful:** ask before you plug into a car you do not own. Reads are non-invasive, but
> it is someone else's property.

**On this page**
- Before you go
- Two things to remember at the car
- At the car, in order
- Read the report on your phone
- During the test drive
- What the report is telling you

## Before you go

- Charge the battery bank. A thorough scan takes several minutes.
- Find out the advertised mileage. You will pass it to the scan so it can be cross-checked.
- Check the unit boots and serves its interface. Do this at home, not in a car park.

## Two things to remember at the car

These two mistakes account for most failed inspections.

1. **Leave the 120 Ω termination jumper OFF.** The car's bus is already terminated at both
   ends. A third terminator causes errors that look like a broken adapter.
2. **Turn the ignition ON, not accessory.** Many modules stay asleep in accessory mode and
   will not answer at all.

## At the car, in order

Do not skip step 2. It is the step that tells you the bus is what you think it is, before
your unit transmits anything.

**1. Plug in and turn the ignition on.**

The socket is under the dashboard on the driver's side. Ignition on, engine off.

**2. Check the bus is healthy, without transmitting.**

Listen-only mode physically cannot transmit, so it cannot disturb the car.

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
candump can0
```

You should see a steady stream of traffic and no error frames. Press Ctrl-C to stop.

If you see nothing at all, or a flood of error frames, stop and see
[troubleshooting](troubleshooting.md).

**3. Bring the interface up normally.**

```bash
sudo systemctl restart carpi-can
```

**4. Scan, telling it the advertised mileage.**

```bash
carpi scan --channel can0 --odometer 145000
```

You should see a report ordered worst finding first. Add `--format json -o car.json` to keep
a copy.

## Read the report on your phone

The unit serves a web interface over its own hotspot. Join the hotspot, then open
`http://10.42.0.1:8080/`.

It shows the report worst finding first, streams live values, and lets you download the raw
data. It works with no internet, because there is none in a car park.

**One inspection at a time.** A second scan, or live values during a scan, is refused rather
than queued. This is deliberate. See [limits and safety](limits-and-safety.md).

## During the test drive

Some faults only appear in motion. Fuel trims under load reveal air leaks that idle does
not.

Open the **Live** tab and watch while somebody else drives. Do not drive and read.

## What the report is telling you

Findings are ordered by severity, worst first. Each one says what it means and what to do
next, not just which sensor is out of range.

Every finding carries a **confidence** level, and the report tells you which one it relied
on:

| Level | What it means |
|---|---|
| `official` | Taken from a published standard |
| `verified` | Confirmed against a real car whose true state was known |
| `community` | Contributed but unconfirmed. Treat with care |

**A question the car would not answer is reported as "not assessed".** It is never reported
as a pass. Silence is not a clean bill of health.

Scans are kept in memory and are lost when the unit powers down. Download anything you want
to keep.

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Older VW or Audi? → [older VW and Audi cars](older-vw-audi.md)
- What does each finding actually mean? → [what it can find](what-it-can-find.md)
- What is it not telling me? → [limits and safety](limits-and-safety.md)

## Words used here

Bus, module, fuel trim, bitrate — see the [glossary](glossary.md).
