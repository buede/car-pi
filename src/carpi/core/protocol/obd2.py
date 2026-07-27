"""Generic OBD-II, per SAE J1979 / ISO 15031-5.

Covers the read-only modes that work on any compliant vehicle without
authentication:

===== ==================================================
01    live data
02    freeze frame -- conditions captured when a fault stored
03    stored fault codes
06    on-board monitor test results, with limits
07    pending fault codes
09    vehicle information: VIN, calibration IDs, CVNs
0A    permanent fault codes
===== ==================================================

**Mode 04 (clear fault codes) is deliberately not implemented here.** Clearing codes
destroys the permanent-DTC and monitor-readiness evidence that this tool exists to
collect, and a scanner that offers it one keystroke from a report is a tool for
sellers rather than buyers. If it is ever added it belongs in a separate module that
the inspection path does not import, guarded by explicit operator intent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from carpi.core.database import Database, PidDef
from carpi.core.protocol import dtc as dtc_module
from carpi.core.protocol.decoders import decode as run_decoder
from carpi.core.transport.base import Channel, NoResponse

__all__ = [
    "MonitorTestResult",
    "NegativeResponse",
    "Obd2Client",
    "PidReading",
    "ProtocolError",
    "decode_pid",
]

log = logging.getLogger(__name__)

MODE_LIVE_DATA = 0x01
MODE_FREEZE_FRAME = 0x02
MODE_STORED_DTCS = 0x03
MODE_MONITOR_RESULTS = 0x06
MODE_PENDING_DTCS = 0x07
MODE_VEHICLE_INFO = 0x09
MODE_PERMANENT_DTCS = 0x0A

_POSITIVE_OFFSET = 0x40
_NEGATIVE_RESPONSE = 0x7F
_RESPONSE_PENDING = 0x78

_NRC_MEANINGS = {
    0x10: "general reject",
    0x11: "service not supported",
    0x12: "sub-function not supported",
    0x13: "incorrect message length",
    0x21: "busy, repeat request",
    0x22: "conditions not correct",
    0x31: "request out of range",
    0x33: "security access denied",
    0x78: "response pending",
}

# Mode 09 items, and how many bytes each data item occupies.
INFO_VIN = 0x02
INFO_CALIBRATION_ID = 0x04
INFO_CVN = 0x06
INFO_ECU_NAME = 0x0A
_INFO_ITEM_WIDTH = {INFO_VIN: 17, INFO_CALIBRATION_ID: 16, INFO_CVN: 4, INFO_ECU_NAME: 20}

_MODE06_GROUP = 9


class ProtocolError(Exception):
    """The ECU answered, but not with something this mode permits."""


class NegativeResponse(ProtocolError):
    """The ECU explicitly refused the request."""

    def __init__(self, service: int, nrc: int) -> None:
        self.service = service
        self.nrc = nrc
        meaning = _NRC_MEANINGS.get(nrc, "unknown reason")
        super().__init__(f"service 0x{service:02X} refused: 0x{nrc:02X} ({meaning})")

    @property
    def is_unsupported(self) -> bool:
        """True when the refusal means "I don't implement this", not "not now"."""
        return self.nrc in (0x11, 0x12, 0x31)


@dataclass(frozen=True)
class PidReading:
    """One decoded parameter."""

    definition: PidDef
    raw: bytes
    value: Any
    plausible: bool = True

    @property
    def name(self) -> str:
        return self.definition.name

    @property
    def unit(self) -> str | None:
        return self.definition.unit

    def __str__(self) -> str:
        if isinstance(self.value, float):
            shown = f"{self.value:.2f}".rstrip("0").rstrip(".")
        else:
            shown = str(self.value)
        suffix = f" {self.definition.unit}" if self.definition.unit else ""
        flag = "" if self.plausible else "  [outside plausible range]"
        return f"{self.definition.label}: {shown}{suffix}{flag}"


@dataclass(frozen=True)
class MonitorTestResult:
    """One Mode 06 on-board monitor test result.

    The engineering scaling of these values depends on a unit-and-scaling table that
    car-pi does not yet ship, so *value*, *minimum* and *maximum* are raw counts.
    That costs less than it sounds: all three share one scaling factor, so
    :attr:`passed` and :attr:`margin` are exact regardless of what it is. A monitor
    sitting at 4% of its allowed range is in trouble whatever the unit turns out to
    be, and that margin is the number worth reading.
    """

    monitor_id: int
    test_id: int
    unit_and_scaling_id: int
    value: int
    minimum: int
    maximum: int

    @property
    def passed(self) -> bool:
        return self.minimum <= self.value <= self.maximum

    @property
    def margin(self) -> float | None:
        """Where the value sits in its allowed band, 0.0 at the worst permitted edge.

        Returns ``None`` when the limits are degenerate and no margin is meaningful.
        A value close to 0.0 or 1.0 is near its limit; comfortably inside is healthy.
        """
        span = self.maximum - self.minimum
        if span <= 0:
            return None
        return (self.value - self.minimum) / span

    def __str__(self) -> str:
        verdict = "pass" if self.passed else "FAIL"
        margin = "" if self.margin is None else f", {self.margin * 100:.0f}% into range"
        return (
            f"monitor 0x{self.monitor_id:02X} test 0x{self.test_id:02X}: "
            f"{self.value} in [{self.minimum}, {self.maximum}] -- {verdict}{margin}"
        )


class Obd2Client:
    """Generic OBD-II conversation with one ECU."""

    def __init__(self, channel: Channel, database: Database, *, timeout: float = 1.0) -> None:
        self._channel = channel
        self._db = database
        self._timeout = timeout
        self._info_support: set[int] | None = None

    @property
    def address(self) -> Any:
        return self._channel.address

    @property
    def database(self) -> Database:
        """The definitions this client decodes with."""
        return self._db

    # --- request plumbing ------------------------------------------------------

    def _exchange(self, request: bytes, *, retries: int = 1) -> bytes:
        """Send *request*, return a validated positive response payload."""
        service = request[0]
        attempt = 0
        while True:
            reply = self._channel.request(request, timeout=self._timeout)
            if not reply:
                raise ProtocolError(f"empty reply to {request.hex(' ')}")

            if reply[0] == _NEGATIVE_RESPONSE:
                if len(reply) < 3:
                    raise ProtocolError(f"truncated negative response: {reply.hex(' ')}")
                nrc = reply[2]
                # "Response pending" means the ECU needs longer, not that it refused.
                # Some modules use it for Mode 06 and for long VIN reads.
                if nrc == _RESPONSE_PENDING and attempt < retries:
                    attempt += 1
                    log.debug("%s: response pending, waiting again", self._channel.address)
                    continue
                raise NegativeResponse(service=reply[1], nrc=nrc)

            expected = service + _POSITIVE_OFFSET
            if reply[0] != expected:
                raise ProtocolError(
                    f"expected service 0x{expected:02X} in reply to 0x{service:02X}, "
                    f"got 0x{reply[0]:02X}"
                )
            return reply

    # --- Mode 01 / 02: live data and freeze frames -----------------------------

    def read_pid(
        self,
        pid: int | str,
        *,
        mode: int = MODE_LIVE_DATA,
        frame: int = 0,
    ) -> PidReading:
        """Read and decode one parameter.

        For *mode* ``0x02`` the *frame* number selects which freeze frame to read.
        """
        definition = self._db.pid(pid)
        if mode == MODE_FREEZE_FRAME:
            request = bytes([mode, definition.pid, frame])
        else:
            request = bytes([mode, definition.pid])

        reply = self._exchange(request)

        # Verify the ECU echoed the PID we asked about. Skipping this check is how a
        # single timeout turns into a whole scan of values attributed to the wrong
        # parameters -- every subsequent reply is one exchange out of step.
        if len(reply) < 2 or reply[1] != definition.pid:
            got = f"0x{reply[1]:02X}" if len(reply) > 1 else "nothing"
            raise ProtocolError(
                f"asked for PID 0x{definition.pid:02X} ({definition.name}) "
                f"but the reply echoed {got}"
            )

        header = 3 if mode == MODE_FREEZE_FRAME else 2
        data = reply[header:]
        if len(data) < definition.length:
            raise ProtocolError(
                f"PID 0x{definition.pid:02X} ({definition.name}) needs "
                f"{definition.length} data bytes, got {len(data)}: {reply.hex(' ')}"
            )
        data = data[: definition.length]
        return decode_pid(definition, data)

    def supported_pids(self, *, mode: int = MODE_LIVE_DATA) -> set[int]:
        """Which PIDs this ECU implements, by walking the support bitmaps.

        Each bitmap PID reports the next 32 PIDs, and its lowest bit doubles as
        "the following bitmap exists". Following that chain costs a handful of
        requests, where blind probing would cost 256 and still risk provoking
        modules with unsolicited requests.
        """
        supported: set[int] = set()
        base = 0x00
        while base <= 0xC0:
            try:
                reading = self.read_pid(base, mode=mode)
            except (NoResponse, ProtocolError):
                break
            offsets = reading.value.get("offsets", []) if isinstance(reading.value, dict) else []
            supported.update(base + offset for offset in offsets)
            next_bitmap = base + 0x20
            if next_bitmap not in supported:
                break
            base = next_bitmap
        return supported

    def read_all_supported(
        self, *, mode: int = MODE_LIVE_DATA, frame: int = 0
    ) -> dict[str, PidReading]:
        """Read every PID this ECU supports that the database can decode.

        Failures are logged and skipped rather than aborting: one PID an ECU
        mishandles must not cost the whole scan.
        """
        readings: dict[str, PidReading] = {}
        for pid in sorted(self.supported_pids(mode=mode)):
            definition = self._db.pids_by_number.get(pid)
            if definition is None:
                log.debug("PID 0x%02X supported but not in the database", pid)
                continue
            if definition.decoder == "pid_support_bitmap":
                continue  # already consumed by discovery
            try:
                readings[definition.name] = self.read_pid(pid, mode=mode, frame=frame)
            except (NoResponse, ProtocolError) as exc:
                log.debug("PID 0x%02X (%s) failed: %s", pid, definition.name, exc)
        return readings

    # --- Modes 03 / 07 / 0A: fault codes ---------------------------------------

    def _read_dtcs(self, mode: int) -> list[str]:
        try:
            reply = self._exchange(bytes([mode]))
        except NegativeResponse as exc:
            if exc.is_unsupported:
                return []
            raise
        try:
            return dtc_module.parse_dtc_response(reply)
        except dtc_module.DtcCountMismatch as exc:
            # Worth a warning, but the codes themselves are still usable and a
            # mismatch is itself reported as a finding.
            log.warning("%s: %s", self._channel.address, exc)
            return exc.codes

    def stored_dtcs(self) -> list[str]:
        """Mode 03 -- confirmed faults, the ones that light the warning lamp."""
        return self._read_dtcs(MODE_STORED_DTCS)

    def pending_dtcs(self) -> list[str]:
        """Mode 07 -- faults seen once, not yet confirmed across enough cycles."""
        return self._read_dtcs(MODE_PENDING_DTCS)

    def permanent_dtcs(self) -> list[str]:
        """Mode 0A -- faults no scan tool can erase.

        The single most valuable read for a used-car inspection: only the ECU clears
        these, and only after the relevant self-test passes repeatedly. They survive
        a seller wiping the codes before a viewing.
        """
        return self._read_dtcs(MODE_PERMANENT_DTCS)

    # --- Mode 06: monitor test results -----------------------------------------

    def monitor_test_results(self) -> list[MonitorTestResult]:
        """Mode 06 -- numeric results and limits for the on-board self-tests.

        This is where a catalytic converter that still passes but is nearly spent
        becomes visible, long before it stores a fault code.
        """
        try:
            available = self._supported_monitor_ids()
        except (NoResponse, ProtocolError) as exc:
            log.debug("Mode 06 support bitmap unavailable: %s", exc)
            return []

        results: list[MonitorTestResult] = []
        for monitor_id in sorted(available):
            try:
                reply = self._exchange(bytes([MODE_MONITOR_RESULTS, monitor_id]))
            except (NoResponse, ProtocolError) as exc:
                log.debug("Mode 06 monitor 0x%02X failed: %s", monitor_id, exc)
                continue
            results.extend(_parse_mode06(reply))
        return results

    def _supported_monitor_ids(self) -> set[int]:
        """Walk the Mode 06 support bitmaps, same chained scheme as Mode 01."""
        supported: set[int] = set()
        base = 0x00
        while base <= 0xE0:
            reply = self._exchange(bytes([MODE_MONITOR_RESULTS, base]))
            if len(reply) < 6 or reply[1] != base:
                break
            raw = int.from_bytes(reply[2:6], "big")
            supported.update(base + (32 - bit) for bit in range(32) if raw & (1 << bit))
            next_bitmap = base + 0x20
            if next_bitmap not in supported:
                break
            base = next_bitmap
        # The bitmap IDs themselves are not monitors.
        return {mid for mid in supported if mid % 0x20 != 0}

    # --- Mode 09: vehicle information ------------------------------------------

    def supported_info_items(self) -> set[int]:
        """Which Mode 09 items this ECU implements, from its support bitmap.

        Consulting the bitmap first matters more than it looks. Plenty of modules --
        a transmission or ABS controller, say -- implement no vehicle information at
        all, and probing four items blind costs four full timeouts *per module*. On a
        five-module car that is most of the scan's wall-clock time spent learning
        nothing.

        If the bitmap itself is unavailable, every known item is returned so the
        blind probe still happens. A sloppy ECU that answers the VIN but not the
        bitmap is worth the wait; silently skipping the VIN would not be.
        """
        if self._info_support is not None:
            return self._info_support

        try:
            reply = self._exchange(bytes([MODE_VEHICLE_INFO, 0x00]))
        except (NoResponse, ProtocolError):
            self._info_support = set(_INFO_ITEM_WIDTH)
            return self._info_support

        if len(reply) >= 6 and reply[1] == 0x00:
            raw = int.from_bytes(reply[2:6], "big")
            self._info_support = {32 - bit for bit in range(32) if raw & (1 << bit)}
        else:
            self._info_support = set(_INFO_ITEM_WIDTH)
        return self._info_support

    def _read_info(self, item: int) -> list[bytes]:
        """Read one Mode 09 item, returning its data items.

        The standard puts a count byte after the item number, but not every ECU
        sends one. The width of each item is fixed and known, so the count byte is
        detected by whether the payload length is an exact multiple of that width.
        """
        if item not in self.supported_info_items():
            return []
        try:
            reply = self._exchange(bytes([MODE_VEHICLE_INFO, item]))
        except NegativeResponse as exc:
            if exc.is_unsupported:
                return []
            raise

        if len(reply) < 2 or reply[1] != item:
            raise ProtocolError(
                f"asked for vehicle info 0x{item:02X}, reply echoed "
                f"{f'0x{reply[1]:02X}' if len(reply) > 1 else 'nothing'}"
            )

        body = reply[2:]
        width = _INFO_ITEM_WIDTH.get(item)
        if width is None:
            return [body]
        if len(body) % width != 0 and len(body) >= 1:
            body = body[1:]  # leading count byte
        if width == 0 or len(body) < width:
            raise ProtocolError(
                f"vehicle info 0x{item:02X}: expected a multiple of {width} bytes, got {len(body)}"
            )
        return [body[offset : offset + width] for offset in range(0, len(body) - width + 1, width)]

    def vin(self) -> str | None:
        """Mode 09 item 02 -- the vehicle identification number.

        Worth cross-checking against the VIN on the windscreen, the V5C/registration
        document and the door sticker: a mismatch means the car is not what the
        paperwork says it is.
        """
        items = self._read_info(INFO_VIN)
        if not items:
            return None
        text = items[0].decode("ascii", errors="replace").strip("\x00 ")
        return text or None

    def calibration_ids(self) -> list[str]:
        """Mode 09 item 04 -- software calibration identifiers."""
        return [
            item.decode("ascii", errors="replace").strip("\x00 ")
            for item in self._read_info(INFO_CALIBRATION_ID)
        ]

    def calibration_verification_numbers(self) -> list[str]:
        """Mode 09 item 06 -- CVNs, a checksum over the calibration.

        A CVN that does not match the stock value for a given calibration ID is
        evidence the ECU has been reflashed -- a remap, or a repair shop's software
        update. Interpreting them needs a reference table car-pi does not yet ship,
        so for now they are reported for the record.
        """
        return [item.hex().upper() for item in self._read_info(INFO_CVN)]

    def ecu_name(self) -> str | None:
        """Mode 09 item 0A -- the module's self-reported name."""
        items = self._read_info(INFO_ECU_NAME)
        if not items:
            return None
        text = items[0].decode("ascii", errors="replace").strip("\x00 ")
        return text or None


def decode_pid(definition: PidDef, data: bytes) -> PidReading:
    """Decode raw payload bytes for one PID.

    Pure and transport-free, so it can be tested against recorded byte vectors
    without a bus, a car, or a simulator anywhere in the picture.

    Formulas see the individual bytes as ``A``, ``B``, ``C``..., plus the whole window
    as ``U`` (unsigned) and ``S`` (signed two's-complement).
    """
    if definition.decoder is not None:
        return PidReading(
            definition=definition,
            raw=data,
            value=run_decoder(definition.decoder, data),
        )

    assert definition.formula is not None  # the schema guarantees one or the other
    variables: dict[str, Any] = {chr(ord("A") + index): byte for index, byte in enumerate(data)}
    variables["U"] = int.from_bytes(data, "big", signed=False)
    variables["S"] = int.from_bytes(data, "big", signed=True)
    value = float(definition.formula.evaluate(variables))
    return PidReading(
        definition=definition,
        raw=data,
        value=value,
        plausible=definition.in_range(value),
    )


def _parse_mode06(reply: bytes) -> list[MonitorTestResult]:
    """Split a Mode 06 response into its fixed-width test-result records."""
    body = reply[1:]
    results: list[MonitorTestResult] = []
    for offset in range(0, len(body) - _MODE06_GROUP + 1, _MODE06_GROUP):
        group = body[offset : offset + _MODE06_GROUP]
        results.append(
            MonitorTestResult(
                monitor_id=group[0],
                test_id=group[1],
                unit_and_scaling_id=group[2],
                value=int.from_bytes(group[3:5], "big"),
                minimum=int.from_bytes(group[5:7], "big"),
                maximum=int.from_bytes(group[7:9], "big"),
            )
        )
    return results
