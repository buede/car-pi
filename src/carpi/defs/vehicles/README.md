# Vehicle definitions

`<make>/<platform>.yaml`, validated against
[`../schema/vehicle.schema.json`](../schema/vehicle.schema.json).

**This directory is nearly empty, and that is deliberate.**

Manufacturer data identifiers cannot be verified without the vehicle in front of you. A
wrong odometer identifier does not fail loudly — it returns four plausible bytes that
decode to a plausible mileage, and somebody buys a car on the strength of it. Every
proprietary tool's value is its database, and the only way this one becomes worth
trusting is if nothing enters it that was not confirmed against a real car.

So the shipped set contains one profile, `example/simulated.yaml`, which describes the
built-in simulator. It is marked `fictional: true`, which excludes it from ever matching
a real vehicle.

## Contributing a profile

The pipeline is:

```bash
# 1. Find the modules. OBD-II only exposes the emissions ones; the odometer is elsewhere.
carpi uds discover --transport socketcan

# 2. Ask a module what it holds. Read-only, and slow — start with a narrow range.
carpi uds scan-dids --request-id 0x714 --response-id 0x77E --out cluster.json

# 3. Work out what the values mean, then write them up as a profile.
```

Step 3 is the part that cannot be automated. The method that works:

1. Record a sweep.
2. Change one thing about the car — drive a kilometre, turn on the lights, let the fuel
   level drop.
3. Sweep again and diff. The identifier that moved by the right amount is your candidate.
4. Confirm it against a **second** car of the same platform whose true state you know
   independently. One car can agree with a wrong guess by coincidence; two rarely do.

Only after step 4 is a read `confidence: verified`. Before that it is `community`, and
the report tells the reader so.

## Confidence, concretely

| | |
|---|---|
| `official` | From a published standard. In practice only the ISO 14229 identification block, which lives in `carpi/core/protocol/uds.py` since it is protocol rather than vehicle data. |
| `verified` | Confirmed against a real vehicle whose behaviour was independently known. Name it in `verified_on`. |
| `community` | Contributed but unconfirmed. Reported, and labelled as unconfirmed. |

Marking something `community` is not a failure. It is how a plausible guess gets into the
tool without being passed off as fact.

## What is worth chasing first

Ordered by how much it matters when buying a used car:

- **Odometer, from every module that holds one.** Mileage tampering is usually done by
  rewriting the instrument cluster only. Give the read the id `odometer_km` in each ECU
  and the cross-module comparison happens automatically.
- **DPF soot load and regeneration count** on diesels. An expensive failure that a test
  drive cannot reveal.
- **Hybrid battery state of health and per-block voltages.** On a used hybrid this
  dominates the car's value.
- **Transmission adaptation values and clutch wear** on dual-clutch boxes.
- **Battery state of health** where the module tracks it.

## Sharing a scan

`carpi uds scan-dids` output contains the VIN, which identifies one physical car and,
through it, a person. Use `--anonymise` before attaching it to a public issue.

## Safety

Nothing in this schema can write to a vehicle; there is no field for it. Reads are safe.

If a module is one whose misconfiguration is dangerous — airbag, ABS, steering,
immobiliser — mark it `safety_critical: true`. It is advisory for reads, and exists so
that any future write path has an unambiguous flag to refuse on.
