# Older VW and Audi cars

**For:** anyone inspecting a roughly 2001–2010 VW, Audi, Škoda or SEAT.
**You need:** car-pi installed, and a CAN interface if you are working on a real car.
**Time:** about five minutes to read.

> **Careful:** this has never been run on a real vehicle. See
> [limits and safety](limits-and-safety.md).

## Why these cars are different

These cars do not use UDS for manufacturer diagnostics. They use **KWP2000 over TP2.0**,
Volkswagen's own transport.

TP2.0 negotiates its addresses per session rather than using fixed ones. So an address sweep
finds nothing, and the `carpi uds` commands will not see these modules at all.

Generic OBD-II still works on the same car, because emissions rules mandate it. That is
exactly why a cheap dongle appears to work while telling you almost nothing: the odometer,
the cluster and the comfort modules are all behind TP2.0.

## Find the modules

This is the equivalent of a VCDS auto-scan.

```bash
carpi vag modules --channel can0
```

You should see a list of modules with their logical addresses, such as `0x01` for the engine
and `0x17` for the instrument cluster.

## Read live values

Measuring blocks are the live readings VCDS shows, grouped by number.

```bash
carpi vag blocks --channel can0 --module 0x17 --range 1-20
```

You should see decoded values with units. Fields whose scaling formula car-pi does not
recognise are shown as **raw bytes**, not guessed at. A plausible wrong engineering value is
worse than an honest unknown.

## Read one stored value

```bash
carpi vag read --channel can0 --module 0x17 --identifier 0x22
```

You should see the raw bytes for that identifier.

## Fault codes look different here

VAG fault codes are five-digit numbers, like `16486`. They are not `P` codes, and they need
their own lookup. Do not try to map them onto generic OBD-II codes.

## Coding these cars is possible

This era predates cryptographic protection. A login is a five-digit code the module compares,
not a signed exchange.

That makes coding feasible, which is not the same as safe. See [coding](coding.md).

## Try it with no car

Every command above accepts `--transport sim`, which talks to a simulated car of this era:

```bash
carpi vag modules --transport sim
```

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Want to change a setting? → [coding](coding.md)
- Doing a full inspection? → [inspect a car](inspect-a-car.md)
- Need the exact commands? → [command reference](commands.md)

## Words used here

KWP2000, TP2.0, UDS, measuring block, logical address — see the [glossary](glossary.md).
