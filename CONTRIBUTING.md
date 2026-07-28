# Contributing to car-pi

**For:** anyone who wants to help, with or without a car.
**You need:** podman or docker. Nothing else.
**Time:** 10 minutes to a working checkout.

## The most valuable contribution is data, not code

The protocol code is a few thousand lines of published standards work. It will not change
much. The **definition database** is unbounded, and it is what proprietary tools actually
charge for.

So the highest-value thing anyone can add is a confirmed read from a real car. See
[contribute vehicle data](docs/contribute-vehicle-data.md) for how, and
[definition files](docs/definition-files.md) for the file formats.

You do not need to write any Python to do this.

## Get set up

```bash
git clone https://github.com/buede/car-pi
cd car-pi
./dev test
```

You should see the suite pass. The first run builds a container image, so it takes a few
minutes. After that it is fast.

## The commands you need

`./dev` runs everything in a container, so your machine needs no Python toolchain.

| Command | What it does |
|---|---|
| `./dev test` | Run the test suite. Extra arguments go to pytest |
| `./dev lint` | Check formatting and shell scripts. This is the CI gate |
| `./dev fmt` | **Fix** formatting automatically. Run this if `lint` fails |
| `./dev defs` | Validate the definition database |
| `./dev demo` | Scan a simulated car |
| `./dev serve` | Serve the web interface on `http://localhost:8080` |
| `./dev socketcan` | Run the SocketCAN tests. Needs a Linux kernel with `vcan` |
| `./dev shell` | Open a shell inside the container |
| `./dev build` | Rebuild the image |
| `./dev clean` | Delete the image |
| `./dev help` | Print this list |

**If CI fails on formatting, run `./dev fmt` and commit the result.** Continuous
integration runs `ruff format --check`, which reports problems but does not fix them.

The container is Python 3.11 on Debian 12, matching Raspberry Pi OS Bookworm. That is
closer to the deployment target than a local install usually is.

## Running the tests

`./dev test` covers everything except the SocketCAN suite, which needs Linux kernel
support. The transport layer is abstracted over two backends so the same code and the same
tests run everywhere:

| Backend | Where it runs | Used for |
|---|---|---|
| `virtual` | Any operating system | Development, CI, the simulator |
| `socketcan` | Linux and the Pi | Real vehicles |

To run the SocketCAN tests:

```bash
./dev socketcan
```

On macOS or Windows the `vcan` module must exist inside your container runtime's virtual
machine, and usually does not. CI runs these on every push against a Linux runner, so
skipping them locally costs no coverage. `./dev socketcan` prints the exact fix for
podman's default machine if you want them anyway.

## Where things live

```
src/carpi/
├─ core/     transport · protocol · defs loader · rules engine · live polling
├─ defs/     THE DATABASE — YAML data + JSON schemas, validated in CI
├─ sim/      virtual ECU with scenario fixtures
├─ server/   local HTTP + WebSocket API, and the web app it serves
├─ report/   text and JSON renderings
├─ coding/   the only package that can write to a car
└─ cli/      command-line surface
docs/        all documentation
hardware/    the netlist for the hand-built CAN interface
```

## Invariants you must not break

Each of these is enforced by a test, not by convention. If your change trips one, the
design has gone wrong rather than the test.

- **The web interface makes no external requests.** No CDN, no fonts, no analytics. A
  stylesheet from a CDN is invisible in development and an unstyled page in a car park.
- **The read-only clients refuse to emit a write service.** No module ever receives one
  during an inspection.
- **`carpi.core`, `carpi.report`, `carpi.sim` and `carpi.server` have no import path to
  `carpi.coding`.** If you need one, something is in the wrong package.
- **A rule whose facts are missing is skipped, never passed.** A car that would not answer
  a question has not answered it favourably. Silence must never render as a clean bill of
  health.
- **Nothing implements Mode `04`.** Clearing fault codes destroys the evidence an
  inspection depends on.

## Never commit vehicle data casually

Raw logs contain the VIN, which identifies a real car and its owner. `.gitignore` already
excludes `scans/`, `*.candump`, `*.asc` and `*.blf`.

Use `--anonymise` on `carpi uds scan-dids` before attaching output to a public issue.
Curated, VIN-scrubbed fixtures belong in `tests/fixtures/` and are committed deliberately.

## Writing documentation

Docs are for people who find dense prose hard to read. Follow these rules.

- One idea per sentence. Twenty words maximum. Split em-dash subclauses into sentences.
- Conclusion first, reason second. Never bury the verdict mid-paragraph.
- **Sandwich every command block:** one line above saying what it does, one line below
  saying what you should see.
- Numbered steps start with a verb. "Turn the ignition on", not "the ignition should be on".
- Bold only the load-bearing word. Never a bold sentence. No italics for emphasis.
- Paragraphs of three lines or fewer. Tables of three columns or fewer.
- Expand each abbreviation once per document, then rely on the [glossary](docs/glossary.md).
- Warnings use `> **Careful:**` (recoverable) or `> **Do not:**` (not recoverable).
- One document answers one question. Aim for 120 lines of **prose**; reference tables and
  wiring diagrams do not count. A document that is long because of paragraphs needs
  splitting. One that is long because of tables is usually fine.
- Avoid metaphor and idiom in instructions.

Every document in `docs/` opens with `**For:**`, `**You need:**` and `**Time:**`, and
closes with `## Next`. Keep that shape — a reader who has read one page then knows where
to look on all the others.

New documents must be listed in [`docs/README.md`](docs/README.md). A test enforces this,
along with every relative link resolving.

## Licences

Code is **GPL-3.0-or-later**. The definition database is **CC-BY-SA-4.0**. Copyleft is
deliberate: a community-built database should not be absorbable into a closed product.

By contributing you agree your work ships under these terms.

## Next

- Got a car to scan? → [contribute vehicle data](docs/contribute-vehicle-data.md)
- Writing a definition? → [definition files](docs/definition-files.md)
- Looking for a document? → [documentation index](docs/README.md)
