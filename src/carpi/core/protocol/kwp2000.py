"""Keyword Protocol 2000 (ISO 14230), read-only subset, as VAG uses it.

KWP2000 is what VAG modules speak before UDS arrived -- on a 2006 Passat, everything the
manufacturer's tool does goes through this. Structurally it is UDS's predecessor and the
family resemblance is strong: a service byte, a positive response of service + 0x40, and
negative responses of ``7F <service> <code>``.

Services implemented:

===== ==========================================================================
0x10  StartDiagnosticSession
0x18  ReadDiagnosticTroubleCodesByStatus -- manufacturer faults, any module
0x1A  ReadEcuIdentification -- part number, software coding, workshop code
0x21  ReadDataByLocalIdentifier -- *measuring blocks*, the heart of VCDS
0x22  ReadDataByCommonIdentifier
0x3E  TesterPresent
===== ==========================================================================

**The write and control services are absent**, on purpose, and asserted absent by tests:
ECUReset (0x11), ClearDiagnosticInformation (0x14), SecurityAccess (0x27),
WriteDataByCommonIdentifier (0x2E), InputOutputControl (0x2F/0x30), the routine services
(0x31-0x33, 0x38-0x3A), WriteDataByLocalIdentifier (0x3B), WriteMemoryByAddress (0x3D),
and the transfer services (0x34-0x37).

Coding lives in :mod:`carpi.coding`, which is a separate package that nothing in the
inspection path imports. That separation is the whole safety argument: a module that
cannot express a write cannot be talked into one.

Measuring blocks
----------------
``ReadDataByLocalIdentifier`` with a group number returns up to four fields, each a
formula byte plus two data bytes. The formula selects one of a long list of scaling
functions -- VAG's own, not in any standard. Only well-attested formulas are decoded here;
the rest are reported as raw bytes rather than guessed at, because a plausible wrong
number is worse than an honest unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from carpi.core.transport.base import Channel, NoResponse

__all__ = [
    "FORBIDDEN_SERVICES",
    "MeasuringBlock",
    "MeasuringValue",
    "KwpClient",
    "KwpError",
    "KwpNegativeResponse",
]

log = logging.getLogger(__name__)

SERVICE_START_SESSION = 0x10
SERVICE_READ_DTC_BY_STATUS = 0x18
SERVICE_READ_ECU_ID = 0x1A
SERVICE_READ_LOCAL_ID = 0x21
SERVICE_READ_COMMON_ID = 0x22
SERVICE_TESTER_PRESENT = 0x3E

NEGATIVE_RESPONSE = 0x7F
_POSITIVE_OFFSET = 0x40
_RESPONSE_PENDING = 0x78

# Diagnostic session types VAG uses. 0x89 is the one VCDS opens for ordinary work.
SESSION_DEFAULT = 0x81
SESSION_DIAGNOSTICS = 0x89
SESSION_ADAPTATION = 0x87

FORBIDDEN_SERVICES = {
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x27: "SecurityAccess",
    0x2E: "WriteDataByCommonIdentifier",
    0x2F: "InputOutputControlByCommonIdentifier",
    0x30: "InputOutputControlByLocalIdentifier",
    0x31: "StartRoutineByLocalIdentifier",
    0x32: "StopRoutineByLocalIdentifier",
    0x33: "RequestRoutineResultsByLocalIdentifier",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x38: "StartRoutineByAddress",
    0x39: "StopRoutineByAddress",
    0x3A: "RequestRoutineResultsByAddress",
    0x3B: "WriteDataByLocalIdentifier",
    0x3D: "WriteMemoryByAddress",
}

_NRC_MEANINGS = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x23: "routine not complete",
    0x31: "request out of range",
    0x33: "security access denied",
    0x35: "invalid key",
    0x36: "exceeded number of attempts",
    0x37: "required time delay not expired",
    0x40: "download not accepted",
    0x50: "upload not accepted",
    0x78: "request received, response pending",
    0x80: "service not supported in active session",
    0x9A: "data decompression failed",
    0x9B: "data decryption failed",
}

# Local identifiers with a fixed meaning across VAG modules.
LID_IDENTIFICATION = 0x80
LID_ADAPTATION = 0x0A

# VAG measuring-block scaling formulas, by formula byte. Only entries attested across
# multiple independent sources are here; an unknown formula reports raw bytes instead of a
# fabricated engineering value. See the module docstring.
#
# Each entry is (unit, function of the two data bytes a and b).
_FORMULAS: dict[int, tuple[str, Any]] = {
    0x01: ("rpm", lambda a, b: a * b * 0.2),
    0x02: ("%", lambda a, b: a * b * 0.002),
    0x03: ("deg", lambda a, b: a * b * 0.002),
    0x04: ("deg BTDC", lambda a, b: abs(b - 127) * 0.01 * max(a, 1)),
    0x05: ("degC", lambda a, b: a * (b - 100) * 0.1),
    0x06: ("V", lambda a, b: a * b * 0.001),
    0x07: ("km/h", lambda a, b: a * b * 0.01),
    0x0F: ("ms", lambda a, b: a * b * 0.01),
    0x12: ("mbar", lambda a, b: a * b * 0.04),
    0x15: ("V", lambda a, b: a * (b - 128) * 0.01),
    0x16: ("ms", lambda a, b: a * b * 0.01),
    0x21: ("%", lambda a, b: (b * 100.0 / a) if a else 0.0),
    0x22: ("kW", lambda a, b: (b - 128) * 0.01 * a),
    0x23: ("l/h", lambda a, b: a * b * 0.1),
    0x25: ("", lambda a, b: float(b)),
    0x31: ("mg/h", lambda a, b: a * b * 0.01),
    0x33: ("mg/h", lambda a, b: (b - 128) * 0.01 * a),
    0x35: ("deg", lambda a, b: (b - 128) * 0.01 * a),
    0x36: ("count", lambda a, b: float(a * 256 + b)),
}


class KwpError(Exception):
    """The exchange did not produce a usable answer."""


class KwpNegativeResponse(KwpError):
    """The module refused the request."""

    def __init__(self, service: int, nrc: int) -> None:
        self.service = service
        self.nrc = nrc
        meaning = _NRC_MEANINGS.get(nrc, "unknown reason")
        super().__init__(f"service 0x{service:02X} refused: 0x{nrc:02X} ({meaning})")

    @property
    def is_unsupported(self) -> bool:
        return self.nrc in (0x11, 0x12, 0x31, 0x80)

    @property
    def is_protected(self) -> bool:
        """Locked behind a login. On this era that is a code, not a cryptographic key."""
        return self.nrc in (0x33, 0x35, 0x36)


@dataclass(frozen=True)
class MeasuringValue:
    """One field of a measuring block."""

    formula: int
    a: int
    b: int
    value: float | None = None
    unit: str = ""

    @property
    def decoded(self) -> bool:
        """False when the scaling formula is not one this module claims to know."""
        return self.value is not None

    def __str__(self) -> str:
        if self.value is None:
            return f"formula 0x{self.formula:02X}: raw {self.a:02X} {self.b:02X} (not decoded)"
        shown = f"{self.value:.2f}".rstrip("0").rstrip(".")
        return f"{shown} {self.unit}".strip()

    def as_dict(self) -> dict[str, Any]:
        return {
            "formula": f"0x{self.formula:02X}",
            "raw": f"{self.a:02X}{self.b:02X}",
            "value": self.value,
            "unit": self.unit or None,
            "decoded": self.decoded,
        }


@dataclass(frozen=True)
class MeasuringBlock:
    """A group of up to four live values, as VCDS displays them."""

    group: int
    values: tuple[MeasuringValue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "group": self.group,
            "values": [value.as_dict() for value in self.values],
        }

    def __str__(self) -> str:
        return f"group {self.group:03d}: " + "  |  ".join(str(v) for v in self.values)


class KwpClient:
    """Read-only KWP2000 conversation with one module."""

    def __init__(self, channel: Channel, *, timeout: float = 1.0) -> None:
        self._channel = channel
        self._timeout = timeout

    @property
    def address(self) -> Any:
        return self._channel.address

    # --- plumbing -------------------------------------------------------------

    def _exchange(self, request: bytes, *, pending_retries: int = 2) -> bytes:
        service = request[0]
        if service in FORBIDDEN_SERVICES:
            # Unreachable through the public API. A backstop, so a refactor that routes a
            # write through here fails loudly instead of quietly changing somebody's car.
            raise KwpError(
                f"service 0x{service:02X} ({FORBIDDEN_SERVICES[service]}) is not "
                f"permitted here: this client is read-only. Coding lives in carpi.coding."
            )

        attempt = 0
        while True:
            reply = self._channel.request(request, timeout=self._timeout)
            if not reply:
                raise KwpError(f"empty reply to {request.hex(' ')}")

            if reply[0] == NEGATIVE_RESPONSE:
                if len(reply) < 3:
                    raise KwpError(f"truncated negative response: {reply.hex(' ')}")
                nrc = reply[2]
                if nrc == _RESPONSE_PENDING and attempt < pending_retries:
                    attempt += 1
                    continue
                raise KwpNegativeResponse(service=reply[1], nrc=nrc)

            expected = service + _POSITIVE_OFFSET
            if reply[0] != expected:
                raise KwpError(
                    f"expected 0x{expected:02X} in reply to 0x{service:02X}, got 0x{reply[0]:02X}"
                )
            return reply

    # --- services -------------------------------------------------------------

    def tester_present(self) -> bool:
        """Ping the module. A refusal still proves it is there."""
        try:
            self._exchange(bytes([SERVICE_TESTER_PRESENT, 0x01]))
        except KwpNegativeResponse:
            return True
        except (NoResponse, KwpError):
            return False
        return True

    def start_session(self, session: int = SESSION_DIAGNOSTICS) -> bool:
        """Open a diagnostic session. Returns False if the module declined.

        Adaptation and coding sessions are permitted to be *requested* here because they
        are read-capable too, but nothing in this client can write in any session.
        """
        try:
            self._exchange(bytes([SERVICE_START_SESSION, session]))
        except (KwpNegativeResponse, NoResponse, KwpError) as exc:
            log.debug("%s declined session 0x%02X: %s", self.address, session, exc)
            return False
        return True

    def identification(self) -> dict[str, Any]:
        """ReadEcuIdentification 0x1A 0x80 -- part number, coding, workshop code.

        This is what VCDS shows at the top of a module's page. The layout is VAG's rather
        than standardised, so the raw bytes are always included: if the field split below
        turns out wrong on a real car, the underlying data is still there to re-read.
        """
        try:
            reply = self._exchange(bytes([SERVICE_READ_ECU_ID, LID_IDENTIFICATION]))
        except (KwpNegativeResponse, NoResponse, KwpError) as exc:
            log.debug("%s: identification unavailable: %s", self.address, exc)
            return {}

        body = reply[2:]
        text = "".join(chr(byte) if 0x20 <= byte <= 0x7E else "." for byte in body)
        return {
            "raw": body.hex(),
            "text": text.strip(),
            # Not split into fields on purpose: the offsets vary between module families,
            # and a confidently mislabelled part number is worse than an honest blob.
            "note": "field layout is module-specific and not parsed; see raw",
        }

    def read_measuring_block(self, group: int) -> MeasuringBlock:
        """ReadDataByLocalIdentifier 0x21 -- one measuring block group.

        Groups are numbered as VCDS numbers them. What each field means is
        module-specific and lives in a vehicle definition, not here.
        """
        if not 0x00 <= group <= 0xFF:
            raise KwpError(f"measuring block group {group} is out of range")
        reply = self._exchange(bytes([SERVICE_READ_LOCAL_ID, group]))

        if len(reply) < 2 or reply[1] != group:
            got = f"{reply[1]}" if len(reply) > 1 else "nothing"
            raise KwpError(f"asked for group {group} but the reply echoed {got}")

        body = reply[2:]
        values: list[MeasuringValue] = []
        for offset in range(0, len(body) - 2, 3):
            formula, a, b = body[offset], body[offset + 1], body[offset + 2]
            values.append(_decode_value(formula, a, b))
        return MeasuringBlock(group=group, values=tuple(values))

    def read_measuring_blocks(self, groups: list[int]) -> dict[int, MeasuringBlock]:
        """Read several groups, skipping those the module will not provide."""
        found: dict[int, MeasuringBlock] = {}
        for group in groups:
            try:
                found[group] = self.read_measuring_block(group)
            except (KwpNegativeResponse, NoResponse, KwpError) as exc:
                log.debug("%s: group %d unavailable: %s", self.address, group, exc)
        return found

    def read_local_identifier(self, identifier: int) -> bytes:
        """Raw ReadDataByLocalIdentifier, for definitions that name an identifier."""
        reply = self._exchange(bytes([SERVICE_READ_LOCAL_ID, identifier]))
        if len(reply) < 2 or reply[1] != identifier:
            raise KwpError(f"reply did not echo identifier 0x{identifier:02X}")
        return reply[2:]

    def read_common_identifier(self, identifier: int) -> bytes:
        """ReadDataByCommonIdentifier 0x22, two-byte identifier."""
        request = bytes([SERVICE_READ_COMMON_ID, identifier >> 8, identifier & 0xFF])
        reply = self._exchange(request)
        if len(reply) < 3:
            raise KwpError(f"reply to 0x{identifier:04X} is too short")
        echoed = (reply[1] << 8) | reply[2]
        if echoed != identifier:
            raise KwpError(f"asked for 0x{identifier:04X} but the reply echoed 0x{echoed:04X}")
        return reply[3:]

    def read_dtcs(self, status: int = 0x00) -> list[str]:
        """ReadDiagnosticTroubleCodesByStatus 0x18.

        VAG fault codes are two bytes plus a status byte, and are conventionally shown as
        a five-digit decimal number -- 01314 rather than P0300 -- which is why they need
        their own presentation rather than reusing the OBD-II decoder.
        """
        request = bytes([SERVICE_READ_DTC_BY_STATUS, status, 0xFF, 0x00])
        try:
            reply = self._exchange(request)
        except KwpNegativeResponse as exc:
            if exc.is_unsupported:
                return []
            raise

        body = reply[1:]
        if not body:
            return []
        # First byte is the count of codes that follow, three bytes each.
        records = body[1:]
        codes: list[str] = []
        for offset in range(0, len(records) - 2, 3):
            high, low = records[offset], records[offset + 1]
            if high == 0xFF and low == 0xFF:
                continue
            if high == 0x00 and low == 0x00:
                continue
            codes.append(f"{(high << 8) | low:05d}")
        return codes


def _decode_value(formula: int, a: int, b: int) -> MeasuringValue:
    entry = _FORMULAS.get(formula)
    if entry is None:
        # Reported honestly rather than guessed. The raw bytes are what a contributor
        # needs in order to work the formula out against a known reference value.
        return MeasuringValue(formula=formula, a=a, b=b)
    unit, function = entry
    try:
        value = float(function(a, b))
    except (ArithmeticError, ValueError):
        return MeasuringValue(formula=formula, a=a, b=b)
    return MeasuringValue(formula=formula, a=a, b=b, value=value, unit=unit)
