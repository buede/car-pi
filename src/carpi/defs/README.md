# The car-pi definition database

This directory is **data, not code**. Adding support for a vehicle means editing YAML
here, never touching Python. That is the whole architectural bet: the protocol engine
is a few thousand lines of standards work that will not change much, while the database
is unbounded and is what proprietary tools actually charge for.

Everything is validated against the JSON Schemas in [`schema/`](schema/) on every
commit. A malformed file fails the build rather than producing a plausible-looking
wrong number in somebody's buying decision.

```
schema/          JSON Schemas, enforced in CI
generic/         standards-based, applies to every OBD-II vehicle
generic/rules/   the findings the report engine evaluates
vehicles/        <make>/<platform>/<ecu>.yaml — manufacturer-specific reads
```

Point the tool at your own checkout with `CARPI_DEFS_PATH=/path/to/defs`.

## Confidence is part of the data

Every definition carries a `confidence` field, and the report tells the reader which
level it relied on:

| Level | Meaning |
|---|---|
| `official` | Taken from a published standard (SAE J1979, ISO 15031-5, J2012). |
| `verified` | Confirmed against a real vehicle whose actual behaviour was known. |
| `community` | Contributed but unconfirmed. Reported, and labelled as unconfirmed. |

Marking something `community` is not a failure — it is how a guess gets into the tool
without being passed off as fact. Please don't promote an entry to `verified` unless
you checked it against a car whose true condition you independently knew.

## Adding a PID

`generic/mode01-pids.yaml` holds the Mode 01/02 parameters. Most are a scale and an
offset, expressed as a formula:

```yaml
- pid: 0x0C
  name: engine_rpm          # stable machine key — rules reference this, so renaming is breaking
  label: Engine RPM
  bytes: 2                  # expected payload length; a shorter reply is an error, not zero-padding
  unit: rpm
  formula: "U / 4"
  range: [0, 16383.75]      # physically plausible; outside it is reported as suspect
```

Formula variables:

| Name | Meaning |
|---|---|
| `A`, `B`, `C`… | individual payload bytes, `A` first |
| `U` | the whole payload as an unsigned big-endian integer |
| `S` | the whole payload as a signed two's-complement integer |

Only arithmetic, comparisons, and `abs`/`min`/`max`/`round` are permitted — the
expression language is a deliberately small allowlist, because these files come in via
pull request. A formula referencing a byte beyond `bytes` is rejected at load time
rather than raising mid-scan.

Bitfields and enumerations cannot be expressed arithmetically, so they name a builtin
in `carpi/core/protocol/decoders.py` instead:

```yaml
- pid: 0x01
  name: monitor_status
  label: Monitor status since DTCs cleared
  bytes: 4
  decoder: monitor_status
```

Exactly one of `formula` or `decoder` is required; the schema enforces it.

## Adding a rule

Rules in `generic/rules/` are findings, also data:

```yaml
- id: permanent-dtcs-present
  title: Permanent fault codes are stored
  severity: critical          # critical | high | medium | low | info
  when: "dtc.permanent_count > 0"
  explain: >
    Written for a non-specialist buyer. Say what it means, why it matters, and what
    to do next — not just which sensor is out of range.
```

**A rule whose facts are missing is skipped, never passed.** If the expression
references `pid.ltft_bank2` and the car has one cylinder bank, the rule reports as
"not assessed". This is the most important behaviour in the whole engine: a car that
would not answer a question has not answered it favourably, and silence must never
render as a clean bill of health.

Available fact namespaces:

| Namespace | Contents |
|---|---|
| `status.*` | `mil_on`, `dtc_count`, `ignition` (`spark`/`compression`) |
| `readiness.*` | `supported_count`, `complete_count`, `incomplete_count`, `<monitor>.complete` |
| `dtc.*` | `stored_count`, `pending_count`, `permanent_count`, `stored_unique_count` |
| `pid.<name>` | any decoded numeric Mode 01 PID, by its `name` above |
| `mode06.*` | `result_count`, `failing_count` |
| `vehicle.*` | `vin`, `ecu_count`, `claimed_odometer_km` (only if the operator supplied it) |

Run `carpi defs facts` to list every fact the current rules reference. CI asserts that
each `pid.*` fact matches a real PID name, so a typo fails the build instead of
silently producing a rule that never fires on any car.

## Adding a vehicle

`vehicles/<make>/<platform>/<ecu>.yaml` is where manufacturer-specific reads go —
odometer values from several modules, DPF soot load, hybrid battery state of health.
This is the part with the most value and the least coverage so far.

The most useful contribution is **data from a car whose behaviour you can verify**:
a scan (`carpi scan --format json`) from a vehicle where you independently know the
mileage, the service history, or what a mechanic found. A definition confirmed against
one known car is worth more than ten plausible guesses.

Please scrub the VIN from anything you attach to a public issue — it identifies a
specific car and its owner.
