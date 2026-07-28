# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

car-pi reads a car's real condition and history over its OBD-II port, aimed at someone
inspecting a used car before buying it. The thesis, from the README: the protocol code is a
few thousand lines of published standards work, and the **definition database** is the actual
product. So adding support for a car means writing YAML in `src/carpi/defs/`, not Python.

Pre-alpha. Reading works. TP2.0, coding and the Pi deploy scripts have never run on real
hardware, and the docs say so in each place — keep it that way.

## Commands

Everything runs in a container via `./dev`, so no local Python toolchain is needed. All 11
subcommands are listed in `CONTRIBUTING.md`; the ones that matter most:

```bash
./dev test              # full suite
./dev lint              # ruff check + ruff format --check + shellcheck. This is the CI gate
./dev fmt               # fixes what lint reports. Run this before assuming CI is wrong
./dev demo              # scan a simulated car
./dev serve             # UI on localhost:8080
./dev socketcan         # the vcan suite; needs a Linux kernel with vcan
```

With a local venv, run pytest directly:

```bash
pytest                                              # 550 tests, ~90s
pytest tests/test_rules.py::TestSkippedNotPassed    # one class
pytest -k odometer                                  # by keyword
pytest -m socketcan                                 # needs CARPI_TEST_SOCKETCAN=vcan0
```

`pyproject.toml` sets `addopts = "-q -m 'not socketcan'"`, so the SocketCAN suite is opted
into, never default. Ruff: line-length 100, `select = ["E","F","I","UP","B","SIM"]`, target
py311.

**Python 3.11 is a deliberate floor** — it is what Raspberry Pi OS Bookworm ships. Do not
raise it without a plan for the Pi image.

Environment variables: `CARPI_DEFS_PATH` (point at an alternative database checkout),
`CARPI_RESTORE_DIR`, `CARPI_TEST_SOCKETCAN`, `CARPI_CAN_INTERFACE`, `CARPI_CAN_BITRATE`.

## Architecture

Four layers, with a hard read/write firewall down the middle:

```
cli/ (main, vag, bench)        server/ (app, jobs, vehicle)
        └──────────► core/scan.py ◄──────────┘
                         │
                 core/rules.py ──► core/expr.py
                 core/database.py ──► defs/*.yaml + defs/schema/*.json
                         │
        core/protocol/ {obd2, uds, kwp2000, dtc, decoders}   ← read-only, enforced
                         │
              core/transport/ {base, canbus, tp20}
                         │
          python-can bus (socketcan | virtual | udp)  ◄── sim/ answers here

coding/ ← the ONLY write path. Imported by nothing above. Enforced by a test.
```

**The transport abstraction is `Channel`** (`core/transport/base.py`), a Protocol with just
`address` and `request(payload, timeout)`. Segmentation is handled below it, so nothing above
knows which transport it is on — that is what lets one `KwpClient` run over both ISO-TP and
TP2.0. Transport choice is made only in the outermost layer (CLI flags, server providers).

**ISO-TP is stateless; TP2.0 is connection-oriented**, and every structural difference follows
from that. TP2.0 negotiates its CAN IDs per session at broadcast `0x200`, so an address sweep
cannot find those modules; channels are objects with a lifetime and a `close()`; and it needs a
keepalive thread or the module drops the channel.

**The three protocol clients share a shape, not a base class.** `Obd2Client`, `UdsClient` and
`KwpClient` each independently implement `_exchange`, NRC classification (`is_unsupported`,
`is_protected`), and per-item echo verification — because one timeout puts every later read
one exchange out of step, silently attributing values to the wrong identifier.

**Read-only is enforced in five places, not one:** a named `FORBIDDEN_SERVICES` set in both
`uds.py` and `kwp2000.py`; a backstop check inside each `_exchange` so a future refactor fails
loudly; session gating (no programming session); Mode 04 simply not existing in `obd2.py`; and
`core/discovery.py` refusing to use any forbidden service as a probe. Tests watch the wire
(`clear_requests`, `write_attempts`) and the import graph, rather than trusting the API.

## The invariants — do not weaken these

Each is enforced by a test. If a change trips one, the design has gone wrong, not the test.

**Silence is never a pass.** This recurs at five layers and is the project's central idea:
`NoResponse` means "unsupported", never "value zero" (transport); implausible values are
omitted from facts rather than clamped (scan); a rule whose facts are missing is **skipped**,
never passed (`core/rules.py` checks `rule.required_facts` *before* evaluating, because
`and`/`or` short-circuit and could otherwise return a pass without touching the missing fact);
and `not_assessed_count` sits beside `passed_count` at every presentation layer.

**The simulator is an independent implementation, never a mirror of the client.**
`sim/encode.py` is written from SAE J1979 directly, *not* by inverting the formulas in
`defs/`, because an encoder built as the algebraic inverse of a decoder agrees with it even
when both are wrong. `sim/tp20.py` likewise does not call into `core/transport/tp20.py`. Where
independence is impossible (same author, same document), the limit is documented rather than
papered over. **Never "fix" a round-trip test by making the two sides share code.**

**Structural safety over configured safety.** Absent code beats disabled code; separate
packages beat flags. The coding path's refusal of safety-critical modules has no override
parameter, and a test asserts none exists.

**Honest unknowns over plausible guesses.** Unknown KWP measuring-block formulas return raw
bytes. Short payloads raise rather than zero-extend. Mode 06 values stay raw counts.

**The expression language is a deliberately tiny allowlist** (`core/expr.py`): seven binary
ops, six comparisons, and `abs`/`min`/`max`/`round`. No attribute access, subscript,
comprehension or lambda is reachable. Definition files arrive by pull request, so neither they
nor rules may reach `eval`. Every addition here widens what an untrusted file can do.

**One conversation at a time.** `server/vehicle.py` holds a non-blocking `threading.Lock` and
**refuses rather than queues**, because two callers sharing a channel would each decode the
other's replies — silent corruption, not an error.

## YAGNI — keep features and files to a minimum

Build what is needed now, not what might be needed later. This is not aspirational here; the
codebase already does it, so follow the precedent rather than interpret the principle.

- **Do not add a file when an existing one fits.** `docs/` keeps a written list of documents
  deliberately *not* created, each with its reason — no FAQ ("where facts go to escape
  ownership"), no protocols explainer ("a textbook, not a task"), no architecture tour. If you
  think a new file is needed, justify it against that list and add it to `docs/README.md`.
- **Do not add a flag for a hypothetical need.** The coding path's refusal of safety-critical
  modules has no override, on the stated grounds that "a flag is exactly what gets passed at
  eleven at night". A test asserts the parameter does not exist.
- **Do not widen a surface speculatively.** `core/expr.py`'s allowlist is four functions, and
  the comment says "Deliberately tiny. Every addition here widens what a definition file can
  do." The same goes for CLI options, API routes and schema fields.
- **Do not write roadmap prose.** "Future DoIP path" and a speculative Pico watchdog aside were
  both cut from the docs for this reason. Unbuilt intentions belong in issues, not in files
  that read as documentation of what exists.
- **Do not pad the database.** `defs/vehicles/` ships nearly empty on purpose. An unverified
  entry is worse than a missing one.
- **Prefer deleting to deprecating**, and absent code to disabled code. There is no CHANGELOG
  at version 0.0.1 because there are no releases yet.
- **A new dependency needs a stated reason.** `pyproject.toml` picks `websockets` over
  `uvicorn[standard]` specifically to avoid compiling uvloop and httptools on a Pi.

When a change feels like it needs a new module, a new flag and a new doc, it is usually one
feature pretending to be three. Ask before building it.

## Non-obvious couplings

These break without touching the file you edited.

| If you change | Also update |
|---|---|
| The `dev` script's header comment (lines 3–22) | `./dev help` prints `sed -n '3,22p'` of itself. Adding a line silently truncates the output |
| `docs/build-the-can-interface.md` | `tests/test_hardware_netlist.py` pins the strings `crystal`, `oscillator=16000000` and `MCP2551`. The oscillator line lives inside a fenced `config.txt` block |
| `carpi scenarios` stdout layout | CI greps `^[a-z]` to enumerate scenarios (`.github/workflows/ci.yml`) |
| The root `README.md` path | `pyproject.toml` `readme =` and two `Dockerfile` COPY lines depend on it. Docker never copies `docs/`, so `readme =` cannot point there |
| Any `docs/` filename | `tests/test_docs.py` requires every relative link to resolve and every doc to be listed in `docs/README.md` |
| A `defs/` PID name | Rules reference PIDs by `name`; CI asserts every `pid.*` fact matches a real one |
| `sim/scenarios.py` expectations | Each scenario declares `expect_findings`, asserted for **exact** set equality |

Versioned JSON schema strings, bumped rather than silently changed: `carpi.inspection/1`
(report), `carpi.didscan/1`, `carpi.discovery/1`, `carpi.vagscan/1`, `carpi.bench/1`,
`carpi.restore/1`.

## Documentation

All prose lives in `docs/`, one question per file, indexed in `docs/README.md`. The root
README is a router capped at 130 lines by a test.

**`CONTRIBUTING.md` holds the writing rules, and they are enforced by convention plus one
test.** The short version: one idea per sentence; sandwich every command block with what it
does above and what you should see below; every doc opens with `**For:** / **You need:** /
**Time:**` and closes with `## Next`. New docs must be added to `docs/README.md`.

Four directories keep 4-line pointer stubs (`hardware/`, `deploy/`, `src/carpi/defs/`,
`src/carpi/defs/vehicles/`) so folder views and paths referenced from YAML and `--help` still
resolve.

## Code register

Comments here carry the *reasoning*, not the mechanics, and usually name the failure mode in
vehicle terms — "a wrong odometer identifier returns plausible bytes, and somebody buys a car
on the strength of it". Matching that register matters more in this repo than in most. If a
non-obvious line has no "why", it is incomplete.

## Known unfinished edge

`Database.profile_for_vin()` and `VehicleProfile.matches_vin()` are implemented and tested,
but called only from `tests/test_vehicles.py` — never from `scan_vehicle` or the CLI. So
automatic VIN-based profile selection does **not** happen, despite `--profile`'s help text
saying "Omit to select one by VIN". Wiring it up is a real task, not a cleanup.
