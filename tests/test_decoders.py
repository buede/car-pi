"""Builtin decoders, with the readiness-monitor polarity pinned down.

PID 01's bytes B and D encode "NOT complete". Getting that backwards would make a
freshly wiped ECU report as fully self-tested, which would defeat the single most
valuable check this tool performs. These tests exist mainly to make that impossible
to break quietly.
"""

from __future__ import annotations

from carpi.core.protocol.decoders import decode, monitor_names


class TestMonitorStatus:
    def test_lamp_and_fault_count(self) -> None:
        value = decode("monitor_status", bytes([0x83, 0x00, 0x00, 0x00]))
        assert value["mil_on"] is True
        assert value["dtc_count"] == 3

    def test_lamp_off(self) -> None:
        value = decode("monitor_status", bytes([0x02, 0x00, 0x00, 0x00]))
        assert value["mil_on"] is False
        assert value["dtc_count"] == 2

    def test_set_bit_means_not_complete(self) -> None:
        """The wire says "incomplete"; the decoder must report the opposite."""
        # C: bits 0 and 5 supported -> catalyst, oxygen_sensor
        # D: bit 5 set -> oxygen_sensor has NOT finished its self-test
        value = decode("monitor_status", bytes([0x00, 0x07, 0x21, 0x20]))
        monitors = value["monitors"]

        assert monitors["catalyst"] == {"supported": True, "complete": True}
        assert monitors["oxygen_sensor"] == {"supported": True, "complete": False}
        assert monitors["heated_catalyst"] == {"supported": False, "complete": False}

    def test_continuous_monitors(self) -> None:
        # B: bits 0,1,2 supported; bit 5 set -> fuel system not complete
        value = decode("monitor_status", bytes([0x00, 0x27, 0x00, 0x00]))
        monitors = value["monitors"]
        assert monitors["misfire"] == {"supported": True, "complete": True}
        assert monitors["fuel_system"] == {"supported": True, "complete": False}
        assert monitors["components"] == {"supported": True, "complete": True}

    def test_unsupported_monitor_is_never_reported_complete(self) -> None:
        """An absent monitor has not passed; it simply does not exist."""
        value = decode("monitor_status", bytes([0x00, 0x00, 0x00, 0x00]))
        assert all(not m["complete"] for m in value["monitors"].values())
        assert all(not m["supported"] for m in value["monitors"].values())

    def test_spark_ignition_by_default(self) -> None:
        value = decode("monitor_status", bytes([0x00, 0x07, 0xFF, 0x00]))
        assert value["ignition"] == "spark"
        assert "catalyst" in value["monitors"]
        assert "pm_filter" not in value["monitors"]

    def test_compression_ignition_remaps_the_monitor_names(self) -> None:
        """Bit 3 of byte B switches the whole C/D layout to the diesel meanings."""
        # B bit 3 set -> compression ignition. C bits 0 and 6, D bit 6 set.
        value = decode("monitor_status", bytes([0x00, 0x0F, 0x41, 0x40]))
        monitors = value["monitors"]
        assert value["ignition"] == "compression"
        assert monitors["nmhc_catalyst"] == {"supported": True, "complete": True}
        assert monitors["pm_filter"] == {"supported": True, "complete": False}
        assert "catalyst" not in monitors

    def test_drive_cycle_variant_omits_lamp_and_count(self) -> None:
        value = decode("monitor_status_drive_cycle", bytes([0xFF, 0x07, 0x01, 0x01]))
        assert "mil_on" not in value
        assert value["monitors"]["catalyst"]["complete"] is False

    def test_monitor_names_differ_by_ignition(self) -> None:
        assert "catalyst" in monitor_names("spark")
        assert "pm_filter" in monitor_names("compression")


class TestEnumerations:
    def test_fuel_system_closed_loop(self) -> None:
        value = decode("fuel_system_status", bytes([0x02, 0x00]))
        assert value["system_1"] == "closed_loop_using_oxygen_sensor"
        assert value["system_2"] == "not_supported"

    def test_unknown_enum_value_is_labelled_not_guessed(self) -> None:
        value = decode("fuel_system_status", bytes([0x40, 0x00]))
        assert value["system_1"] == "unknown_0x40"

    def test_obd_standard(self) -> None:
        assert decode("obd_standard", bytes([0x06]))["standard"] == "EOBD"
        assert "unknown" in decode("obd_standard", bytes([0xFE]))["standard"]

    def test_fuel_type(self) -> None:
        assert decode("fuel_type", bytes([0x04]))["fuel"] == "diesel"
        assert decode("fuel_type", bytes([0x14]))["fuel"] == "hybrid electric"

    def test_oxygen_sensors_present(self) -> None:
        value = decode("o2_sensors_present_2banks", bytes([0x03]))
        assert value["bank_1"] == [True, True, False, False]
        assert value["bank_2"] == [False, False, False, False]
        assert value["count"] == 2

    def test_power_take_off(self) -> None:
        assert decode("aux_input_status", bytes([0x01]))["power_take_off_active"] is True
        assert decode("aux_input_status", bytes([0x00]))["power_take_off_active"] is False


class TestFreezeFrameDtc:
    def test_decodes_the_triggering_code(self) -> None:
        assert decode("dtc_pair", bytes([0x04, 0x20]))["dtc"] == "P0420"
