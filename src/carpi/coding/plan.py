"""The safety layer around a write. This is where the rules are enforced.

A write is two-phase. :func:`build_plan` reads the current value, checks every
precondition, and returns a plan that changes nothing. :func:`apply_plan` performs it,
having first archived the old value to disk.

The rules, and why each one is here:

**Safety-critical modules are refused, and the refusal cannot be overridden.** Airbag,
ABS, steering, immobiliser and parking brake controllers are excluded. A wrong value in a
comfort module is an inconvenience; a wrong value in an airbag controller is somebody not
being protected in a crash, and they will not find out until it matters. No flag turns
this off, because a flag is exactly what gets passed at eleven at night.

**Nothing is written until the old value is archived.** If the restore point cannot be
written to disk, the write does not happen. A coding value you cannot get back to is the
difference between a mistake and a repair bill.

**Voltage and motion are checked.** A module interrupted mid-write by a dying battery is
the classic way to destroy one, so a low supply voltage aborts. So does a vehicle that is
moving.

**The operator types the module's name to confirm.** Not a y/n prompt. The point of
friction is to make it impossible to confirm without having read which module is about to
change.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from carpi.coding.session import CodingSession
from carpi.core.protocol.kwp2000 import KwpError, KwpNegativeResponse
from carpi.core.transport.tp20 import SAFETY_CRITICAL_MODULES, VAG_MODULES

__all__ = [
    "MINIMUM_VOLTAGE",
    "CodingPlan",
    "CodingRefused",
    "RestorePoint",
    "apply_plan",
    "build_plan",
    "load_restore_point",
    "restore_directory",
]

log = logging.getLogger(__name__)

# Below this, abort. A write interrupted by a collapsing supply is how a module is
# destroyed rather than merely misconfigured. Battery-only (engine off) sits near 12.4 V
# on a healthy car, so this permits coding with the engine off but not on a flat battery.
MINIMUM_VOLTAGE = 11.5

# Above this, something is wrong with the reading rather than the car.
MAXIMUM_VOLTAGE = 15.5


class CodingRefused(Exception):
    """A precondition was not met. The vehicle was not touched."""


def restore_directory() -> Path:
    """Where restore points are written.

    ``CARPI_RESTORE_DIR`` overrides it. Under the user's home by default rather than
    beside the code, so a reinstall cannot take the archive with it.
    """
    override = os.environ.get("CARPI_RESTORE_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".carpi" / "restore"


@dataclass(frozen=True)
class RestorePoint:
    """The value a module held before a write, and enough context to put it back."""

    created_at: str
    module_address: int
    module_name: str
    identifier: int
    previous: str
    intended: str
    vin: str | None = None
    login_code: int | None = None
    note: str | None = None

    @property
    def previous_bytes(self) -> bytes:
        return bytes.fromhex(self.previous)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "carpi.restore/1",
            "created_at": self.created_at,
            "module_address": f"0x{self.module_address:02X}",
            "module_name": self.module_name,
            "identifier": f"0x{self.identifier:02X}",
            "previous": self.previous,
            "intended": self.intended,
            "vin": self.vin,
            "login_code": self.login_code,
            "note": self.note,
        }

    def filename(self) -> str:
        stamp = self.created_at.replace(":", "").replace("-", "")
        return f"{stamp}-{self.module_address:02X}-{self.identifier:02X}.json"

    def write(self, directory: Path | None = None) -> Path:
        """Archive to disk, returning the path. Raises if it cannot be written.

        Failing loudly is the point: a write with no way back is not permitted, so an
        unwritable archive has to stop the operation rather than be logged and ignored.
        """
        target_dir = directory or restore_directory()
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / self.filename()
            path.write_text(json.dumps(self.as_dict(), indent=2) + "\n", encoding="utf-8")
        except OSError as exc:
            raise CodingRefused(
                f"could not write a restore point to {target_dir}: {exc}. Nothing was "
                f"written to the vehicle -- a coding change you cannot undo is not "
                f"permitted."
            ) from exc
        return path


def load_restore_point(path: Path) -> RestorePoint:
    """Read a restore point back, for undoing a change."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodingRefused(f"could not read {path}: {exc}") from exc
    if document.get("schema") != "carpi.restore/1":
        raise CodingRefused(f"{path} is not a car-pi restore point")
    return RestorePoint(
        created_at=document["created_at"],
        module_address=int(document["module_address"], 16),
        module_name=document["module_name"],
        identifier=int(document["identifier"], 16),
        previous=document["previous"],
        intended=document["intended"],
        vin=document.get("vin"),
        login_code=document.get("login_code"),
        note=document.get("note"),
    )


@dataclass
class CodingPlan:
    """A checked, not-yet-applied change."""

    module_address: int
    module_name: str
    identifier: int
    previous: bytes
    intended: bytes
    vin: str | None = None
    login_code: int | None = None
    warnings: tuple[str, ...] = ()
    voltage: float | None = None
    checks: dict[str, Any] = field(default_factory=dict)

    @property
    def is_noop(self) -> bool:
        """True when the module already holds the intended value."""
        return self.previous == self.intended

    @property
    def confirmation_phrase(self) -> str:
        """What the operator must type. The module name, so it has to have been read."""
        return self.module_name

    def diff_lines(self) -> list[str]:
        """A byte-by-byte before and after, with changed positions marked."""
        width = max(len(self.previous), len(self.intended))
        before = self.previous.ljust(width, b"\x00")
        after = self.intended.ljust(width, b"\x00")
        lines = [
            f"  before: {self.previous.hex(' ')}",
            f"  after:  {self.intended.hex(' ')}",
        ]
        changed = [index for index in range(width) if before[index] != after[index]]
        if changed:
            marks = "         " + " ".join(
                "^^" if index in changed else "  " for index in range(width)
            )
            lines.append(marks)
            for index in changed:
                lines.append(
                    f"  byte {index}: 0x{before[index]:02X} -> 0x{after[index]:02X} "
                    f"({before[index]:08b} -> {after[index]:08b})"
                )
        return lines

    def as_dict(self) -> dict[str, Any]:
        return {
            "module_address": f"0x{self.module_address:02X}",
            "module_name": self.module_name,
            "identifier": f"0x{self.identifier:02X}",
            "previous": self.previous.hex(),
            "intended": self.intended.hex(),
            "is_noop": self.is_noop,
            "vin": self.vin,
            "voltage": self.voltage,
            "warnings": list(self.warnings),
            "checks": self.checks,
        }

    def restore_point(self) -> RestorePoint:
        return RestorePoint(
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            module_address=self.module_address,
            module_name=self.module_name,
            identifier=self.identifier,
            previous=self.previous.hex(),
            intended=self.intended.hex(),
            vin=self.vin,
            login_code=self.login_code,
        )


def build_plan(
    session: CodingSession,
    *,
    module_address: int,
    identifier: int,
    intended: bytes,
    vin: str | None = None,
    login_code: int | None = None,
    voltage: float | None = None,
    vehicle_speed: float | None = None,
) -> CodingPlan:
    """Check every precondition and read the current value. Changes nothing.

    Raises :class:`CodingRefused` if the change must not proceed. *voltage* and
    *vehicle_speed* come from a generic OBD-II read on the same vehicle; pass them, because
    omitting them only removes a safeguard.
    """
    name = VAG_MODULES.get(module_address, f"module 0x{module_address:02X}")

    # First, and not overridable.
    if module_address in SAFETY_CRITICAL_MODULES:
        raise CodingRefused(
            f"refusing to write to {name} (0x{module_address:02X}). Airbag, ABS, steering, "
            f"immobiliser and parking-brake controllers are excluded from coding, and there "
            f"is no flag to override it. A wrong value here is not an inconvenience; it is "
            f"somebody unprotected in a crash, and they will not find out until it matters. "
            f"If you genuinely need to change one, use equipment intended for it and accept "
            f"the consequences knowingly."
        )

    if not intended:
        raise CodingRefused("no value given to write")
    if len(intended) > 32:
        raise CodingRefused(
            f"{len(intended)} bytes is longer than any coding value on this platform; "
            f"check the value is what you meant"
        )

    warnings: list[str] = []
    checks: dict[str, Any] = {"safety_critical": False}

    if voltage is not None:
        checks["voltage"] = voltage
        if voltage < MINIMUM_VOLTAGE:
            raise CodingRefused(
                f"supply voltage is {voltage:.2f} V, below the {MINIMUM_VOLTAGE} V minimum. "
                f"A module interrupted part-way through a write by a collapsing supply is "
                f"the usual way one is destroyed. Charge the battery, or run the engine, "
                f"and try again."
            )
        if voltage > MAXIMUM_VOLTAGE:
            raise CodingRefused(
                f"supply voltage reads {voltage:.2f} V, which is implausible. Fix the "
                f"charging system or the reading before writing anything."
            )
    else:
        warnings.append("supply voltage was not checked; pass --voltage or let the tool read it")

    if vehicle_speed is not None:
        checks["vehicle_speed"] = vehicle_speed
        if vehicle_speed > 0:
            raise CodingRefused(
                f"the vehicle is moving at {vehicle_speed:.0f} km/h. Coding is done stationary."
            )
    else:
        warnings.append("vehicle speed was not checked")

    if not session.logged_in:
        warnings.append("not logged in yet; apply will require it")

    # Read the current value last, so nothing is sent to the module until the local checks
    # have all passed.
    try:
        previous = session.read_raw(identifier)
    except KwpNegativeResponse as exc:
        if exc.is_protected:
            raise CodingRefused(
                f"{name} will not disclose identifier 0x{identifier:02X} without a login. "
                f"Log in first: the current value has to be readable, because it is what "
                f"gets archived before the change."
            ) from exc
        raise CodingRefused(
            f"{name} refused to read identifier 0x{identifier:02X}: {exc}. Without the "
            f"current value there is no restore point, so the write cannot proceed."
        ) from exc
    except KwpError as exc:
        raise CodingRefused(
            f"could not read identifier 0x{identifier:02X} from {name}: {exc}"
        ) from exc

    if len(intended) != len(previous):
        warnings.append(
            f"the module holds {len(previous)} byte(s) but {len(intended)} are to be "
            f"written; confirm this is the right length for this identifier"
        )

    return CodingPlan(
        module_address=module_address,
        module_name=name,
        identifier=identifier,
        previous=previous,
        intended=bytes(intended),
        vin=vin,
        login_code=login_code,
        warnings=tuple(warnings),
        voltage=voltage,
        checks=checks,
    )


def apply_plan(
    session: CodingSession,
    plan: CodingPlan,
    *,
    confirmation: str,
    restore_dir: Path | None = None,
) -> tuple[Path, bool]:
    """Archive, write, and read back. Returns the restore-point path and whether it took.

    *confirmation* must equal :attr:`CodingPlan.confirmation_phrase` -- the module's name.
    A y/n prompt can be answered without reading it; typing the name cannot.
    """
    if plan.is_noop:
        raise CodingRefused(
            f"{plan.module_name} already holds {plan.intended.hex(' ')}; nothing to do"
        )

    if confirmation.strip().casefold() != plan.confirmation_phrase.casefold():
        raise CodingRefused(
            f"confirmation did not match. To proceed, type the module name exactly: "
            f"{plan.confirmation_phrase!r}"
        )

    if plan.module_address in SAFETY_CRITICAL_MODULES:
        # Checked again here. build_plan already refused, but a plan can be constructed by
        # other means, and this is the last gate before the bus.
        raise CodingRefused(f"{plan.module_name} is safety-critical and cannot be written to")

    if not session.logged_in:
        raise CodingRefused("not logged in; call session.login() with the module's code first")

    # Archived before anything is sent. Raises if it cannot be written.
    path = plan.restore_point().write(restore_dir)
    log.warning("restore point written to %s", path)

    session.write_raw(plan.identifier, plan.intended)
    took = session.verify(plan.identifier, plan.intended)
    if not took:
        log.error(
            "%s accepted the write but reads back differently; restore point at %s",
            plan.module_name,
            path,
        )
    return path, took
