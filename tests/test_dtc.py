"""DTC encoding, per SAE J2012."""

from __future__ import annotations

import pytest

from carpi.core.protocol.dtc import (
    DtcCountMismatch,
    decode_dtc,
    describe_dtc,
    encode_dtc,
    parse_dtc_response,
)


class TestDescribe:
    """What the standard fixes about a code, for a reader with no internet.

    Deliberately shallow. A per-code fault description that is wrong does not fail
    loudly -- it sends somebody to replace the wrong part -- so nothing here goes beyond
    what J2012 actually promises.
    """

    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("P0420", "auxiliary emission controls"),
            ("P0730", "transmission"),
            ("P0301", "ignition system or misfire"),
            ("P0A80", "hybrid propulsion"),
        ],
    )
    def test_standardised_powertrain_codes_name_their_subsystem(
        self, code: str, expected: str
    ) -> None:
        meaning = describe_dtc(code)
        assert meaning is not None
        assert meaning.subsystem == expected
        assert meaning.standardised is True

    @pytest.mark.parametrize(
        ("code", "letter"),
        [("P0420", "powertrain"), ("C0035", "chassis"), ("B0092", "body"), ("U0100", "network")],
    )
    def test_the_letter_names_the_part_of_the_car(self, code: str, letter: str) -> None:
        meaning = describe_dtc(code)
        assert meaning is not None
        assert meaning.system.startswith(letter)

    @pytest.mark.parametrize("code", ["P1234", "P3000", "B1001", "U1012"])
    def test_manufacturer_codes_say_so_and_claim_no_subsystem(self, code: str) -> None:
        """The subsystem allocation binds only the codes makers must use identically.

        Reading it off a manufacturer-specific code would invent a meaning the standard
        does not give it -- and P1xxx means different faults on different makes.
        """
        meaning = describe_dtc(code)
        assert meaning is not None
        assert meaning.standardised is False
        assert meaning.subsystem is None
        assert "manufacturer-specific" in meaning.summary

    def test_a_uds_failure_type_suffix_is_tolerated(self) -> None:
        """UDS codes arrive as "P0420-08"; the suffix is a failure type, not a digit."""
        meaning = describe_dtc("P0420-08")
        assert meaning is not None
        assert meaning.code == "P0420"

    @pytest.mark.parametrize("code", ["", "nonsense", "X0420", "P042", "P0420X", "P4420"])
    def test_a_malformed_code_describes_nothing(self, code: str) -> None:
        """Better to show the bare code than to invent a reading of it."""
        assert describe_dtc(code) is None


class TestDecode:
    @pytest.mark.parametrize(
        ("high", "low", "expected"),
        [
            (0x01, 0x43, "P0143"),
            (0x41, 0x43, "C0143"),  # top two bits 01 -> chassis
            (0x81, 0x43, "B0143"),  # 10 -> body
            (0xC1, 0x43, "U0143"),  # 11 -> network
            (0x20, 0x96, "P2096"),  # first digit 2
            (0x30, 0x00, "P3000"),  # first digit 3, the highest allowed
            (0x1A, 0x2B, "P1A2B"),  # later digits are hexadecimal, not decimal
            (0x01, 0x00, "P0100"),
        ],
    )
    def test_decodes(self, high: int, low: int, expected: str) -> None:
        assert decode_dtc(high, low) == expected

    def test_all_zero_is_padding_not_a_fault(self) -> None:
        """P0000 is not a code. Reporting it as one invents a fault the car doesn't have."""
        assert decode_dtc(0x00, 0x00) is None

    @pytest.mark.parametrize("code", ["P0143", "C0143", "B0143", "U0143", "P2096", "P1A2B"])
    def test_round_trip(self, code: str) -> None:
        assert decode_dtc(*encode_dtc(code)) == code


class TestEncode:
    @pytest.mark.parametrize("code", ["P014", "P01432", "X0143", "P0G43", "P4143", ""])
    def test_rejects_malformed(self, code: str) -> None:
        with pytest.raises(ValueError):
            encode_dtc(code)

    def test_accepts_lowercase_and_whitespace(self) -> None:
        assert encode_dtc(" p0143 ") == (0x01, 0x43)


class TestParseResponse:
    def test_with_count_byte(self) -> None:
        assert parse_dtc_response(bytes.fromhex("43 02 0143 0196")) == ["P0143", "P0196"]

    def test_without_count_byte(self) -> None:
        """Not every ECU sends the count byte, so parity decides which layout it is."""
        assert parse_dtc_response(bytes.fromhex("43 0143 0196")) == ["P0143", "P0196"]

    def test_padding_pairs_are_dropped(self) -> None:
        assert parse_dtc_response(bytes.fromhex("43 01 0143 0000")) == ["P0143"]

    def test_no_faults(self) -> None:
        assert parse_dtc_response(bytes.fromhex("43 00")) == []

    @pytest.mark.parametrize("sid", ["43", "47", "4A"])
    def test_accepts_all_three_dtc_modes(self, sid: str) -> None:
        assert parse_dtc_response(bytes.fromhex(f"{sid} 01 0143")) == ["P0143"]

    def test_count_disagreement_is_raised_with_the_codes_attached(self) -> None:
        """A caller needs both the anomaly and the codes -- it must not have to choose."""
        with pytest.raises(DtcCountMismatch) as info:
            parse_dtc_response(bytes.fromhex("43 03 0143"))
        assert info.value.declared == 3
        assert info.value.codes == ["P0143"]

    def test_rejects_wrong_service_id(self) -> None:
        with pytest.raises(ValueError, match="not a DTC response"):
            parse_dtc_response(bytes.fromhex("41 00"))

    def test_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            parse_dtc_response(b"")
