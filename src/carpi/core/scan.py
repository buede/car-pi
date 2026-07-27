"""Running a full inspection scan and turning it into facts.

A scan talks to every ECU that answers, not just the engine controller, because
permanent fault codes and stored faults live wherever the fault occurred.

Aggregation across modules needs care to stay comparable. ``status.dtc_count`` is the
**sum** of each ECU's own count from PID 01, and ``dtc.stored_count`` is the **sum** of
the codes those same ECUs then listed. Summing both keeps the disagreement rule
honest; comparing one ECU's count against every ECU's codes would flag every
multi-module car as tampered with.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from carpi.core.database import Database
from carpi.core.protocol.obd2 import (
    MODE_FREEZE_FRAME,
    MonitorTestResult,
    Obd2Client,
    PidReading,
    ProtocolError,
)
from carpi.core.protocol.uds import UdsClient, UdsError
from carpi.core.rules import Evaluation, evaluate, flatten_facts
from carpi.core.transport.base import EcuAddress, NoResponse, TransportError
from carpi.core.transport.canbus import CanLink
from carpi.core.vehicles import ModuleReading, VehicleProfile, read_module

__all__ = [
    "EcuScan",
    "ProgressCallback",
    "ScanResult",
    "build_facts",
    "scan_ecu",
    "scan_vehicle",
]

log = logging.getLogger(__name__)

# Called with a short human-readable description of what the scan is doing now.
ProgressCallback = Callable[[str], None]

# Conventional reply ID of the engine controller on an 11-bit bus. Preferred as the
# primary module because it carries the readiness monitors and live data.
_ENGINE_RESPONSE_ID = 0x7E8


@dataclass
class EcuScan:
    """Everything one ECU told us."""

    address: EcuAddress
    ecu_name: str | None = None
    supported_pids: tuple[int, ...] = ()
    readings: dict[str, PidReading] = field(default_factory=dict)
    stored_dtcs: tuple[str, ...] = ()
    pending_dtcs: tuple[str, ...] = ()
    permanent_dtcs: tuple[str, ...] = ()
    freeze_frame: dict[str, PidReading] = field(default_factory=dict)
    monitor_results: tuple[MonitorTestResult, ...] = ()
    vin: str | None = None
    calibration_ids: tuple[str, ...] = ()
    calibration_verification_numbers: tuple[str, ...] = ()
    # Reads the module simply does not implement. In OBD-II an ECU declines by
    # staying silent, so this is ordinary and expected -- a transmission controller
    # has no VIN to report. Kept apart from `errors` so a report does not cry wolf.
    unsupported: tuple[str, ...] = ()
    # Reads that went genuinely wrong: a malformed reply, or a transport fault.
    errors: tuple[str, ...] = ()
    # VIN as this module itself reports it, via the standardised UDS identifier 0xF190.
    # A module holding a different VIN from the rest of the car was fitted from another
    # vehicle -- which for an instrument cluster is the classic mileage-tampering method.
    uds_vin: str | None = None

    @property
    def monitors(self) -> dict[str, dict[str, bool]]:
        """Readiness monitors this ECU reports, or empty if it reports none."""
        reading = self.readings.get("monitor_status")
        if reading is None or not isinstance(reading.value, dict):
            return {}
        monitors = reading.value.get("monitors", {})
        return monitors if isinstance(monitors, dict) else {}

    @property
    def mil_on(self) -> bool | None:
        reading = self.readings.get("monitor_status")
        if reading is None or not isinstance(reading.value, dict):
            return None
        value = reading.value.get("mil_on")
        return bool(value) if value is not None else None

    @property
    def reported_dtc_count(self) -> int | None:
        """The ECU's own fault counter from PID 01."""
        reading = self.readings.get("monitor_status")
        if reading is None or not isinstance(reading.value, dict):
            return None
        value = reading.value.get("dtc_count")
        return int(value) if value is not None else None


@dataclass
class ScanResult:
    """A complete vehicle scan."""

    started_at: str
    finished_at: str
    transport: str
    ecus: tuple[EcuScan, ...] = ()
    claimed_odometer_km: float | None = None
    notes: tuple[str, ...] = ()
    # Manufacturer-specific results, present only when a vehicle profile was used.
    profile_id: str | None = None
    profile_label: str | None = None
    module_readings: tuple[ModuleReading, ...] = ()

    @property
    def odometer_by_module(self) -> dict[str, float]:
        """Every module that reported an odometer, by module name.

        The comparison this enables is the point of the whole manufacturer-specific
        path: tampering is nearly always done by rewriting the instrument cluster,
        leaving every other module holding the true figure.
        """
        found: dict[str, float] = {}
        for reading in self.module_readings:
            value = reading.values.get("odometer_km")
            if isinstance(value, int | float):
                found[reading.ecu.name] = float(value)
        return found

    @property
    def primary(self) -> EcuScan | None:
        """The module to read live data from -- the engine controller if present."""
        if not self.ecus:
            return None
        for ecu in self.ecus:
            if ecu.address.rx_id == _ENGINE_RESPONSE_ID:
                return ecu
        return max(self.ecus, key=lambda e: len(e.readings))

    @property
    def vin(self) -> str | None:
        for ecu in self.ecus:
            if ecu.vin:
                return ecu.vin
        return None

    @property
    def all_stored_dtcs(self) -> list[str]:
        return [code for ecu in self.ecus for code in ecu.stored_dtcs]

    @property
    def all_pending_dtcs(self) -> list[str]:
        return [code for ecu in self.ecus for code in ecu.pending_dtcs]

    @property
    def all_permanent_dtcs(self) -> list[str]:
        return [code for ecu in self.ecus for code in ecu.permanent_dtcs]

    def evaluate(self, database: Database) -> Evaluation:
        """Run the inspection rules over this scan."""
        return evaluate(database, build_facts(self))


def scan_ecu(
    client: Obd2Client,
    *,
    read_freeze_frame: bool = True,
    on_progress: ProgressCallback | None = None,
) -> EcuScan:
    """Interrogate one ECU as thoroughly as it permits.

    Each step is independently guarded. A module that mishandles one mode -- and
    plenty do -- must not cost us everything else it was willing to tell us.

    *on_progress* is called with a short description before each step. A full scan
    takes tens of seconds on a real car, most of it waiting out timeouts for modes a
    module does not implement, so a caller with a screen needs something to show.
    """
    scan = EcuScan(address=client.address)
    errors: list[str] = []
    unsupported: list[str] = []

    def attempt(label: str, action: Any) -> Any:
        if on_progress is not None:
            on_progress(f"{client.address}: reading {label}")
        try:
            return action()
        except NoResponse:
            # Silence is how an OBD-II ECU says "I don't implement that". Recording it
            # as a failure would bury the genuine faults in expected noise.
            log.debug("%s: %s not supported", client.address, label)
            unsupported.append(label)
            return None
        except (ProtocolError, TransportError) as exc:
            log.debug("%s: %s failed: %s", client.address, label, exc)
            errors.append(f"{label}: {exc}")
            return None

    supported = attempt("supported PIDs", client.supported_pids)
    scan.supported_pids = tuple(sorted(supported)) if supported else ()

    readings = attempt("live data", client.read_all_supported)
    scan.readings = readings or {}

    for label, reader, attribute in (
        ("stored DTCs", client.stored_dtcs, "stored_dtcs"),
        ("pending DTCs", client.pending_dtcs, "pending_dtcs"),
        ("permanent DTCs", client.permanent_dtcs, "permanent_dtcs"),
    ):
        codes = attempt(label, reader)
        setattr(scan, attribute, tuple(codes) if codes else ())

    results = attempt("monitor test results", client.monitor_test_results)
    scan.monitor_results = tuple(results) if results else ()

    scan.vin = attempt("VIN", client.vin)
    calibration = attempt("calibration IDs", client.calibration_ids)
    scan.calibration_ids = tuple(calibration) if calibration else ()
    cvns = attempt("CVNs", client.calibration_verification_numbers)
    scan.calibration_verification_numbers = tuple(cvns) if cvns else ()
    scan.ecu_name = attempt("ECU name", client.ecu_name)

    # Only worth reading a freeze frame if a fault actually stored one. Reading it
    # unconditionally provokes negative responses on healthy cars for no gain.
    if read_freeze_frame and (scan.stored_dtcs or scan.permanent_dtcs):
        frame = attempt(
            "freeze frame",
            lambda: client.read_all_supported(mode=MODE_FREEZE_FRAME),
        )
        scan.freeze_frame = frame or {}

    scan.unsupported = tuple(unsupported)
    scan.errors = tuple(errors)
    return scan


def scan_vehicle(
    link: CanLink,
    database: Database,
    *,
    claimed_odometer_km: float | None = None,
    timeout: float = 1.0,
    discovery_timeout: float = 1.0,
    on_progress: ProgressCallback | None = None,
    profile: VehicleProfile | None = None,
    read_module_vins: bool = True,
) -> ScanResult:
    """Discover every responding ECU and scan each in turn.

    *profile* adds manufacturer-specific reads, including modules outside the OBD-II
    address range. Without one, only the standardised layer is available -- which is
    universal but shallow, and cannot reach the odometer.

    *read_module_vins* asks each module for the standardised VIN identifier. One request
    per module, and it is what makes the cross-module VIN comparison possible.
    """
    started = datetime.now(UTC).isoformat(timespec="seconds")
    notes: list[str] = []

    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    report("looking for modules that answer")
    addresses = link.discover_ecus(timeout=discovery_timeout)
    if not addresses:
        notes.append(
            "No ECU answered the broadcast request. On a real vehicle, check that the "
            "ignition is ON rather than in accessory mode, that the interface bitrate "
            "matches the bus, and that CAN_H and CAN_L are not swapped."
        )

    report(f"{len(addresses)} module(s) answered")
    scans: list[EcuScan] = []
    for index, address in enumerate(addresses, start=1):
        report(f"module {index} of {len(addresses)} ({address})")
        channel = link.channel(address)
        client = Obd2Client(channel, database, timeout=timeout)
        scan = scan_ecu(client, on_progress=on_progress)
        if read_module_vins:
            scan.uds_vin = _read_module_vin(link, address, timeout=timeout)
        scans.append(scan)

    readings: list[ModuleReading] = []
    if profile is not None:
        report(f"reading manufacturer data ({profile.label})")
        for ecu_profile in profile.ecus:
            uds = UdsClient(link.channel(ecu_profile.address), timeout=timeout)
            readings.append(read_module(uds, ecu_profile, on_progress=on_progress))

    report("evaluating findings")
    return ScanResult(
        started_at=started,
        finished_at=datetime.now(UTC).isoformat(timespec="seconds"),
        transport=type(link.bus).__name__,
        ecus=tuple(scans),
        claimed_odometer_km=claimed_odometer_km,
        notes=tuple(notes),
        profile_id=profile.id if profile else None,
        profile_label=profile.label if profile else None,
        module_readings=tuple(readings),
    )


def _read_module_vin(link: CanLink, address: EcuAddress, *, timeout: float) -> str | None:
    """Ask one module for the standardised VIN identifier (0xF190).

    Standardised, so it needs no manufacturer definition. Plenty of modules do not
    implement UDS at all and simply do not answer, which is not an error.
    """
    try:
        client = UdsClient(link.channel(address), timeout=timeout)
        raw = client.read_did(0xF190)
    except (NoResponse, TransportError, UdsError):
        return None
    text = raw.decode("ascii", errors="replace").strip("\x00 ")
    return text or None


def _aggregate_monitors(ecus: tuple[EcuScan, ...]) -> dict[str, bool]:
    """Merge readiness monitors across modules, into ``name -> complete``.

    A monitor counts as supported if any module supports it, and as complete only if
    every module that supports it says so. Erring toward "not complete" is the safe
    direction: it prompts another drive cycle rather than certifying a car whose
    self-tests have not actually run.
    """
    merged: dict[str, bool] = {}
    for ecu in ecus:
        for name, state in ecu.monitors.items():
            if not state.get("supported"):
                continue
            complete = bool(state.get("complete"))
            merged[name] = complete if name not in merged else (merged[name] and complete)
    return merged


def build_facts(result: ScanResult) -> dict[str, Any]:
    """Flatten a scan into the fact mapping the rules evaluate against."""
    facts: dict[str, Any] = {}

    facts["vehicle.ecu_count"] = len(result.ecus)
    if result.vin:
        facts["vehicle.vin"] = result.vin
    if result.claimed_odometer_km is not None:
        facts["vehicle.claimed_odometer_km"] = float(result.claimed_odometer_km)

    # Fault counts, summed per module so they stay comparable. See module docstring.
    facts["dtc.stored_count"] = len(result.all_stored_dtcs)
    facts["dtc.pending_count"] = len(result.all_pending_dtcs)
    facts["dtc.permanent_count"] = len(result.all_permanent_dtcs)
    facts["dtc.stored_unique_count"] = len(set(result.all_stored_dtcs))

    reported = [ecu.reported_dtc_count for ecu in result.ecus]
    if any(count is not None for count in reported):
        facts["status.dtc_count"] = sum(count for count in reported if count is not None)

    mil_flags = [ecu.mil_on for ecu in result.ecus if ecu.mil_on is not None]
    if mil_flags:
        facts["status.mil_on"] = any(mil_flags)

    monitors = _aggregate_monitors(result.ecus)
    if monitors:
        facts["readiness.supported_count"] = len(monitors)
        facts["readiness.complete_count"] = sum(1 for done in monitors.values() if done)
        facts["readiness.incomplete_count"] = sum(1 for done in monitors.values() if not done)
        for name, done in monitors.items():
            facts[f"readiness.{name}.complete"] = done

    primary = result.primary
    if primary is not None:
        ignition = primary.readings.get("monitor_status")
        if ignition is not None and isinstance(ignition.value, dict):
            detected = ignition.value.get("ignition")
            if detected:
                facts["status.ignition"] = detected

        for name, reading in primary.readings.items():
            if reading.definition.decoder == "pid_support_bitmap":
                continue
            # Values the ECU reported as physically impossible are omitted rather
            # than fed to rules, so a broken sensor cannot manufacture a finding.
            if isinstance(reading.value, float) and not reading.plausible:
                log.debug(
                    "%s reported %s outside its plausible range; excluded from facts",
                    primary.address,
                    name,
                )
                continue
            flatten_facts(f"pid.{name}", reading.value, facts)

    monitor_failures = [
        test for ecu in result.ecus for test in ecu.monitor_results if not test.passed
    ]
    facts["mode06.failing_count"] = len(monitor_failures)
    facts["mode06.result_count"] = sum(len(ecu.monitor_results) for ecu in result.ecus)

    facts.update(_uds_facts(result))
    return facts


def _uds_facts(result: ScanResult) -> dict[str, Any]:
    """Facts from the manufacturer-specific and standardised-UDS layers."""
    facts: dict[str, Any] = {}

    # Cross-module VIN comparison. Standardised (identifier 0xF190), so this works
    # without any manufacturer definition at all.
    module_vins = {ecu.address.label: ecu.uds_vin for ecu in result.ecus if ecu.uds_vin}
    if module_vins:
        facts["uds.module_vin_count"] = len(module_vins)
        reference = result.vin or next(iter(module_vins.values()))
        facts["uds.vin_mismatch_count"] = sum(1 for vin in module_vins.values() if vin != reference)

    if result.module_readings:
        facts["profile.module_count"] = len(result.module_readings)
        facts["profile.modules_reached"] = sum(
            1 for reading in result.module_readings if reading.reached
        )

    odometers = result.odometer_by_module
    if len(odometers) >= 2:
        # Only meaningful with two or more sources. With one there is nothing to compare
        # it against, and reporting a spread of zero would imply agreement that was
        # never actually established.
        facts["vehicle.odometer_module_count"] = len(odometers)
        facts["vehicle.odometer_spread_km"] = max(odometers.values()) - min(odometers.values())
        facts["vehicle.odometer_highest_km"] = max(odometers.values())

    return facts
