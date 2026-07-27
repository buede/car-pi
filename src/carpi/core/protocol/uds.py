"""Unified Diagnostic Services, ISO 14229-1. Read-only subset.

UDS is where the interesting data lives. Generic OBD-II is an emissions mandate and
exposes only what regulators required; UDS is the protocol the manufacturer's own tool
speaks, reaching every module on the vehicle rather than the eight the law names.

Services implemented here:

===== ==========================================================================
0x10  DiagnosticSessionControl -- default and extended sessions only
0x22  ReadDataByIdentifier -- the workhorse
0x19  ReadDTCInformation -- manufacturer fault codes, from any module
0x3E  TesterPresent -- keeps a session alive, and probes whether a module exists
===== ==========================================================================

**Every service that changes the vehicle is deliberately absent**, and that absence is
asserted by a test that watches the wire. Not implemented, and not to be added to this
module: WriteDataByIdentifier (0x2E), ClearDiagnosticInformation (0x14), ECUReset
(0x11), SecurityAccess (0x27), RoutineControl (0x31), InputOutputControlByIdentifier
(0x2F), and the transfer services (0x34-0x37).

If coding is ever built, it belongs in a separate module that the inspection path does
not import. Keeping the write services out of reach is worth more than the convenience
of having them nearby: an inspection tool that *cannot* modify a car is one you can
hand to somebody without a briefing.

A note on ``NRC 0x33 securityAccessDenied``: it is a *successful* discovery. It means
the identifier exists and holds something the manufacturer chose to protect, which is
strictly more informative than the silence an unsupported identifier returns.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from carpi.core.transport.base import Channel, NoResponse

__all__ = [
    "DTC_STATUS_BITS",
    "NEGATIVE_RESPONSE",
    "STANDARD_DIDS",
    "DiagnosticSession",
    "UdsClient",
    "UdsDtc",
    "UdsError",
    "UdsNegativeResponse",
]

log = logging.getLogger(__name__)

SERVICE_SESSION_CONTROL = 0x10
SERVICE_READ_DTC = 0x19
SERVICE_READ_DATA_BY_ID = 0x22
SERVICE_TESTER_PRESENT = 0x3E

NEGATIVE_RESPONSE = 0x7F
_POSITIVE_OFFSET = 0x40
_RESPONSE_PENDING = 0x78

# Services this module refuses to emit. Listed so the intent is explicit in code as
# well as in prose, and so the test suite can assert none of them is reachable.
FORBIDDEN_SERVICES = {
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x27: "SecurityAccess",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
}


class DiagnosticSession:
    """Session types this client will request. Programming sessions are not included."""

    DEFAULT = 0x01
    EXTENDED = 0x03


_NRC_MEANINGS = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length or invalid format",
    0x14: "response too long",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x24: "request sequence error",
    0x25: "no response from subnet component",
    0x26: "failure prevents execution of requested action",
    0x31: "request out of range",
    0x33: "security access denied",
    0x34: "authentication required",
    0x35: "invalid key",
    0x36: "exceeded number of attempts",
    0x37: "required time delay not expired",
    0x70: "upload/download not accepted",
    0x71: "transfer data suspended",
    0x72: "general programming failure",
    0x73: "wrong block sequence counter",
    0x78: "request received, response pending",
    0x7E: "sub-function not supported in active session",
    0x7F: "service not supported in active session",
    0x81: "RPM too high",
    0x82: "RPM too low",
    0x83: "engine is running",
    0x84: "engine is not running",
    0x85: "engine run time too low",
    0x86: "temperature too high",
    0x87: "temperature too low",
    0x88: "vehicle speed too high",
    0x89: "vehicle speed too low",
    0x8A: "throttle pedal too high",
    0x8B: "throttle pedal too low",
    0x8C: "transmission range not in neutral",
    0x8D: "transmission range not in gear",
    0x8F: "brake switch not closed",
    0x90: "shifter lever not in park",
    0x91: "torque converter clutch locked",
    0x92: "voltage too high",
    0x93: "voltage too low",
}

# ISO 14229-1 Table D.2. Bit 3, confirmedDTC, is the one that corresponds to a fault
# the module has actually committed to rather than merely observed once.
DTC_STATUS_BITS: tuple[tuple[int, str], ...] = (
    (0x01, "test_failed"),
    (0x02, "test_failed_this_cycle"),
    (0x04, "pending"),
    (0x08, "confirmed"),
    (0x10, "test_not_completed_since_clear"),
    (0x20, "test_failed_since_clear"),
    (0x40, "test_not_completed_this_cycle"),
    (0x80, "warning_indicator_requested"),
)

# ISO 14229-1 Annex C, the standardised identification block. Every one of these is in
# the standard rather than reverse-engineered, which is why they can ship as fact.
STANDARD_DIDS: dict[int, str] = {
    0xF180: "boot_software_identification",
    0xF181: "application_software_identification",
    0xF182: "application_data_identification",
    0xF186: "active_diagnostic_session",
    0xF187: "manufacturer_spare_part_number",
    0xF188: "manufacturer_ecu_software_number",
    0xF189: "manufacturer_ecu_software_version",
    0xF18A: "system_supplier_identifier",
    0xF18B: "ecu_manufacturing_date",
    0xF18C: "ecu_serial_number",
    0xF18E: "manufacturer_kit_assembly_part_number",
    0xF190: "vin",
    0xF191: "manufacturer_ecu_hardware_number",
    0xF192: "supplier_ecu_hardware_number",
    0xF193: "supplier_ecu_hardware_version",
    0xF194: "supplier_ecu_software_number",
    0xF195: "supplier_ecu_software_version",
    0xF197: "system_name_or_engine_type",
    0xF198: "repair_shop_code_or_tester_serial",
    0xF199: "programming_date",
    0xF19A: "calibration_repair_shop_code",
    0xF19B: "calibration_date",
    0xF19D: "ecu_installation_date",
}

_LETTERS = "PCBU"


class UdsError(Exception):
    """The exchange did not produce a usable answer."""


class UdsNegativeResponse(UdsError):
    """The module refused the request."""

    def __init__(self, service: int, nrc: int) -> None:
        self.service = service
        self.nrc = nrc
        meaning = _NRC_MEANINGS.get(nrc, "unknown reason")
        super().__init__(f"service 0x{service:02X} refused: 0x{nrc:02X} ({meaning})")

    @property
    def is_unsupported(self) -> bool:
        """The module does not implement this at all."""
        return self.nrc in (0x11, 0x12, 0x31, 0x7E, 0x7F)

    @property
    def is_protected(self) -> bool:
        """The identifier exists but is locked.

        Worth distinguishing from unsupported: this is a positive finding about what
        the module holds, and it is what a DID sweep is really looking for.
        """
        return self.nrc in (0x33, 0x34)

    @property
    def needs_different_conditions(self) -> bool:
        """Refused because of vehicle state -- engine running, speed, voltage."""
        return self.nrc in (0x22, 0x24, 0x81, 0x82, 0x83, 0x84, 0x85, 0x92, 0x93)


@dataclass(frozen=True)
class UdsDtc:
    """One manufacturer fault code, three bytes plus a status byte.

    UDS codes carry a failure-type byte the two-byte OBD-II format has no room for,
    which is why they are shown as ``P0420-08``: the same fault, with the specific
    failure mode the module recorded.
    """

    high: int
    middle: int
    low: int
    status: int

    @property
    def code(self) -> str:
        """The code in the conventional ``P0420-08`` form."""
        letter = _LETTERS[(self.high >> 6) & 0x03]
        first = (self.high >> 4) & 0x03
        return (
            f"{letter}{first}{self.high & 0x0F:X}"
            f"{self.middle >> 4:X}{self.middle & 0x0F:X}"
            f"-{self.low:02X}"
        )

    @property
    def flags(self) -> tuple[str, ...]:
        """Which status bits are set, by name."""
        return tuple(name for bit, name in DTC_STATUS_BITS if self.status & bit)

    @property
    def confirmed(self) -> bool:
        """The module has committed to this fault, not merely seen it once."""
        return bool(self.status & 0x08)

    @property
    def warning_requested(self) -> bool:
        """The module is asking for a dashboard warning."""
        return bool(self.status & 0x80)

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "status": self.status,
            "flags": list(self.flags),
            "confirmed": self.confirmed,
            "warning_requested": self.warning_requested,
        }

    def __str__(self) -> str:
        return f"{self.code} [{', '.join(self.flags) or 'no flags'}]"


class UdsClient:
    """Read-only UDS conversation with one module."""

    def __init__(self, channel: Channel, *, timeout: float = 1.0) -> None:
        self._channel = channel
        self._timeout = timeout

    @property
    def address(self) -> Any:
        return self._channel.address

    # --- plumbing -------------------------------------------------------------

    def _exchange(self, request: bytes, *, pending_retries: int = 2) -> bytes:
        """Send *request* and return a validated positive response."""
        service = request[0]
        if service in FORBIDDEN_SERVICES:
            # Unreachable through this module's public API. Present as a backstop, so
            # that a future refactor which routes a write through here fails loudly
            # rather than quietly modifying somebody's car.
            raise UdsError(
                f"service 0x{service:02X} ({FORBIDDEN_SERVICES[service]}) is not "
                f"permitted: car-pi's inspection path is read-only"
            )

        attempt = 0
        while True:
            reply = self._channel.request(request, timeout=self._timeout)
            if not reply:
                raise UdsError(f"empty reply to {request.hex(' ')}")

            if reply[0] == NEGATIVE_RESPONSE:
                if len(reply) < 3:
                    raise UdsError(f"truncated negative response: {reply.hex(' ')}")
                nrc = reply[2]
                # Modules commonly use 0x78 while they fetch something slow. It is a
                # promise of a later answer, not a refusal.
                if nrc == _RESPONSE_PENDING and attempt < pending_retries:
                    attempt += 1
                    log.debug("%s: response pending on 0x%02X", self.address, service)
                    continue
                raise UdsNegativeResponse(service=reply[1], nrc=nrc)

            expected = service + _POSITIVE_OFFSET
            if reply[0] != expected:
                raise UdsError(
                    f"expected 0x{expected:02X} in reply to 0x{service:02X}, got 0x{reply[0]:02X}"
                )
            return reply

    # --- 0x3E TesterPresent ---------------------------------------------------

    def tester_present(self) -> bool:
        """Ping the module. True if it answered at all, negatively or otherwise.

        The most benign request in UDS, which makes it the right probe for finding out
        whether anything is listening on an address.
        """
        try:
            self._exchange(bytes([SERVICE_TESTER_PRESENT, 0x00]))
        except UdsNegativeResponse:
            # A refusal still proves somebody is home, which is all this asks.
            return True
        except (NoResponse, UdsError):
            return False
        return True

    # --- 0x10 DiagnosticSessionControl ---------------------------------------

    def start_session(self, session: int = DiagnosticSession.EXTENDED) -> bool:
        """Request a diagnostic session. Returns False if the module declined.

        Only default and extended sessions are permitted. A programming session is the
        gateway to reflashing and has no place in a read-only tool.
        """
        if session not in (DiagnosticSession.DEFAULT, DiagnosticSession.EXTENDED):
            raise UdsError(
                f"session 0x{session:02X} is not permitted; only default (0x01) and "
                f"extended (0x03) are, and neither can modify the vehicle"
            )
        try:
            self._exchange(bytes([SERVICE_SESSION_CONTROL, session]))
        except (UdsNegativeResponse, NoResponse, UdsError) as exc:
            log.debug("%s: session 0x%02X declined: %s", self.address, session, exc)
            return False
        return True

    # --- 0x22 ReadDataByIdentifier -------------------------------------------

    def read_did(self, did: int) -> bytes:
        """Read one data identifier. Returns the raw payload."""
        if not 0 <= did <= 0xFFFF:
            raise UdsError(f"data identifier 0x{did:X} is out of range")

        request = bytes([SERVICE_READ_DATA_BY_ID, did >> 8, did & 0xFF])
        reply = self._exchange(request)

        # Verify the module echoed the identifier asked for. Without this a single
        # timeout puts every later read one exchange out of step, and the values are
        # then attributed to the wrong identifiers -- silently.
        if len(reply) < 3:
            raise UdsError(f"reply to DID 0x{did:04X} is too short: {reply.hex(' ')}")
        echoed = (reply[1] << 8) | reply[2]
        if echoed != did:
            raise UdsError(f"asked for DID 0x{did:04X} but the reply echoed 0x{echoed:04X}")
        return reply[3:]

    def read_dids(self, dids: list[int]) -> dict[int, bytes]:
        """Read several identifiers, skipping those the module will not provide."""
        found: dict[int, bytes] = {}
        for did in dids:
            try:
                found[did] = self.read_did(did)
            except (UdsNegativeResponse, NoResponse, UdsError) as exc:
                log.debug("%s: DID 0x%04X unavailable: %s", self.address, did, exc)
        return found

    def identification(self) -> dict[str, Any]:
        """Read the ISO 14229 identification block.

        Standardised, so it works on any UDS module without a manufacturer definition:
        VIN, serial number, part and software numbers, and the programming and
        calibration dates. Those last two matter for a used car -- a module reprogrammed
        recently on a car with high mileage is worth asking about.
        """
        result: dict[str, Any] = {}
        for did, name in STANDARD_DIDS.items():
            try:
                raw = self.read_did(did)
            except (UdsNegativeResponse, NoResponse, UdsError):
                continue
            result[name] = {"did": f"0x{did:04X}", "raw": raw.hex(), "text": _as_text(raw)}
        return result

    # --- 0x19 ReadDTCInformation ---------------------------------------------

    def read_dtcs(self, status_mask: int = 0xFF) -> list[UdsDtc]:
        """Report DTCs matching *status_mask* (sub-function 0x02).

        The default mask asks for everything the module has. Manufacturer codes from a
        cluster or ABS module are invisible to generic OBD-II, which only ever sees the
        emissions-related subset.
        """
        request = bytes([SERVICE_READ_DTC, 0x02, status_mask & 0xFF])
        try:
            reply = self._exchange(request)
        except UdsNegativeResponse as exc:
            if exc.is_unsupported:
                return []
            raise

        # 19 02 <availability mask> then 4 bytes per DTC.
        body = reply[2:] if len(reply) >= 2 else b""
        if len(body) < 1:
            return []
        records = body[1:]
        dtcs: list[UdsDtc] = []
        for offset in range(0, len(records) - 3, 4):
            group = records[offset : offset + 4]
            if group[:3] == b"\x00\x00\x00":
                continue  # padding, not a fault
            dtcs.append(UdsDtc(high=group[0], middle=group[1], low=group[2], status=group[3]))
        return dtcs

    def count_dtcs(self, status_mask: int = 0xFF) -> int | None:
        """Sub-function 0x01: how many DTCs match, without listing them.

        Cheap, and a useful cross-check: a count that disagrees with the list is the
        same signal as in generic OBD-II.
        """
        request = bytes([SERVICE_READ_DTC, 0x01, status_mask & 0xFF])
        try:
            reply = self._exchange(request)
        except (UdsNegativeResponse, NoResponse, UdsError):
            return None
        # 19 01 <availability> <format> <count high> <count low>
        if len(reply) < 6:
            return None
        return (reply[4] << 8) | reply[5]


def _as_text(raw: bytes) -> str | None:
    """Render a payload as text if it plausibly is text.

    Identification DIDs hold part numbers and dates, mostly ASCII but not always, and
    a module is free to return binary. Returning ``None`` rather than mojibake keeps the
    hex in ``raw`` the authoritative value.
    """
    if not raw:
        return None
    if all(0x20 <= byte <= 0x7E for byte in raw):
        return raw.decode("ascii").strip()
    return None
