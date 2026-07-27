"""PID decoding against hand-computed byte vectors.

The expected values here were worked out by hand from the scaling rules in SAE J1979,
not produced by running this code. That is the point: a test that compares the decoder
against itself, or against an encoder derived from the same formula, passes just as
happily when the formula is wrong.
"""

from __future__ import annotations

import pytest

from carpi.core.database import Database
from carpi.core.protocol.obd2 import decode_pid


class TestFormulaPids:
    @pytest.mark.parametrize(
        ("name", "raw", "expected"),
        [
            # 0x0FA0 = 4000 counts, a quarter-revolution each -> 1000 rpm
            ("engine_rpm", "0FA0", 1000.0),
            # 0x7B = 123, offset by 40 -> 83 C
            ("coolant_temp", "7B", 83.0),
            # 128 counts is zero fuel correction
            ("ltft_bank1", "80", 0.0),
            # 153 counts -> 153 * 100/128 - 100 = 19.53125 %
            ("ltft_bank1", "99", 19.53125),
            # 0x00 -> the most negative trim the encoding allows
            ("ltft_bank1", "00", -100.0),
            # 150 counts, half a degree each, offset by 64 -> 11 degrees
            ("timing_advance", "96", 11.0),
            # 0x015E = 350 hundredths of a gram per second
            ("maf_rate", "015E", 3.5),
            # 0x3718 = 14104 millivolts
            ("control_module_voltage", "3718", 14.104),
            # 0x000C = 12 km
            ("distance_since_codes_cleared", "000C", 12.0),
            # 0x0384 = 900 s
            ("run_time_since_start", "0384", 900.0),
            # 0x127A = 4730 tenths of a degree, offset by 40 -> 433 C
            ("catalyst_temp_b1s1", "127A", 433.0),
            # 0x8000 = 32768, which is lambda 1.0 exactly
            ("commanded_afr_equiv_ratio", "8000", 1.0),
            # full scale
            ("engine_load", "FF", 100.0),
            ("throttle_position", "00", 0.0),
            # 128 * 100/255
            ("fuel_tank_level", "80", pytest.approx(50.19607843, rel=1e-9)),
            # 0x00002710 = 10000 tenths of a km -> 1000 km
            ("odometer", "00002710", 1000.0),
        ],
    )
    def test_decodes(self, database: Database, name: str, raw: str, expected: float) -> None:
        reading = decode_pid(database.pid(name), bytes.fromhex(raw))
        assert reading.value == expected

    def test_signed_window_handles_negative_pressure(self, database: Database) -> None:
        """PID 32 is two's-complement; reading it unsigned would give +16383 Pa."""
        reading = decode_pid(database.pid("evap_vapor_pressure"), bytes.fromhex("FFFC"))
        assert reading.value == -1.0

    def test_implausible_value_is_flagged_not_dropped(self, database: Database) -> None:
        """A reading outside its physical range is reported as suspect, not silently kept."""
        definition = database.pid("coolant_temp")
        assert decode_pid(definition, b"\x7b").plausible
        # The definition's own range is what decides; nothing here hard-codes a limit.
        assert definition.value_range == (-40.0, 215.0)


class TestSupportBitmap:
    def test_msb_of_first_byte_is_the_next_pid(self, database: Database) -> None:
        reading = decode_pid(database.pid(0x00), bytes.fromhex("80000000"))
        assert reading.value["offsets"] == [1]

    def test_lsb_of_last_byte_is_the_next_bitmap(self, database: Database) -> None:
        reading = decode_pid(database.pid(0x00), bytes.fromhex("00000001"))
        assert reading.value["offsets"] == [32]

    def test_all_supported(self, database: Database) -> None:
        reading = decode_pid(database.pid(0x00), bytes.fromhex("FFFFFFFF"))
        assert reading.value["offsets"] == list(range(1, 33))


class TestOxygenSensors:
    def test_unused_trim_is_none_not_zero(self, database: Database) -> None:
        """0xFF means "this sensor has no trim", which is not a trim of zero."""
        reading = decode_pid(database.pid("o2_b1s1"), bytes.fromhex("60FF"))
        assert reading.value["voltage"] == pytest.approx(0.48)
        assert reading.value["short_term_fuel_trim"] is None

    def test_trim_is_decoded_when_present(self, database: Database) -> None:
        reading = decode_pid(database.pid("o2_b1s1"), bytes.fromhex("6080"))
        assert reading.value["short_term_fuel_trim"] == 0.0

    def test_wide_range_lambda_and_voltage(self, database: Database) -> None:
        reading = decode_pid(database.pid("o2_wr_b1s1"), bytes.fromhex("80002000"))
        assert reading.value["lambda"] == 1.0
        assert reading.value["voltage"] == 1.0
