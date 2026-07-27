# car-pi

An open-source vehicle diagnostics hub for Raspberry Pi. Plug into a car's OBD-II
port, extract everything the ECUs will report, and get a report that says what the
car's real condition and history are — including whether someone cleared the fault
codes shortly before you arrived to look at it.

Built as an alternative to the proprietary tools (VCDS, BimmerCode, Carly, Autel),
with the definition database open and community-editable.

> **Status: pre-alpha.** The generic OBD-II read path, read-only UDS, the virtual ECU
> simulator and the phone UI all work. Nothing here writes to a vehicle. The Raspberry Pi
> deployment scripts in `deploy/` have not yet been run on real hardware — see
> [`deploy/README.md`](deploy/README.md).

## Why this exists

The interesting part of a diagnostics tool is not the protocol code — that's a few
thousand lines of well-documented standards work. It's the **definition database**:
which data identifiers exist on which ECU, what the bytes mean, and which coding
values are safe. Proprietary vendors keep that database closed, and that is the
entire product.

car-pi is therefore a thin generic engine plus a large declarative database in
[`src/carpi/defs/`](src/carpi/defs/). Adding support for a new car means writing
YAML, not Python.

## What it can tell you about a used car

Generic OBD-II is standardized and works on essentially every car since ~2008, with
no authentication and nothing a seller can refuse. The checks that matter most:

- **Permanent DTCs** (Mode `0A`) — *cannot be cleared by any scan tool.* Only the
  ECU clears them, and only after its monitors re-pass. These survive a wipe.
- **Recent code-clear detection** — readiness monitors that haven't completed, plus
  zero stored faults, plus a low "distance since codes cleared", is a confession.
- **Mode `06` monitor test results** — numeric headroom on catalyst efficiency,
  misfire counters and EVAP, so you see failures that are coming, not just arrived.
- **Freeze frames** (Mode `02`) — the operating conditions when a fault was stored.
- **Fuel trims** at idle versus cruise — vacuum leaks, MAF and injector wear.
- **ECU swap / tune detection** — Mode `09` calibration IDs and CVNs.

## Beyond OBD-II

OBD-II reaches eight modules, chosen by emissions regulators. The instrument cluster
holding the odometer is not one of them, and neither is the ABS or body controller. So
car-pi also speaks **UDS (ISO 14229)**, read-only:

- **Cross-module odometer comparison.** Mileage tampering is nearly always done by
  rewriting the instrument cluster, leaving every other module holding the true figure.
  Modules do not drift apart on their own, so a disagreement is close to proof.
- **Cross-module VIN comparison** via the standardised identifier `0xF190`. A module
  holding another car's VIN came out of another car.
- **Manufacturer fault codes** from any module, invisible to generic OBD-II.
- **Module identification** — serial and part numbers, and the programming and
  calibration dates. A cluster reprogrammed last month on a high-mileage car is worth a
  question.

There is no standard map of manufacturer module addresses, so car-pi finds them:

```bash
carpi uds discover                              # sweep 0x700–0x7FF, read-only probes
carpi uds identify --request-id 0x714 --response-id 0x77E
carpi uds scan-dids --request-id 0x714 --response-id 0x77E --out cluster.json
```

`scan-dids` is how the database grows: it asks a module for every data identifier in a
range and records what comes back. Three outcomes all matter — data, *locked*
(`NRC 0x33`, meaning the identifier exists and the manufacturer protected it, often the
interesting ones), and nothing there.

**Nothing in this can modify a vehicle.** The UDS services that could —
`WriteDataByIdentifier`, `RoutineControl`, `SecurityAccess`, `ECUReset`, the transfer
services — are not implemented, the client refuses to emit them if one is ever routed
through it, and a test asserts no simulated module ever receives one.

### Why `defs/vehicles/` is nearly empty

Manufacturer identifiers cannot be verified without the car in front of you. A wrong
odometer identifier does not fail loudly — it returns plausible bytes that decode to a
plausible mileage, and somebody buys a car on the strength of it.

So car-pi ships the *engine* and the *scanner*, not a database of guesses. Only the ISO
14229 standard block is claimed as fact, because it is in a published standard. Everything
make-specific arrives from real scans, confirmed against a car whose true state was
independently known. See [`src/carpi/defs/vehicles/README.md`](src/carpi/defs/vehicles/README.md)
for how to contribute one — that is the highest-value thing anyone can add.

**car-pi will not clear your fault codes.** Mode `04` is deliberately unreachable
from the inspection path — clearing codes destroys exactly the evidence above, and a
tool that offers it one tap away from a report is a tool for sellers, not buyers.

## Older VAG cars: KWP2000 over TP2.0

VAG vehicles from roughly 2001 to 2010 — a Passat B6, for instance — do **not** use UDS
over ISO-TP for manufacturer diagnostics. They use KWP2000 over **TP2.0**, VW's own
connection-oriented transport, where the CAN IDs are negotiated per session rather than
fixed. Generic OBD-II on the same car still works over ISO-TP because EOBD mandates it,
which is exactly why a cheap dongle works while telling you almost nothing.

```bash
carpi vag modules                          # VCDS-style auto-scan by logical address
carpi vag blocks  --module 0x17 --range 1-20   # measuring blocks
carpi vag read    --module 0x17 --identifier 0x22
```

Fault codes come back as VAG's five-digit numbers (`16486`), not `P` codes, and
measuring-block fields whose scaling formula is not recognised are shown as raw bytes
rather than guessed at.

## Coding

**`carpi coding` is the only part of this that writes to a car.** Everything else is
structurally incapable of it — the read-only clients refuse to emit a write service, and
tests assert no module ever receives one.

Coding is feasible on the KWP2000 era because a login is a five-digit code the module
compares, not a cryptographic exchange. It is *not* feasible on a modern VAG: MQB-evo and
MEB (roughly 2020+) use **SFD**, which needs a token signed by VW's servers, and that is
not bypassable by anyone without them.

Feasible is not safe, so:

- **Airbag, ABS, steering, immobiliser and parking-brake modules are refused**, and there
  is no flag to override it.
- **The current value is archived to disk before any write.** If it cannot be archived,
  the write does not happen.
- **`plan` shows a decoded before-and-after and changes nothing.** `apply` requires you to
  type the module's name — a y/n prompt can be answered without reading it.
- **Supply voltage and vehicle speed are checked first.** A module interrupted mid-write by
  a dying battery is the usual way one is destroyed.
- **It is not exposed over the web interface, and will not be** — that server has no
  authentication.

`carpi coding restore --file <archived>` puts a module back.

## Honest limitations

- **TP2.0 and coding have never run on a real vehicle.** Both sides of the transport were
  written from the same published specification, so the tests prove internal consistency,
  not that a Passat agrees. Treat it as a careful hypothesis until confirmed.
- Coverage for coding will always be per-make, per-platform, per-generation.
  Read-only diagnostics are the part that generalizes.
- Toyota, Honda and Mazda expose very little configurable behaviour to begin with —
  they are excellent read targets and poor coding targets.
- Emissions-defeat modifications (DPF/EGR delete) are out of scope. They're illegal
  in most jurisdictions and would compromise the project's standing.

## Development without a car

You need neither a vehicle nor CAN hardware. `carpi.sim` is a virtual ECU that answers
real OBD-II requests over a real ISO-TP stack on an in-process virtual bus, so
everything above the transport layer is exercised exactly as it would be in a driveway.

### With containers, and no Python on your machine

```bash
./dev test        # run the suite
./dev serve       # UI on http://localhost:8080
./dev demo        # scan a simulated car
./dev lint        # ruff + shellcheck
./dev help        # everything else
```

Works with podman or docker; nothing else needs installing. The image is **Python 3.11
on Debian 12**, which is what Raspberry Pi OS Bookworm ships — so this runs closer to
the deployment target than a local install usually does.

### Natively

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

carpi scenarios                             # what simulated cars are available
carpi demo --scenario recently-cleared      # scan one, end to end
carpi demo --scenario healthy --detail      # all live data and Mode 06 results
carpi serve --transport sim                 # the phone UI, against a simulated car
pytest
```

For a two-terminal setup, `carpi sim` serves a scenario over UDP multicast and
`carpi scan --transport udp` connects to it.

### The SocketCAN path

The transport a real car uses needs a Linux kernel with `vcan`:

```bash
./dev socketcan                              # in a container
# or natively on Linux:
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
CARPI_TEST_SOCKETCAN=vcan0 pytest -m socketcan
```

On macOS or Windows the module must exist in your container runtime's virtual machine,
which it usually does not. CI runs these on every push against a Linux runner, so
skipping them locally costs no coverage — `./dev socketcan` prints the exact fix for
podman's default machine if you want them anyway.

### Portability note

SocketCAN and the `can-isotp` kernel module are Linux-only. The transport layer is
abstracted over two backends so the same code and the same tests run everywhere:

| Backend | Where | Used for |
|---|---|---|
| `virtual` — python-can virtual bus + userspace ISO-TP | any OS | development, CI, the simulator |
| `socketcan` — SocketCAN + kernel ISO-TP | Linux / Pi | real vehicles |

## Hardware

Portable inspection unit, roughly €100:

| Item | Choice | Notes |
|---|---|---|
| SBC | Raspberry Pi 4 | The Ethernet port is the future DoIP path |
| CAN | Waveshare 2-CH CAN FD HAT (2× MCP2518FD) | `mcp251xfd` driver is mainline. Screw terminals, so no DB9 pinout mistakes. |
| Power | USB-C PD power bank | See below |
| Harness | OBD-II (J1962) male pigtail | CAN_H = pin 6, CAN_L = pin 14, GND = pin 4 or 5 |

**On a pre-2019 car, build the interface instead.** CAN FD did not exist in 2006, so on a
Passat B6 the CAN FD HAT is paying for a feature the car cannot use. An MCP2515 plus a
3.3 V SN65HVD230 transceiver costs about €5, needs no custom software, and is fully
adequate — see [`hardware/README.md`](hardware/README.md) for the wiring, the netlist, and
the two traps that catch most hand-built boards.

`carpi bench` runs the simulator on one CAN interface and the client on another, so two
cheap nodes on a wire validate the stack — including TP2.0's timing and keepalive — before
anything touches a car.

**Power the Pi from a battery bank, not the car.** This removes the automotive
transient-protection stage, removes SD-card corruption when the ignition is cycled,
and avoids draining a stranger's battery during a read that takes several minutes
with the engine off. Grounding the transceiver through OBD pin 4/5 is sufficient;
the Pi never needs pin 16.

Two things worth writing on the case:

1. **Leave the 120 Ω termination jumper OFF.** The vehicle bus is already terminated
   at both ends. A third terminator causes bus errors.
2. **Ignition ON, not ACC.** Many ECUs stay asleep in accessory mode and won't answer.

## The phone interface

`carpi serve` runs a local web UI, meant for a phone joined to the Pi's own hotspot.
It shows the report worst-finding-first, streams live values during a test drive, and
lets you download the raw JSON.

It is entirely self-contained: no CDN, no fonts, no analytics, no external request of
any kind. That is enforced by a test, because a stylesheet from a CDN is invisible in
development and an unstyled page in a car park.

**The interface is used by one conversation at a time.** A second inspection, or live
values during an inspection, is refused rather than queued — two request/response
conversations on one ISO-TP channel would each decode the other's replies, producing
values quietly attributed to the wrong parameter.

There is no authentication. The server is read-only, on its own hotspot, and a login
would make a tool used one-handed in a driveway materially worse. **That reasoning stops
holding the moment writing to a vehicle becomes possible** — coding must not ship
without authentication in front of it.

## Layout

```
src/carpi/
├─ core/     transport · protocol · defs loader · rules engine · live polling
├─ defs/     THE DATABASE — YAML data + JSON schemas, validated in CI
├─ sim/      virtual ECU with scenario fixtures
├─ server/   local HTTP + WebSocket API, and the PWA it serves
├─ report/   text and JSON renderings
└─ cli/      command-line surface
deploy/      Pi setup: CAN HAT, systemd, hotspot, read-only root (unverified)
tests/       unit tests, API contract tests, offline guarantees
```

## Contributing

The most valuable contribution is data, not code: a scan from a car whose behaviour
you can verify. See [`src/carpi/defs/README.md`](src/carpi/defs/README.md) for the
file formats. Every definition carries a `confidence` field — `community`,
`verified`, or `official` — and the report tells the user which it relied on.

Set `CARPI_DEFS_PATH` to point at a checkout of an alternative database.

## Etiquette and law

Ask before plugging into a car you don't own. Reads are non-invasive, but it's
someone else's property, and the conversation goes better when the report is
something you offer to share rather than something you did covertly.

Diagnosing and repairing your own vehicle is explicitly protected in many
jurisdictions (the US DMCA §1201 exemption for vehicle diagnosis/repair/
modification, EU right-to-repair). Tampering with odometers or emissions controls is
not, anywhere.

## Licence

Code: **GPL-3.0-or-later**. Definition database: **CC-BY-SA-4.0**.

Copyleft is deliberate — a community-built definition database should not be
absorbable into a closed product.
