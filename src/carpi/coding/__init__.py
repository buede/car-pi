"""Writing to a vehicle. Kept apart from everything else on purpose.

This is the only package in car-pi that can change a car. Everything else -- the OBD-II
client, the UDS client, the KWP2000 client, the scan path, the server -- is structurally
incapable of it: those modules refuse to emit a write service, and tests assert no
simulated module ever receives one.

Two rules hold that separation up, and both are enforced by tests rather than by
convention:

**Nothing in the inspection path imports this package.** ``tests/test_coding.py`` asserts
that ``carpi.core``, ``carpi.report`` and ``carpi.sim`` have no path to it. If you find
yourself needing to import ``carpi.coding`` from one of those, the design has gone wrong.

**This is never reachable over the network.** ``carpi.server`` must not import it. The
server has no authentication, which is defensible for a read-only tool on its own hotspot
and indefensible the moment a write is possible: the failure mode changes from "somebody
on your hotspot reads your fuel trims" to "somebody on your hotspot reconfigures your
ABS". Coding is CLI-only, at a keyboard, by whoever owns the car.

What it can and cannot do
-------------------------
Coding on the KWP2000 era is genuinely feasible: a login is a five-digit code compared
by the module, not a cryptographic seed/key exchange, and a coding value is a handful of
bytes written by ``WriteDataByLocalIdentifier``. That is why this is possible on a 2006
Passat and impossible on a 2021 one, where SFD requires a token signed by VW.

Feasible is not the same as safe. Writing a wrong value to a module can leave it
unusable, and some modules should not be written at all from a hobby tool. So:

* Safety-critical modules -- airbag, ABS, steering, immobiliser, parking brake -- are
  refused outright, and the refusal is not overridable by a flag.
* Every write is preceded by reading and archiving the current value to a restore point
  on disk. If the archive cannot be written, the write does not happen.
* Applying is two-phase. ``plan`` shows a decoded before-and-after and changes nothing;
  ``apply`` requires the operator to type the module's name back.
* Supply voltage and vehicle speed are checked first. A module interrupted mid-write by a
  dying battery is the classic way to destroy one.
"""

from carpi.coding.plan import (
    CodingPlan,
    CodingRefused,
    RestorePoint,
    apply_plan,
    build_plan,
    load_restore_point,
)
from carpi.coding.session import CodingSession, LoginFailed

__all__ = [
    "CodingPlan",
    "CodingRefused",
    "CodingSession",
    "LoginFailed",
    "RestorePoint",
    "apply_plan",
    "build_plan",
    "load_restore_point",
]
