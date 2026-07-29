# Limits, safety and the law

**For:** anyone deciding how much to trust this, and what they are allowed to do with it.
**You need:** nothing. Read this before you use car-pi on a car you care about.
**Time:** about ten minutes.

**On this page**
- What is finished and what is not
- What it cannot do to your car
- It will not clear your fault codes
- What has not been proven yet
- One inspection at a time
- There is no password
- What ends up on your disk
- Asking, and the law
- What is out of scope

## What is finished and what is not

car-pi is **pre-alpha**.

| Part | State |
|---|---|
| Generic OBD-II reading | Works |
| Read-only UDS, module discovery, identifier sweeping | Works |
| The virtual car simulator | Works |
| The phone interface | Works |
| KWP2000 over TP2.0, for older VW and Audi | Written, never run on a real car |
| Coding, the write path | Written, never run on a real car |
| The Raspberry Pi setup scripts | Written, never run on real hardware |

## What it cannot do to your car

Everything except `carpi coding` is **structurally incapable** of writing to a vehicle.

The UDS services that could change something are not implemented at all:
`WriteDataByIdentifier`, `RoutineControl`, `SecurityAccess`, `ECUReset`, and the transfer
services. The client refuses to emit them if one is ever routed through it. A test asserts
that no simulated module ever receives one.

This is not a promise about intent. It is a property of the code, checked on every push.

`carpi coding` is the one exception, and it is deliberately quarantined. See
[coding](coding.md).

## It will not clear your fault codes

Mode `04` is not implemented, and it is not reachable from the inspection path.

Clearing codes destroys exactly the evidence an inspection depends on. A tool that offers
it one tap away from a report is a tool for sellers, not buyers.

## What has not been proven yet

**TP2.0 and coding have never run on a real vehicle.** Both sides of the transport were
written from the same published specification. The tests therefore prove internal
consistency, not that a real car agrees. Treat it as a careful hypothesis until somebody
confirms it.

**The vehicle definition database is nearly empty, on purpose.** A wrong odometer identifier
does not fail loudly. It returns plausible bytes that decode to a plausible mileage, and
somebody buys a car on the strength of it. Nothing enters the database that was not
confirmed against a real car. See [contribute vehicle data](contribute-vehicle-data.md).

**Fault codes are not described individually.** car-pi says which part of the car a code
concerns and whether it is standardised, both of which are readable from the code itself. It
does not tell you that `P0420` means a spent catalytic converter.

That is deliberate. A per-code description table would have to cover the manufacturer-specific
ranges to be worth having, and there the same code means different faults on different makes.
A wrong description does not fail loudly — it sends somebody to replace the wrong part. So
car-pi says what the standard says and stops.

**Coding coverage will always be narrow.** It is per-make, per-platform, per-generation.
Read-only diagnostics are the part that generalises.

**Some makes expose very little.** Toyota, Honda and Mazda offer almost nothing
configurable. They are excellent read targets and poor coding targets.

## One inspection at a time

A second inspection, or live values during an inspection, is refused rather than queued.

This is not a missing feature. Two request-and-response conversations sharing one channel
would each decode the other's replies. You would get real-looking values quietly attributed
to the wrong parameter, which is worse than an error message.

## There is no password

The web interface has no authentication. That is defensible today: the server is read-only,
it sits on its own hotspot, and a login would make a tool used one-handed in a driveway
materially worse.

**That reasoning stops holding the moment writing to a vehicle becomes possible.** Whoever
puts coding behind the web interface must put authentication in front of it first. The
failure mode changes from "somebody on your hotspot reads your fuel trims" to "somebody on
your hotspot reconfigures your ABS".

Coding is command-line only, at a keyboard, by whoever owns the car.

## What ends up on your disk

A scan is not a neutral document. It contains the VIN, which identifies one physical car and,
through it, a person.

So anything car-pi writes containing vehicle data is **readable only by you**, in a directory
readable only by you. That covers reports, identifier sweeps and coding restore points. A
restore point is the most sensitive of them, because it also holds the module's login code.

Reports and sweeps default to a path under `~/.carpi` rather than to the directory you happen
to be in. A bare filename in a checkout is one `git add -A` away from publishing a real car's
VIN, and `.gitignore` covers the names car-pi suggests as a backstop.

A **contribution** is the exception, and deliberately so: it carries no values, no serial
numbers and no VIN, so it is written with ordinary permissions because it exists to be shared.
See [contribute vehicle data](contribute-vehicle-data.md).

This is not encryption. It addresses the ordinary risk — a shared machine, a backup that
copies your home directory, a file forgotten in a working copy.

## Asking, and the law

**Ask before plugging into a car you do not own.** Reads are non-invasive, but it is someone
else's property. The conversation also goes better when the report is something you offer to
share rather than something you did covertly.

Diagnosing and repairing your own vehicle is explicitly protected in many jurisdictions.
In the United States there is a DMCA §1201 exemption for vehicle diagnosis, repair and
modification. The European Union has right-to-repair provisions.

Tampering with odometers or emissions controls is not protected anywhere.

## What is out of scope

**Emissions-defeat modifications are out of scope.** Diesel particulate filter and exhaust
gas recirculation deletes will not be added. They are illegal in most jurisdictions and
would compromise the project's standing.

## Next

- What can it actually find? → [what it can find](what-it-can-find.md)
- Thinking about coding? → [coding](coding.md)
- Want to help close the gaps? → [CONTRIBUTING.md](../CONTRIBUTING.md)

## Words used here

UDS, DTC, TP2.0, Mode 04, ABS — see the [glossary](glossary.md).
