# Change a module setting (coding)

**For:** someone who owns the car and wants to change a configuration value.
**You need:** a 2001–2010 VW-group car, its module login code, and a stable power supply.
**Time:** 15 minutes, and do not rush it.

> **Do not:** run this on a car you do not own. A wrong value can leave a module unusable.

**On this page**
- Which cars this works on
- What is refused outright
- Step 1: plan, which writes nothing
- Step 2: check the preconditions
- Step 3: apply
- Step 4: put it back

**This is the only part of car-pi that can write to a car.** Everything else is
structurally incapable of it. It is command-line only, and it is not exposed over the web
interface — that server has no authentication, and it will not be given this power.

## Which cars this works on

| Era | Coding possible? | Why |
|---|---|---|
| Roughly 2001–2010 VW group | Yes | A login is a five-digit code the module compares |
| Roughly 2020 onward VW group | No | SFD requires a token signed by Volkswagen's servers |

SFD is not bypassable by anyone without those servers. This is a hard limit, not a missing
feature.

## What is refused outright

These modules are refused, and **there is no flag to override it**:

- Airbag
- ABS
- Steering
- Immobiliser
- Parking brake

The refusal is in the code, not in the documentation. Asking politely will not help.

## Step 1: plan, which writes nothing

Always start here. `plan` reads the current value, decodes it, and shows you what would
change. It writes nothing at all.

```bash
carpi coding plan --module 0x46 --value 0A1B2D
```

You should see a decoded before-and-after. Nothing has been written to the car.

Read that output properly. This is the step where you catch a wrong value, and it costs you
nothing.

If the module needs a login just to read, add `--login`.

## Step 2: check the preconditions

`apply` checks these itself and refuses if they fail. Check them anyway, because a refusal
at this point is cheaper than a retry.

1. **Supply voltage is in range.** A module interrupted mid-write by a dying battery is the
   usual way one is destroyed.
2. **The vehicle is stationary.**
3. **The previous value can be archived to disk.** If it cannot be archived, the write does
   not happen.
4. **The module is not on the refused list above.**
5. **You can name the module.** `apply` makes you type it back.

## Step 3: apply

`apply` archives the current value first, then writes.

```bash
carpi coding apply --module 0x46 --value 0A1B2D --login 13861
```

You will be prompted to type the module's name before anything is written. That is
deliberate: a yes-or-no prompt can be answered without reading it.

`--login` is required here, even if `plan` did not need it.

## Step 4: put it back

Every `apply` leaves a restore point. To see them, newest first:

```bash
carpi coding list-restore-points
```

To put a module back:

```bash
carpi coding restore --file <the archived file>
```

You should see the module return to its previous value.

## If it did not work

See [troubleshooting](troubleshooting.md).

## Next

- Reading these cars instead? → [older VW and Audi cars](older-vw-audi.md)
- What else can and cannot be done? → [limits and safety](limits-and-safety.md)

## Words used here

Coding, SFD, module, login, ABS — see the [glossary](glossary.md).
