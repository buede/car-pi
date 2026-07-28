# Words and abbreviations

**For:** anyone reading the other documents who hits a term they do not know.
**You need:** nothing.
**Time:** look up one word, or skim in five minutes.

Car diagnostics uses a lot of three-letter abbreviations. Several look alike and mean
completely different things. This page exists so you never have to guess.

**On this page**
- Three that look alike
- Mode numbers
- Everything else

## Three that look alike

These three appear in the same paragraphs and differ by one letter. They are unrelated.

| Term | What it means | Think of it as |
|---|---|---|
| **DTC** | Diagnostic Trouble Code. A stored fault, like `P0300`. | A complaint the car has recorded |
| **PID** | Parameter ID. A numbered live reading, like engine speed. | A gauge you can ask for |
| **DID** | Data Identifier. A numbered stored value, like the odometer or the VIN. | A field you can read |

The short version: a **DTC** is something wrong, a **PID** is something changing, a
**DID** is something written down.

## Mode numbers

Generic diagnostics groups its requests into numbered modes. You will see these as bare
hex numbers.

| Mode | What it gives you |
|---|---|
| `01` | Live readings now — engine speed, coolant temperature, fuel trims |
| `02` | Freeze frames — the readings captured when a fault was stored |
| `06` | Self-test results with numbers, so you see how close something is to failing |
| `09` | Identification — the VIN, plus calibration IDs and CVNs |
| `0A` | Permanent fault codes, which no scan tool can erase |

## Everything else

| Term | What it means | Where it shows up |
|---|---|---|
| **bitrate** | How fast the bus runs. Cars use 500000 or 250000. | `ip link` setup, wrong value means silence |
| **CAN** | The two-wire network the modules talk over. | All wiring |
| **CAN FD** | A faster, newer version of CAN. Roughly 2019 onward. | Choosing hardware |
| **candump** | A Linux command that prints raw bus traffic. | Checking a bus is alive |
| **coding** | Changing a module's configuration value. | `carpi coding` |
| **CVN** | Calibration Verification Number. A checksum of the engine software. | Spotting a remap |
| **DPF** | Diesel Particulate Filter. Traps soot, and is expensive to replace. | Diesel checks |
| **EOBD** | The European name for OBD-II. Same thing. | Standards talk |
| **freeze frame** | The snapshot of conditions saved when a fault was stored. | Mode `02` |
| **J1962** | The official name for the 16-pin socket in the car. | Buying a cable |
| **KWP2000** | The older diagnostic language, before UDS. | VW and Audi, 2001–2010 |
| **ISO-TP** | The standard way of splitting a long message across CAN frames. | Nearly every car |
| **listen-only** | An interface mode that physically cannot transmit. | Safe bus checking |
| **logical address** | VW's module numbering. `0x17` is the instrument cluster. | `carpi vag` |
| **measuring block** | A numbered group of live values on a KWP2000 module. | `carpi vag blocks` |
| **MIL** | Malfunction Indicator Lamp. The engine warning light. | Report output |
| **module** | One of the small computers in the car. Also called an ECU. | Everywhere |
| **NRC** | Negative Response Code. A module's reason for refusing. | `0x33` means protected |
| **OBD-II** | The standard every car since about 2008 must support. | Any car |
| **readiness monitor** | A self-test the car runs while driving. Incomplete means it has not finished. | Spotting a recent code wipe |
| **SFD** | VW's signed-token protection on newer cars. Blocks coding entirely. | Roughly 2020 onward |
| **SocketCAN** | Linux's built-in CAN support. Linux only. | Real cars |
| **TP2.0** | VW's own transport, used instead of ISO-TP for its own diagnostics. | VW and Audi, 2001–2010 |
| **UDS** | The modern diagnostic language, ISO 14229. | Reaching modules OBD-II cannot |
| **vcan** | A fake CAN interface in the Linux kernel, for testing without hardware. | Running the test suite |
| **VIN** | Vehicle Identification Number. Identifies one specific car, and its owner. | Never publish it |

> **Careful:** a VIN identifies a real car and the person who owns it. Use `--anonymise`
> before you share any scan publicly.

## Next

- Want the exact command for something? → [command reference](commands.md)
- Wondering what any of this finds? → [what it can find](what-it-can-find.md)
