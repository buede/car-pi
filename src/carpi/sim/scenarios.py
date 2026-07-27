"""Simulated vehicles, each built to exercise a specific inspection outcome.

Every scenario declares the rule ids it is expected to trigger in ``expect_findings``,
and the test suite asserts exactly that. The fixtures therefore document what the tool
is supposed to conclude, and a change that quietly stops detecting a cleared ECU fails
the build rather than shipping.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from carpi.sim import encode as enc

__all__ = ["SCENARIOS", "EcuSpec", "Scenario", "get_scenario"]

# Deliberately not a real VIN: it contains the letter I, which real VINs never use.
# Nothing here should ever be mistakable for a specific physical car.
SIM_VIN = "CARPI0SIMULATED01"


@dataclass(frozen=True)
class EcuSpec:
    """One simulated module."""

    response_id: int
    label: str
    pids: dict[int, bytes] = field(default_factory=dict)
    freeze_frame: dict[int, bytes] = field(default_factory=dict)
    stored_dtcs: tuple[str, ...] = ()
    pending_dtcs: tuple[str, ...] = ()
    permanent_dtcs: tuple[str, ...] = ()
    # (monitor id, test id, unit-and-scaling id, value, minimum, maximum)
    monitor_tests: tuple[tuple[int, int, int, int, int, int], ...] = ()
    vin: str | None = None
    calibration_ids: tuple[str, ...] = ()
    cvns: tuple[bytes, ...] = ()
    ecu_name: str | None = None


@dataclass(frozen=True)
class Scenario:
    """A simulated vehicle plus the findings it should produce."""

    name: str
    summary: str
    ecus: tuple[EcuSpec, ...]
    claimed_odometer_km: float | None = None
    expect_findings: tuple[str, ...] = ()


def _engine_pids(
    *,
    mil_on: bool = False,
    dtc_count: int = 0,
    incomplete: set[str] | None = None,
    coolant_c: float = 89,
    rpm: float = 760,
    run_time_s: float = 900,
    ltft_bank1: float = 1.6,
    ltft_bank2: float | None = 2.3,
    stft_bank1: float = -0.8,
    distance_since_clear_km: float = 8200,
    warmups_since_clear: int = 210,
    minutes_since_clear: float = 9400,
    distance_with_mil_km: float = 0,
    minutes_with_mil: float = 0,
    voltage: float = 14.1,
    odometer_km: float | None = None,
) -> dict[int, bytes]:
    """A plausible warm-idle petrol engine controller, with the interesting bits open."""
    pids: dict[int, bytes] = {
        0x01: enc.monitor_status(mil_on=mil_on, dtc_count=dtc_count, incomplete=incomplete),
        0x03: bytes([0x02, 0x00]),  # closed loop on oxygen sensor feedback
        0x04: enc.percent(24),
        0x05: enc.temperature(coolant_c),
        0x06: enc.trim_percent(stft_bank1),
        0x07: enc.trim_percent(ltft_bank1),
        0x0B: enc.pressure_kpa(38),
        0x0C: enc.engine_rpm(rpm),
        0x0D: enc.u8(0),
        0x0E: enc.timing_advance(11.5),
        0x0F: enc.temperature(31),
        0x10: enc.maf_rate(3.4),
        0x11: enc.percent(14),
        0x13: bytes([0x03]),  # bank 1 sensors 1 and 2 present
        0x1C: bytes([0x06]),  # EOBD
        0x1F: enc.seconds(run_time_s),
        0x21: enc.distance_km(distance_with_mil_km),
        0x2F: enc.percent(56),
        0x30: enc.u8(warmups_since_clear),
        0x31: enc.distance_km(distance_since_clear_km),
        0x33: enc.pressure_kpa(101),
        0x3C: enc.catalyst_temperature(438),
        0x42: enc.control_module_voltage(voltage),
        0x43: enc.u16(round(31 * 255 / 100)),
        0x44: enc.lambda_ratio(1.0),
        0x46: enc.temperature(18),
        0x4D: enc.minutes(minutes_with_mil),
        0x4E: enc.minutes(minutes_since_clear),
        0x51: bytes([0x01]),  # petrol
        0x5C: enc.temperature(94),
    }
    if ltft_bank2 is not None:
        pids[0x08] = enc.trim_percent(-1.1)
        pids[0x09] = enc.trim_percent(ltft_bank2)
    if odometer_km is not None:
        pids[0xA6] = enc.odometer_km(odometer_km)
    return pids


_FREEZE_FRAME = {
    0x02: bytes([0x04, 0x20]),  # the DTC that stored this frame: P0420
    0x04: enc.percent(41),
    0x05: enc.temperature(91),
    0x0C: enc.engine_rpm(2180),
    0x0D: enc.u8(74),
    0x10: enc.maf_rate(14.2),
    0x11: enc.percent(28),
}

_CAL_IDS = ("CARPISIM-CAL-001",)
_CVNS = (bytes.fromhex("A1B2C3D4"),)


def _engine(
    pids: dict[int, bytes],
    **extra: object,
) -> EcuSpec:
    return EcuSpec(
        response_id=0x7E8,
        label="Engine control module",
        pids=pids,
        vin=SIM_VIN,
        calibration_ids=_CAL_IDS,
        cvns=_CVNS,
        ecu_name="CARPI-SIM-ECM",
        **extra,  # type: ignore[arg-type]
    )


# --- The scenarios ---------------------------------------------------------------

_HEALTHY = Scenario(
    name="healthy",
    summary="A well-maintained car with nothing to report. The control case.",
    ecus=(
        _engine(
            _engine_pids(odometer_km=96_420),
            monitor_tests=(
                (0x21, 0x80, 0x0B, 40, 0, 128),  # catalyst bank 1, comfortably inside
                (0x01, 0x81, 0x0B, 62, 20, 140),  # oxygen sensor bank 1 sensor 1
            ),
        ),
    ),
    claimed_odometer_km=96_500,
    expect_findings=(),
)

_RECENTLY_CLEARED = Scenario(
    name="recently-cleared",
    summary=(
        "The seller wiped the fault codes shortly before the viewing. No stored faults, "
        "but the self-tests have not re-run and a permanent code the seller cannot erase "
        "is still there."
    ),
    ecus=(
        _engine(
            _engine_pids(
                incomplete={"catalyst", "evap_system", "oxygen_sensor", "egr_vvt_system"},
                distance_since_clear_km=12,
                warmups_since_clear=2,
                minutes_since_clear=38,
                odometer_km=178_300,
            ),
            # Only the ECU can clear this, and only after the catalyst monitor passes
            # repeatedly. It survives the wipe, which is the whole point.
            permanent_dtcs=("P0420",),
        ),
    ),
    claimed_odometer_km=178_000,
    expect_findings=("permanent-dtcs-present", "recent-code-clear"),
)

_FAILING_CATALYST = Scenario(
    name="failing-catalyst",
    summary=(
        "Warning light on for a spent catalytic converter, driven a long way in that "
        "state, plus a transmission fault in a second module."
    ),
    ecus=(
        _engine(
            _engine_pids(
                mil_on=True,
                dtc_count=1,
                distance_with_mil_km=1_240,
                minutes_with_mil=1_910,
                distance_since_clear_km=5_400,
                odometer_km=203_770,
            ),
            stored_dtcs=("P0420",),
            pending_dtcs=("P0430",),
            permanent_dtcs=("P0420",),
            freeze_frame=_FREEZE_FRAME,
            monitor_tests=(
                (0x21, 0x80, 0x0B, 121, 0, 128),  # catalyst almost at its limit
                (0x01, 0x81, 0x0B, 58, 20, 140),
            ),
        ),
        EcuSpec(
            response_id=0x7E9,
            label="Transmission control module",
            pids={
                0x01: enc.monitor_status(mil_on=True, dtc_count=1),
                0x05: enc.temperature(88),
                0x1F: enc.seconds(900),
            },
            stored_dtcs=("P0730",),
            ecu_name="CARPI-SIM-TCM",
        ),
    ),
    claimed_odometer_km=203_500,
    expect_findings=(
        "permanent-dtcs-present",
        "mil-driven-far",
        "mil-on",
        "pending-dtcs-present",
    ),
)

_LEAN_BANK_ONE = Scenario(
    name="lean-bank1",
    summary=(
        "No fault codes yet, but bank 1 is being fuelled 17 percent richer than baseline "
        "to compensate for something. An air leak in the making."
    ),
    ecus=(
        _engine(
            _engine_pids(
                ltft_bank1=17.2,
                ltft_bank2=1.4,
                stft_bank1=6.3,
                odometer_km=132_050,
            ),
        ),
    ),
    claimed_odometer_km=132_000,
    expect_findings=(
        "fuel-trim-banks-diverge",
        "fuel-trim-excessive-bank1",
    ),
)

_MILEAGE_TAMPERED = Scenario(
    name="mileage-tampered",
    summary=(
        "Advertised at 145,000 km, but the engine controller still holds 285,400. The "
        "cluster was rewritten and this module was not."
    ),
    ecus=(_engine(_engine_pids(odometer_km=285_400)),),
    claimed_odometer_km=145_000,
    expect_findings=("odometer-disagrees-with-advertised",),
)

_COLD_RUNNING = Scenario(
    name="cold-running",
    summary=(
        "Thermostat stuck open and a charging system on the way out. Cheap faults that "
        "also suppress the emissions self-tests, so they can mask worse news."
    ),
    ecus=(
        _engine(
            _engine_pids(
                coolant_c=58,
                run_time_s=1_450,
                voltage=12.4,
                incomplete={"catalyst", "evap_system"},
                distance_since_clear_km=4_300,
                odometer_km=158_900,
            ),
        ),
    ),
    claimed_odometer_km=159_000,
    expect_findings=(
        "control-module-voltage-low",
        "coolant-not-reaching-temperature",
        "monitors-incomplete",
    ),
)

SCENARIOS: dict[str, Scenario] = {
    scenario.name: scenario
    for scenario in (
        _HEALTHY,
        _RECENTLY_CLEARED,
        _FAILING_CATALYST,
        _LEAN_BANK_ONE,
        _MILEAGE_TAMPERED,
        _COLD_RUNNING,
    )
}


def get_scenario(name: str) -> Scenario:
    """Look up a scenario by name."""
    try:
        return SCENARIOS[name]
    except KeyError:
        raise KeyError(
            f"no scenario named {name!r}. Available: {', '.join(sorted(SCENARIOS))}"
        ) from None
