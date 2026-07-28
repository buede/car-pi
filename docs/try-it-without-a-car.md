# Try it without a car

**For:** anyone who wants to see car-pi work before committing to anything.
**You need:** podman or docker. No car, no Raspberry Pi, no CAN hardware.
**Time:** about five minutes, most of it waiting for one image to build.

car-pi ships a virtual car. It answers real diagnostic requests over a real transport
stack, so everything above the wiring behaves exactly as it would in a driveway.

## Run it

```bash
git clone https://github.com/buede/car-pi
cd car-pi
./dev demo
```

You should see an inspection report for a simulated car, worst finding first. The first run
builds a container image and takes a few minutes. Later runs are quick.

That car is the `recently-cleared` scenario: a seller wiped the fault codes shortly before
the viewing. The report should point out that the self-tests have not re-run, and that a
permanent fault code is still stored.

## Try the other cars

```bash
./dev demo mileage-tampered
```

You should see a mileage disagreement between two modules.

To list them all:

```bash
carpi scenarios
```

There are seven, including a healthy control case. Each one names the findings it expects.

## See the phone interface

```bash
./dev serve
```

Open `http://localhost:8080`. You should see three tabs: Inspect, Live and History. Press
Ctrl-C to stop.

This is the interface you would use from a phone at the car.

## Or, if you already have Python

You do not need the container. Python 3.11 or newer:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
carpi demo --scenario recently-cleared
```

The container path exists so you do not have to do this. Either is fine.

## Two terminals, if you want a real transport in between

One terminal serves a simulated car. The other scans it over the network.

```bash
carpi sim                          # leave this running
carpi scan --transport udp         # in a second terminal
```

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Ready to check a real car? → [inspect a car](inspect-a-car.md)
- Want to know what it finds and why it matters? → [what it can find](what-it-can-find.md)
- Want to help? → [CONTRIBUTING.md](../CONTRIBUTING.md)

## Words used here

Scenario, module, permanent fault code — see the [glossary](glossary.md).
