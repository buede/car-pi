# car-pi

An open-source vehicle diagnostics hub for Raspberry Pi. Plug into a car's OBD-II
port, extract everything the ECUs will report, and get a report that says what the
car's real condition and history are — including whether someone cleared the fault
codes shortly before you arrived to look at it.

Built as an alternative to the proprietary tools (VCDS, BimmerCode, Carly, Autel),
with the definition database open and community-editable.

> **Status: pre-alpha.** The generic OBD-II read path and the virtual ECU simulator
> are the current focus. Nothing here writes to a vehicle yet.

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

Make-specific reads (odometer cross-checked across several ECUs to catch mileage
tampering, DPF soot load, hybrid battery state of health) come from the definition
database and are being built out for VAG and Toyota/Honda/Mazda first.

**car-pi will not clear your fault codes.** Mode `04` is deliberately unreachable
from the inspection path — clearing codes destroys exactly the evidence above, and a
tool that offers it one tap away from a report is a tool for sellers, not buyers.

## Honest limitations

- **Coding/writing is gated by cryptography, not obscurity.** UDS Security Access
  (service `0x27`) uses proprietary per-platform seed/key algorithms. On VAG
  MQB-evo/MEB (roughly 2020+), **SFD** requires an online-signed token from VW.
  That is not bypassable, by us or anyone else without VW's servers.
- Coverage for coding will always be per-make, per-platform, per-generation.
  Read-only diagnostics are the part that generalizes.
- Toyota, Honda and Mazda expose very little configurable behaviour to begin with —
  they are excellent read targets and poor coding targets.
- Emissions-defeat modifications (DPF/EGR delete) are out of scope. They're illegal
  in most jurisdictions and would compromise the project's standing.

## Development without a car

You do not need a vehicle, or even CAN hardware, to work on this. `carpi.sim` is a
virtual ECU that answers OBD-II requests over an in-process virtual CAN bus, so the
whole stack runs on macOS and in CI.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'

carpi scenarios                             # what simulated cars are available
carpi demo --scenario recently-cleared      # scan one, end to end
carpi demo --scenario healthy --detail      # all live data and Mode 06 results
pytest
```

`carpi demo` runs a simulated car in-process and scans it over a real ISO-TP stack, so
everything above the transport layer is exercised exactly as it would be in a driveway.
For a two-terminal setup, `carpi sim` serves a scenario over UDP multicast and
`carpi scan --transport udp` connects to it.

The SocketCAN path — the one a real car uses — is covered separately, and needs Linux:

```bash
sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
CARPI_TEST_SOCKETCAN=vcan0 pytest -m socketcan
```

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

**Avoid MCP2515 boards.** CAN 2.0 only, no CAN FD, which rules out 2019+ platforms.

**Power the Pi from a battery bank, not the car.** This removes the automotive
transient-protection stage, removes SD-card corruption when the ignition is cycled,
and avoids draining a stranger's battery during a read that takes several minutes
with the engine off. Grounding the transceiver through OBD pin 4/5 is sufficient;
the Pi never needs pin 16.

Two things worth writing on the case:

1. **Leave the 120 Ω termination jumper OFF.** The vehicle bus is already terminated
   at both ends. A third terminator causes bus errors.
2. **Ignition ON, not ACC.** Many ECUs stay asleep in accessory mode and won't answer.

## Layout

```
src/carpi/
├─ core/     transport · protocol · defs loader · rules engine
├─ defs/     THE DATABASE — YAML data + JSON schemas, validated in CI
├─ sim/      virtual ECU with scenario fixtures
└─ cli/      command-line surface
tests/       unit tests + recorded-session replay fixtures
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
