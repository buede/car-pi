"""Manufacturer-specific reads, driven by definition files.

A vehicle profile says which modules a platform has, where to reach them, and which
data identifiers hold what. Everything is UDS ``ReadDataByIdentifier``, so a profile can
only ever read; the schema has no way to express a write.

Selecting a profile
-------------------
By VIN prefix. The first three characters of a VIN are the World Manufacturer
Identifier, so ``WVW`` is Volkswagen and ``JT`` is Toyota, and a longer prefix can
narrow to a platform. A profile with no ``match`` block is never selected automatically.

Why the shipped set is nearly empty
-----------------------------------
Manufacturer identifiers cannot be verified without the car in front of you. A wrong
odometer identifier does not fail loudly -- it returns four plausible bytes that decode
to a plausible mileage, and somebody buys a car on the strength of it. So the real make
directories start empty and are filled from :mod:`carpi.core.didscan` output confirmed
against a vehicle whose true state was independently known.

The one profile that ships describes the simulator and is marked ``fictional``, which
excludes it from ever matching a real vehicle. It exists to prove the machinery works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from carpi.core.protocol.uds import DiagnosticSession, UdsClient, UdsError, UdsNegativeResponse
from carpi.core.transport.base import EcuAddress, NoResponse

__all__ = [
    "DecodeSpec",
    "EcuProfile",
    "ModuleReading",
    "VehicleProfile",
    "VehicleRead",
    "read_module",
]

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class DecodeSpec:
    """How to turn payload bytes into a value."""

    type: str
    offset: int = 0
    length: int | None = None
    scale: float = 1.0
    add: float = 0.0
    value_range: tuple[float, float] | None = None

    def decode(self, payload: bytes) -> Any:
        """Decode *payload*, raising :class:`ValueError` if it is too short.

        Short payloads raise rather than pad. A definition that expects three bytes and
        gets two is wrong about something, and quietly zero-extending would turn that
        into a confidently reported number.
        """
        end = None if self.length is None else self.offset + self.length
        window = payload[self.offset : end]
        needed = self.length if self.length is not None else 1
        if len(window) < needed:
            raise ValueError(
                f"need {needed} byte(s) at offset {self.offset}, payload has "
                f"{len(payload)} in total"
            )

        if self.type == "raw":
            return window.hex()
        if self.type == "ascii":
            return window.decode("ascii", errors="replace").strip("\x00 ")
        if self.type == "bcd":
            # Binary-coded decimal: each nibble is one decimal digit. Used for dates
            # and part numbers, where 0x20 means 20 rather than 32.
            digits = "".join(f"{byte >> 4}{byte & 0x0F}" for byte in window)
            return digits
        signed = self.type == "int"
        number = int.from_bytes(window, "big", signed=signed)
        return number * self.scale + self.add

    def plausible(self, value: Any) -> bool:
        if self.value_range is None or not isinstance(value, int | float):
            return True
        low, high = self.value_range
        return low <= float(value) <= high


@dataclass(frozen=True)
class VehicleRead:
    """One identifier to read from one module."""

    id: str
    did: int
    decode: DecodeSpec
    label: str | None = None
    unit: str | None = None
    confidence: str = "community"
    verified_on: tuple[str, ...] = ()
    note: str | None = None

    @property
    def display(self) -> str:
        return self.label or self.id.replace("_", " ")


@dataclass(frozen=True)
class EcuProfile:
    """One module of a platform."""

    name: str
    request_id: int
    response_id: int
    reads: tuple[VehicleRead, ...]
    extended: bool = False
    session: str = "extended"
    safety_critical: bool = False

    @property
    def address(self) -> EcuAddress:
        return EcuAddress(
            tx_id=self.request_id,
            rx_id=self.response_id,
            extended=self.extended,
            name=self.name,
        )

    @property
    def session_id(self) -> int:
        return (
            DiagnosticSession.DEFAULT if self.session == "default" else DiagnosticSession.EXTENDED
        )


@dataclass(frozen=True)
class VehicleProfile:
    """Manufacturer-specific definitions for one platform."""

    id: str
    make: str
    platform: str
    confidence: str
    ecus: tuple[EcuProfile, ...]
    vin_prefixes: tuple[str, ...] = ()
    years: tuple[int, int] | None = None
    fictional: bool = False
    source: str | None = None
    note: str | None = None

    @property
    def label(self) -> str:
        return f"{self.make} {self.platform}"

    def matches_vin(self, vin: str | None) -> bool:
        """Whether this profile applies to *vin*.

        A fictional profile never matches a real vehicle, whatever its prefixes say.
        Otherwise a simulator fixture could be selected for somebody's actual car and
        its invented identifiers reported as that car's data.
        """
        if self.fictional or not vin or not self.vin_prefixes:
            return False
        upper = vin.upper()
        return any(upper.startswith(prefix.upper()) for prefix in self.vin_prefixes)


@dataclass
class ModuleReading:
    """What one module returned for a profile's reads."""

    ecu: EcuProfile
    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, str] = field(default_factory=dict)
    implausible: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    protected: tuple[str, ...] = ()
    reached: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ecu": self.ecu.name,
            "address": self.ecu.address.label,
            "reached": self.reached,
            "values": self.values,
            "raw": self.raw,
            "implausible": list(self.implausible),
            "unavailable": list(self.unavailable),
            "protected": list(self.protected),
        }


def read_module(
    client: UdsClient,
    profile: EcuProfile,
    *,
    on_progress: Any = None,
) -> ModuleReading:
    """Perform a profile's reads against one module.

    Each read is independently guarded. A definition with one wrong identifier should
    still yield the others, because a partly-correct contribution is worth having.
    """
    reading = ModuleReading(ecu=profile)

    if not client.tester_present():
        log.debug("%s did not answer at %s", profile.name, profile.address)
        return reading
    reading.reached = True

    # Manufacturer identifiers usually need the extended session; asking for it in the
    # default session returns securityAccessDenied or serviceNotSupportedInActiveSession
    # and looks exactly like an identifier that does not exist.
    client.start_session(profile.session_id)

    implausible: list[str] = []
    unavailable: list[str] = []
    protected: list[str] = []

    for read in profile.reads:
        if on_progress is not None:
            on_progress(f"{profile.name}: reading {read.display}")
        try:
            payload = client.read_did(read.did)
        except UdsNegativeResponse as exc:
            (protected if exc.is_protected else unavailable).append(read.id)
            continue
        except (NoResponse, UdsError) as exc:
            log.debug("%s: %s (DID 0x%04X) failed: %s", profile.name, read.id, read.did, exc)
            unavailable.append(read.id)
            continue

        reading.raw[read.id] = payload.hex()
        try:
            value = read.decode.decode(payload)
        except ValueError as exc:
            # The identifier answered but not with what the definition expected, which
            # says the definition is wrong. Recorded, not guessed at.
            log.debug("%s: %s decode failed: %s", profile.name, read.id, exc)
            unavailable.append(read.id)
            continue

        if not read.decode.plausible(value):
            # Kept out of `values` so a wrong definition cannot manufacture a finding.
            implausible.append(read.id)
            continue
        reading.values[read.id] = value

    reading.implausible = tuple(implausible)
    reading.unavailable = tuple(unavailable)
    reading.protected = tuple(protected)
    return reading
