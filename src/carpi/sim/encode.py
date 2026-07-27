"""Encoders that turn engineering values into OBD-II wire bytes.

Written from the scaling rules in SAE J1979 directly, **not** by inverting the
formulas in ``defs/generic/mode01-pids.yaml``. If these were the algebraic inverse of
those formulas, a mistake in a formula would cancel out in every round-trip test and
the tool would confidently report a wrong number to somebody buying a car. Two
independent derivations disagree when one of them is wrong, which is the whole point.
"""

from __future__ import annotations

__all__ = [
    "catalyst_temperature",
    "control_module_voltage",
    "distance_km",
    "engine_rpm",
    "lambda_ratio",
    "maf_rate",
    "minutes",
    "monitor_status",
    "odometer_km",
    "percent",
    "pressure_kpa",
    "seconds",
    "temperature",
    "timing_advance",
    "trim_percent",
    "u8",
    "u16",
    "u32",
]


def u8(value: int) -> bytes:
    return bytes([_clamp(value, 0, 0xFF)])


def u16(value: int) -> bytes:
    return _clamp(value, 0, 0xFFFF).to_bytes(2, "big")


def u32(value: int) -> bytes:
    return _clamp(value, 0, 0xFFFFFFFF).to_bytes(4, "big")


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def temperature(celsius: float) -> bytes:
    """PIDs 05, 0F, 46, 5C: one byte, offset by 40 degrees."""
    return u8(round(celsius) + 40)


def percent(value: float) -> bytes:
    """PIDs 04, 11, 2F and similar: one byte spanning 0 to 100 percent."""
    return u8(round(value * 255 / 100))


def trim_percent(value: float) -> bytes:
    """PIDs 06-09: fuel trim, where 128 counts is zero correction."""
    return u8(round(value * 128 / 100) + 128)


def engine_rpm(rpm: float) -> bytes:
    """PID 0C: two bytes in quarter-revolution steps."""
    return u16(round(rpm * 4))


def maf_rate(grams_per_second: float) -> bytes:
    """PID 10: two bytes in hundredths of a gram per second."""
    return u16(round(grams_per_second * 100))


def timing_advance(degrees: float) -> bytes:
    """PID 0E: half-degree steps, offset by 64 degrees."""
    return u8(round(degrees * 2) + 128)


def pressure_kpa(kpa: float) -> bytes:
    """PIDs 0B, 33: one byte, one kPa per count."""
    return u8(round(kpa))


def control_module_voltage(volts: float) -> bytes:
    """PID 42: two bytes in millivolts."""
    return u16(round(volts * 1000))


def seconds(value: float) -> bytes:
    """PID 1F: two bytes of seconds."""
    return u16(round(value))


def minutes(value: float) -> bytes:
    """PIDs 4D, 4E: two bytes of minutes."""
    return u16(round(value))


def distance_km(value: float) -> bytes:
    """PIDs 21, 31: two bytes of kilometres."""
    return u16(round(value))


def catalyst_temperature(celsius: float) -> bytes:
    """PIDs 3C-3F: two bytes in tenths of a degree, offset by 40."""
    return u16(round((celsius + 40) * 10))


def lambda_ratio(ratio: float) -> bytes:
    """PID 44: two bytes where 32768 counts is lambda 1.0."""
    return u16(round(ratio * 32768))


def odometer_km(value: float) -> bytes:
    """PID A6: four bytes in tenths of a kilometre."""
    return u32(round(value * 10))


# --- PID 01, the readiness monitors ---------------------------------------------
#
# Built from the bit assignments in J1979 rather than from the decoder. Note that the
# wire encodes "NOT complete": a set bit means the self-test has not finished. Passing
# `incomplete` here rather than `complete` keeps this file honest about that, instead
# of quietly inverting somewhere and hiding the polarity.

_CONTINUOUS_BITS = {
    "misfire": (0, 4),
    "fuel_system": (1, 5),
    "components": (2, 6),
}

_NON_CONTINUOUS_SPARK = (
    "catalyst",
    "heated_catalyst",
    "evap_system",
    "secondary_air_system",
    "ac_refrigerant",
    "oxygen_sensor",
    "oxygen_sensor_heater",
    "egr_vvt_system",
)

_NON_CONTINUOUS_COMPRESSION = (
    "nmhc_catalyst",
    "nox_scr_aftertreatment",
    "reserved_c2",
    "boost_pressure",
    "reserved_c4",
    "exhaust_gas_sensor",
    "pm_filter",
    "egr_vvt_system",
)


def monitor_status(
    *,
    mil_on: bool,
    dtc_count: int,
    ignition: str = "spark",
    supported: frozenset[str] | set[str] | None = None,
    incomplete: frozenset[str] | set[str] | None = None,
) -> bytes:
    """Encode PID 01.

    *supported* names the monitors this engine has; omit it for a sensible default of
    every monitor the ignition type normally implements. *incomplete* names those
    whose self-test has not finished -- the set a freshly cleared ECU would report.
    """
    non_continuous = (
        _NON_CONTINUOUS_COMPRESSION if ignition == "compression" else _NON_CONTINUOUS_SPARK
    )
    if supported is None:
        supported = set(_CONTINUOUS_BITS) | {
            name for name in non_continuous if not name.startswith("reserved")
        }
    incomplete = set(incomplete or ())

    unknown = (set(supported) | incomplete) - (set(_CONTINUOUS_BITS) | set(non_continuous))
    if unknown:
        raise ValueError(f"unknown monitor names for {ignition} ignition: {sorted(unknown)}")

    byte_a = (0x80 if mil_on else 0x00) | _clamp(dtc_count, 0, 0x7F)

    byte_b = 0x08 if ignition == "compression" else 0x00
    for name, (support_bit, incomplete_bit) in _CONTINUOUS_BITS.items():
        if name in supported:
            byte_b |= 1 << support_bit
            if name in incomplete:
                byte_b |= 1 << incomplete_bit

    byte_c = 0
    byte_d = 0
    for bit, name in enumerate(non_continuous):
        if name in supported:
            byte_c |= 1 << bit
            if name in incomplete:
                byte_d |= 1 << bit

    return bytes([byte_a, byte_b, byte_c, byte_d])


def support_bitmap(supported_pids: set[int], base: int) -> bytes:
    """Encode a PID support bitmap for the 32 PIDs after *base*.

    The lowest bit doubles as "the next bitmap PID exists", which is what lets a
    scanner walk the whole space in a handful of requests.
    """
    raw = 0
    for offset in range(1, 33):
        if (base + offset) in supported_pids:
            raw |= 1 << (32 - offset)
    return raw.to_bytes(4, "big")
