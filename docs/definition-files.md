# Definition files

**For:** anyone adding a reading, a finding, or a vehicle to the database.
**You need:** a text editor. No Python.
**Time:** 15 minutes to read, then it is copy-and-adapt.

The database is **data, not code**. Adding support for a vehicle means editing YAML, never
touching Python.

That is the whole architectural bet. The protocol engine is a few thousand lines of standards
work that will not change much. The database is unbounded, and it is what proprietary tools
actually charge for.

**On this page**
- What is in here
- Confidence
- Add a PID
- Add a rule
- Add a vehicle
- The safety_critical flag

## What is in here

Everything lives under `src/carpi/defs/`:

```
schema/          JSON Schemas, enforced in CI
generic/         standards-based, applies to every OBD-II vehicle
generic/rules/   the findings the report engine evaluates
vehicles/        <make>/<platform>.yaml — manufacturer-specific reads
```

Every file is validated against its schema on every commit. A malformed file fails the build
rather than producing a plausible-looking wrong number in somebody's buying decision.

```bash
carpi defs check
```

You should see no errors.

To point car-pi at your own copy of the database, set `CARPI_DEFS_PATH`:

```bash
CARPI_DEFS_PATH=/path/to/defs carpi scan --channel can0
```

## Confidence

Every definition carries a `confidence` field. The report tells the reader which level it
relied on.

| Level | Meaning |
|---|---|
| `official` | Taken from a published standard, such as SAE J1979, ISO 15031-5 or J2012 |
| `verified` | Confirmed against a real vehicle whose actual behaviour was known. Name it in `verified_on` |
| `community` | Contributed but unconfirmed. Reported, and labelled as unconfirmed |

Marking something `community` is not a failure. It is how a guess gets into the tool without
being passed off as fact.

> **Careful:** do not promote an entry to `verified` unless you checked it against a car whose
> true condition you independently knew. See
> [contribute vehicle data](contribute-vehicle-data.md).

## Add a PID

`generic/mode01-pids.yaml` holds the live parameters. Most are a scale and an offset,
expressed as a formula.

```yaml
- pid: 0x0C
  name: engine_rpm          # stable machine key — rules reference this, so renaming breaks them
  label: Engine RPM
  bytes: 2                  # expected payload length; a shorter reply is an error
  unit: rpm
  formula: "U / 4"
  range: [0, 16383.75]      # physically plausible; outside it is reported as suspect
```

Formula variables:

| Name | Meaning |
|---|---|
| `A`, `B`, `C`… | Individual payload bytes, `A` first |
| `U` | The whole payload as an unsigned big-endian integer |
| `S` | The whole payload as a signed two's-complement integer |

Only arithmetic, comparisons, and `abs`, `min`, `max` and `round` are permitted. The
expression language is a deliberately small allowlist, because these files arrive by pull
request.

A formula referencing a byte beyond `bytes` is rejected when the file loads, not mid-scan.

Bitfields and enumerations cannot be expressed arithmetically. They name a builtin decoder
instead:

```yaml
- pid: 0x01
  name: monitor_status
  label: Monitor status since DTCs cleared
  bytes: 4
  decoder: monitor_status
```

Decoders live in `src/carpi/core/protocol/decoders.py`. Exactly one of `formula` or `decoder`
is required, and the schema enforces that.

## Add a rule

Rules in `generic/rules/` are the findings the report shows. They are also data.

```yaml
- id: permanent-dtcs-present
  title: Permanent fault codes are stored
  severity: critical          # critical | high | medium | low | info
  when: "dtc.permanent_count > 0"
  explain: >
    Written for a non-specialist buyer. Say what it means, why it matters, and what
    to do next — not just which sensor is out of range.
```

### A rule whose facts are missing is skipped, never passed

This is the most important behaviour in the whole engine.

If the expression references `pid.ltft_bank2` and the car has one cylinder bank, the rule
reports as **"not assessed"**. It does not report as a pass.

A car that would not answer a question has not answered it favourably. Silence must never
render as a clean bill of health.

### Facts you can reference

| Namespace | Contents |
|---|---|
| `status.*` | `mil_on`, `dtc_count`, `ignition` (`spark` or `compression`) |
| `readiness.*` | `supported_count`, `complete_count`, `incomplete_count`, `<monitor>.complete` |
| `dtc.*` | `stored_count`, `pending_count`, `permanent_count`, `stored_unique_count` |
| `pid.<name>` | Any decoded numeric live parameter, by its `name` above |
| `mode06.*` | `result_count`, `failing_count` |
| `vehicle.*` | `vin`, `ecu_count`, `claimed_odometer_km` (only if the operator supplied it) |

To list every fact the current rules reference:

```bash
carpi defs facts
```

CI asserts that each `pid.*` fact matches a real PID name, so a typo fails the build instead
of silently producing a rule that never fires on any car.

## Add a vehicle

`vehicles/<make>/<platform>.yaml` holds manufacturer-specific reads: odometer values from
several modules, diesel particulate filter soot load, hybrid battery state of health.

This is the part with the most value and the least coverage.

**The field procedure is a separate document**, because it is the hard part. See
[contribute vehicle data](contribute-vehicle-data.md).

## The safety_critical flag

If a module is one whose misconfiguration is dangerous — airbag, ABS, steering, immobiliser —
mark it:

```yaml
safety_critical: true
```

It is advisory for reads. It exists so that any write path has an unambiguous flag to refuse
on. Nothing in this schema can write to a vehicle; there is no field for it.

## Next

- Got a car to scan? → [contribute vehicle data](contribute-vehicle-data.md)
- Sending a pull request? → [CONTRIBUTING.md](../CONTRIBUTING.md)

## Words used here

PID, DTC, DID, Mode 01, readiness monitor — see the [glossary](glossary.md).
