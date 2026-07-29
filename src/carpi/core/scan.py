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
from collections.abc import Callable, Iterable
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
from carpi.core.vehicles import EcuProfile, ModuleReading, VehicleProfile, read_module

__all__ = [
    "EcuScan",
    "ProgressCallback",
    "ScanResult",
    "build_facts",
    "discover_modules",
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
    discover: bool = False,
    discovery_window: float = 0.08,
) -> ScanResult:
    """Discover every responding ECU and scan each in turn.

    *profile* adds manufacturer-specific reads, including modules outside the OBD-II
    address range. Without one, only the standardised layer is available -- which is
    universal but shallow, and cannot reach the odometer.

    *read_module_vins* asks each module for the standardised VIN identifier. One request
    per module, and it is what makes the cross-module VIN comparison possible.

    *discover* additionally sweeps the diagnostic address range and reads the standardised
    identification block from anything it finds. Off by default, because it transmits a
    few hundred more frames and adds most of a minute. With it, a scan reports which
    modules a car has and compares their VINs even when no definition file for that
    vehicle exists -- which is every real car today.

    *discovery_window* is how long each probed address gets to answer. Most of a sweep's
    duration is that multiplied by 256, so it is the knob trading sweep time against the
    risk of missing a slow module.
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

    # Selecting a profile by VIN needs the VIN, which is only known now. `--profile` has
    # promised this in its help text since it was written, and until now it did not happen.
    partial = ScanResult(started_at=started, finished_at=started, transport="", ecus=tuple(scans))
    if profile is None and partial.vin:
        profile = database.profile_for_vin(partial.vin)
        if profile is not None:
            report(f"VIN matches the {profile.label} profile")

    readings: list[ModuleReading] = []
    if profile is not None:
        report(f"reading manufacturer data ({profile.label})")
        for ecu_profile in profile.ecus:
            uds = UdsClient(link.channel(ecu_profile.address), timeout=timeout)
            readings.append(read_module(uds, ecu_profile, on_progress=on_progress))

    if discover:
        # After the profile, so a module a profile already describes properly is not also
        # listed with only its standardised identification.
        described = {reading.ecu.response_id for reading in readings}
        described.update(address.rx_id for address in addresses)
        readings.extend(
            discover_modules(
                link,
                known=described,
                timeout=timeout,
                response_window=discovery_window,
                on_progress=on_progress,
            )
        )

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


def _standard_reads(identification: dict[str, Any]) -> tuple[dict[str, Any], dict[str, str]]:
    """Split an identification block into decoded values and raw bytes."""
    values: dict[str, Any] = {}
    raw: dict[str, str] = {}
    for name, entry in identification.items():
        # Text when the bytes are printable, raw hex otherwise. Never a guess: an
        # identification field whose encoding is manufacturer-specific stays as bytes
        # rather than being rendered as mojibake that looks like a part number.
        values[name] = entry.get("text") or entry.get("raw")
        raw[name] = entry.get("raw", "")
    return values, raw


def discover_modules(
    link: CanLink,
    *,
    known: Iterable[int] = (),
    timeout: float = 1.0,
    request_delay: float = 0.02,
    response_window: float = 0.08,
    on_progress: ProgressCallback | None = None,
) -> list[ModuleReading]:
    """Sweep for modules outside the OBD-II range and read what each one says it is.

    This is what makes a scan capable on a car nobody has written a definition for. The
    sweep finds the addresses, and every identifier read afterwards is from ISO 14229
    Annex C -- standardised, so it needs no per-vehicle data at all. What comes back is
    each module's part number, serial number, software versions, programming date, and
    its VIN.

    That last one is the point. A module holding a different VIN from the rest of the car
    came out of a different car, and the comparison needs no definition file to work. The
    odometer still does, because no standard identifier holds it.

    Read-only throughout: the sweep probes with TesterPresent and the reads are all
    ReadDataByIdentifier.
    """
    from carpi.core.discovery import sweep_addresses

    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    report("sweeping for modules outside the OBD-II range")
    stats = sweep_addresses(
        link,
        request_delay=request_delay,
        response_window=response_window,
        on_progress=on_progress,
    )

    already = set(known)
    # OBD-II modules are excluded because `scan_ecu` has already interrogated them far more
    # thoroughly than an identification block would. Re-reading them here would cost a
    # request each to learn less.
    candidates = [
        module
        for module in stats.modules
        if not module.is_obd_address and module.response_id not in already
    ]
    report(f"{len(stats.modules)} address(es) answered, {len(candidates)} outside OBD-II")

    readings: list[ModuleReading] = []
    for index, module in enumerate(candidates, start=1):
        report(f"identifying module {index} of {len(candidates)} ({module.label})")
        try:
            client = UdsClient(link.channel(module.address), timeout=timeout)
            # Many modules answer the identification block only inside an extended
            # session. A refusal is not fatal -- the reads are attempted either way, and
            # whatever answers is kept.
            client.start_session()
            identification = client.identification()
        except (NoResponse, TransportError, UdsError) as exc:
            log.debug("module %s could not be identified: %s", module.label, exc)
            continue

        if not identification:
            continue

        values, raw = _standard_reads(identification)
        # The address stays in the name. `EcuAddress.label` prefers a name when it has one,
        # so a module that names itself would otherwise push its own address out of the
        # report -- and the address is what somebody needs to go back and read more.
        reported = values.get("system_name_or_engine_type")
        name = f"{reported} ({module.label})" if reported else f"Module {module.label}"
        profile = EcuProfile(
            name=str(name),
            request_id=module.request_id,
            response_id=module.response_id,
            extended=module.extended,
            reads=(),
        )
        readings.append(ModuleReading(ecu=profile, values=values, raw=raw, reached=True))

    report(f"identified {len(readings)} module(s) beyond generic OBD-II")
    return readings


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

    # Modules found by an address sweep hold their VIN at the same standardised identifier,
    # and they are the ones worth comparing: an instrument cluster fitted from another car
    # is the usual way a mileage discrepancy arises, and no OBD-II address reaches it.
    for reading in result.module_readings:
        vin = reading.values.get("vin")
        if isinstance(vin, str) and vin.strip():
            module_vins[reading.ecu.name] = vin.strip()

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
