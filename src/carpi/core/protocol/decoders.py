"""Builtin decoders for PIDs that arithmetic cannot express.

Most Mode 01 PIDs are a scale and an offset, so `mode01-pids.yaml` handles them with
a `formula`. Bitfields and enumerations need real logic, and they live here. A
definition file opts in by naming one: ``decoder: monitor_status``.

Each decoder takes the payload bytes and returns either a scalar or a nested mapping
of scalars. Nested results are flattened into dotted fact keys downstream, so
``o2_b1s1`` returning ``{"voltage": 0.45}`` becomes the fact ``pid.o2_b1s1.voltage``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from carpi.core.protocol.dtc import decode_dtc

__all__ = ["DECODERS", "decode", "monitor_names"]

# --- Readiness monitors ----------------------------------------------------------
#
# PID 01 is the most important PID for inspecting a used car. It carries the warning
# lamp state, the stored fault count, and the readiness monitors -- the ECU's record
# of which self-tests it has managed to run since it was last cleared.
#
# The trap: bytes B and D encode "NOT complete". A set bit means the test has *not*
# finished. Getting this polarity backwards inverts the single most load-bearing
# signal in the whole tool -- a freshly wiped ECU would report as fully tested. The
# `complete` key below is therefore always the negation of the wire bit.

# Continuous monitors, from byte B. (support bit, not-complete bit)
_CONTINUOUS: tuple[tuple[str, int, int], ...] = (
    ("misfire", 0, 4),
    ("fuel_system", 1, 5),
    ("components", 2, 6),
)

# Non-continuous monitors: byte C is support, byte D is not-complete, same bit index.
# The meaning of each bit depends on the ignition type, flagged by bit 3 of byte B.
_NON_CONTINUOUS_SPARK: tuple[str, ...] = (
    "catalyst",
    "heated_catalyst",
    "evap_system",
    "secondary_air_system",
    "ac_refrigerant",
    "oxygen_sensor",
    "oxygen_sensor_heater",
    "egr_vvt_system",
)

_NON_CONTINUOUS_COMPRESSION: tuple[str, ...] = (
    "nmhc_catalyst",
    "nox_scr_aftertreatment",
    "reserved_c2",
    "boost_pressure",
    "reserved_c4",
    "exhaust_gas_sensor",
    "pm_filter",
    "egr_vvt_system",
)


def monitor_names(ignition: str) -> tuple[str, ...]:
    """Monitor names for an ignition type, in bit order."""
    seq = _NON_CONTINUOUS_COMPRESSION if ignition == "compression" else _NON_CONTINUOUS_SPARK
    return tuple(name for name, _, _ in _CONTINUOUS) + seq


def _monitors(byte_b: int, byte_c: int, byte_d: int, ignition: str) -> dict[str, Any]:
    monitors: dict[str, Any] = {}
    for name, support_bit, incomplete_bit in _CONTINUOUS:
        supported = bool(byte_b & (1 << support_bit))
        monitors[name] = {
            "supported": supported,
            # Not-complete bit inverted. See the note above.
            "complete": supported and not (byte_b & (1 << incomplete_bit)),
        }

    seq = _NON_CONTINUOUS_COMPRESSION if ignition == "compression" else _NON_CONTINUOUS_SPARK
    for bit, name in enumerate(seq):
        supported = bool(byte_c & (1 << bit))
        monitors[name] = {
            "supported": supported,
            "complete": supported and not (byte_d & (1 << bit)),
        }
    return monitors


def monitor_status(data: bytes) -> dict[str, Any]:
    """PID 01 -- warning lamp, stored fault count, and monitor readiness."""
    a, b, c, d = data[0], data[1], data[2], data[3]
    ignition = "compression" if b & 0x08 else "spark"
    return {
        "mil_on": bool(a & 0x80),
        "dtc_count": a & 0x7F,
        "ignition": ignition,
        "monitors": _monitors(b, c, d, ignition),
    }


def monitor_status_drive_cycle(data: bytes) -> dict[str, Any]:
    """PID 41 -- monitor status for the current drive cycle only.

    Byte A is reserved here; the lamp and fault count live in PID 01. Otherwise the
    bit layout matches, so this reports enablement and completion for this cycle.
    """
    b, c, d = data[1], data[2], data[3]
    ignition = "compression" if b & 0x08 else "spark"
    return {"ignition": ignition, "monitors": _monitors(b, c, d, ignition)}


# --- Support bitmaps -------------------------------------------------------------


def pid_support_bitmap(data: bytes) -> dict[str, Any]:
    """PID 00/20/40/... -- which of the next 32 PIDs this ECU supports.

    Returns 1-based offsets from the queried PID, because the decoder does not know
    which bitmap it was given. The protocol layer adds the base. The MSB of byte A is
    offset 1, and offset 32 doubles as "the next bitmap PID exists", which is how
    discovery walks the space without blindly probing all 256 PIDs.
    """
    raw = int.from_bytes(data[:4], "big")
    offsets = [32 - bit for bit in range(32) if raw & (1 << bit)]
    return {"offsets": sorted(offsets), "raw": raw}


def dtc_pair(data: bytes) -> dict[str, Any]:
    """PID 02 -- the DTC that caused the freeze frame to be stored."""
    return {"dtc": decode_dtc(data[0], data[1])}


# --- Enumerations ----------------------------------------------------------------
#
# These are one-hot bitfields in the standard rather than plain integers, so a value
# is looked up by its bit rather than its ordinal.

_FUEL_SYSTEM_STATUS = {
    0x00: "not_supported",
    0x01: "open_loop_engine_cold",
    0x02: "closed_loop_using_oxygen_sensor",
    0x04: "open_loop_high_load_or_fuel_cut",
    0x08: "open_loop_system_failure",
    0x10: "closed_loop_with_feedback_fault",
}


def fuel_system_status(data: bytes) -> dict[str, Any]:
    """PID 03 -- fuelling loop state for up to two fuel systems.

    ``closed_loop_using_oxygen_sensor`` is the healthy warm state. Anything else on a
    fully warmed engine is worth a second look.
    """
    return {
        "system_1": _FUEL_SYSTEM_STATUS.get(data[0], f"unknown_0x{data[0]:02X}"),
        "system_2": _FUEL_SYSTEM_STATUS.get(data[1], f"unknown_0x{data[1]:02X}")
        if len(data) > 1
        else None,
    }


_SECONDARY_AIR_STATUS = {
    0x01: "upstream_of_catalyst",
    0x02: "downstream_of_catalyst",
    0x04: "from_outside_atmosphere_or_off",
    0x08: "pump_commanded_on_for_diagnostics",
}


def secondary_air_status(data: bytes) -> dict[str, Any]:
    """PID 12 -- commanded secondary air injection state."""
    return {"status": _SECONDARY_AIR_STATUS.get(data[0], f"unknown_0x{data[0]:02X}")}


def o2_sensors_present_2banks(data: bytes) -> dict[str, Any]:
    """PID 13 -- which oxygen sensors exist, two banks of four."""
    raw = data[0]
    return {
        "bank_1": [bool(raw & (1 << bit)) for bit in range(4)],
        "bank_2": [bool(raw & (1 << bit)) for bit in range(4, 8)],
        "count": bin(raw).count("1"),
    }


def o2_sensors_present_4banks(data: bytes) -> dict[str, Any]:
    """PID 1D -- which oxygen sensors exist, four banks of two."""
    raw = data[0]
    return {
        f"bank_{bank + 1}": [bool(raw & (1 << (bank * 2 + s))) for s in range(2)]
        for bank in range(4)
    } | {"count": bin(raw).count("1")}


_OBD_STANDARDS = {
    1: "OBD-II (California ARB)",
    2: "OBD (US EPA)",
    3: "OBD and OBD-II",
    4: "OBD-I",
    5: "not OBD compliant",
    6: "EOBD",
    7: "EOBD and OBD-II",
    8: "EOBD and OBD",
    9: "EOBD, OBD and OBD-II",
    10: "JOBD",
    11: "JOBD and OBD-II",
    12: "JOBD and EOBD",
    13: "JOBD, EOBD and OBD-II",
    17: "EMD",
    18: "EMD+",
    19: "HD OBD-C",
    20: "HD OBD",
    21: "WWH OBD",
    23: "HD EOBD-I",
    24: "HD EOBD-I N",
    25: "HD EOBD-II",
    26: "HD EOBD-II N",
    28: "OBDBr-1",
    29: "OBDBr-2",
    30: "KOBD",
    31: "IOBD-I",
    32: "IOBD-II",
    33: "HD EOBD-IV",
}


def obd_standard(data: bytes) -> dict[str, Any]:
    """PID 1C -- which OBD standard the vehicle claims to conform to."""
    return {"standard": _OBD_STANDARDS.get(data[0], f"unknown ({data[0]})"), "raw": data[0]}


_FUEL_TYPES = {
    0: "not available",
    1: "petrol",
    2: "methanol",
    3: "ethanol",
    4: "diesel",
    5: "LPG",
    6: "CNG",
    7: "propane",
    8: "electric",
    9: "bifuel petrol",
    10: "bifuel methanol",
    11: "bifuel ethanol",
    12: "bifuel LPG",
    13: "bifuel CNG",
    14: "bifuel propane",
    15: "bifuel electric",
    16: "bifuel electric and combustion",
    17: "hybrid petrol",
    18: "hybrid ethanol",
    19: "hybrid diesel",
    20: "hybrid electric",
    21: "hybrid mixed fuel",
    22: "hybrid regenerative",
}


def fuel_type(data: bytes) -> dict[str, Any]:
    """PID 51 -- fuel the vehicle runs on."""
    return {"fuel": _FUEL_TYPES.get(data[0], f"unknown ({data[0]})"), "raw": data[0]}


def aux_input_status(data: bytes) -> dict[str, Any]:
    """PID 1E -- auxiliary input state. Only the power-take-off bit is defined."""
    return {"power_take_off_active": bool(data[0] & 0x01)}


# --- Oxygen sensors --------------------------------------------------------------


def o2_voltage_trim(data: bytes) -> dict[str, Any]:
    """PID 14-1B -- narrow-band sensor voltage, plus its short-term trim.

    ``0xFF`` in the trim byte means this sensor is not used for trim, which is
    distinct from a trim of zero and must not be reported as a number.
    """
    trim = None if data[1] == 0xFF else data[1] * 100 / 128 - 100
    return {"voltage": data[0] / 200, "short_term_fuel_trim": trim}


def o2_lambda_voltage(data: bytes) -> dict[str, Any]:
    """PID 24-2B -- wide-range sensor equivalence ratio and voltage."""
    return {
        "lambda": int.from_bytes(data[0:2], "big") / 32768,
        "voltage": int.from_bytes(data[2:4], "big") / 8192,
    }


def o2_lambda_current(data: bytes) -> dict[str, Any]:
    """PID 34-3B -- wide-range sensor equivalence ratio and current."""
    return {
        "lambda": int.from_bytes(data[0:2], "big") / 32768,
        "current_ma": int.from_bytes(data[2:4], "big") / 256 - 128,
    }


DECODERS: dict[str, Callable[[bytes], Any]] = {
    "monitor_status": monitor_status,
    "monitor_status_drive_cycle": monitor_status_drive_cycle,
    "pid_support_bitmap": pid_support_bitmap,
    "dtc_pair": dtc_pair,
    "fuel_system_status": fuel_system_status,
    "secondary_air_status": secondary_air_status,
    "o2_sensors_present_2banks": o2_sensors_present_2banks,
    "o2_sensors_present_4banks": o2_sensors_present_4banks,
    "obd_standard": obd_standard,
    "aux_input_status": aux_input_status,
    "fuel_type": fuel_type,
    "o2_voltage_trim": o2_voltage_trim,
    "o2_lambda_voltage": o2_lambda_voltage,
    "o2_lambda_current": o2_lambda_current,
}


def decode(name: str, data: bytes) -> Any:
    """Run the named builtin decoder over *data*."""
    try:
        decoder = DECODERS[name]
    except KeyError:
        raise KeyError(f"no builtin decoder named {name!r}") from None
    return decoder(data)
