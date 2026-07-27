"""OBD-II diagnostic addressing arithmetic, per ISO 15765-4.

The 29-bit layout swaps the target and source bytes between request and reply, which
is the easy thing to get backwards. Getting it wrong produces a scanner that hears
replies and never manages to address a module directly.
"""

from __future__ import annotations

import pytest

from carpi.core.transport.base import (
    FUNCTIONAL_REQUEST_11BIT,
    FUNCTIONAL_REQUEST_29BIT,
    EcuAddress,
)


class TestStandardAddressing:
    @pytest.mark.parametrize(
        ("rx_id", "expected_tx"),
        [(0x7E8, 0x7E0), (0x7E9, 0x7E1), (0x7EF, 0x7E7)],
    )
    def test_request_id_is_reply_minus_eight(self, rx_id: int, expected_tx: int) -> None:
        address = EcuAddress.from_response_id(rx_id, extended=False)
        assert address.tx_id == expected_tx
        assert address.rx_id == rx_id
        assert not address.extended

    @pytest.mark.parametrize("rx_id", [0x7E7, 0x7F0, 0x000, 0x123, 0x7DF])
    def test_rejects_ids_outside_the_obd_reply_range(self, rx_id: int) -> None:
        """Ordinary powertrain broadcast traffic must not be mistaken for an ECU reply."""
        with pytest.raises(ValueError, match="not an OBD-II 11-bit response ID"):
            EcuAddress.from_response_id(rx_id, extended=False)

    def test_ecu_number_is_zero_based(self) -> None:
        assert EcuAddress.from_response_id(0x7E8, extended=False).ecu_number == 0
        assert EcuAddress.from_response_id(0x7EB, extended=False).ecu_number == 3

    def test_functional_address(self) -> None:
        address = EcuAddress.functional()
        assert address.tx_id == FUNCTIONAL_REQUEST_11BIT == 0x7DF
        assert address.label == "functional"

    def test_label(self) -> None:
        assert EcuAddress.from_response_id(0x7E8, extended=False).label == "7E0/7E8"


class TestExtendedAddressing:
    @pytest.mark.parametrize(
        ("rx_id", "expected_tx"),
        [
            # Reply 0x18DAF1nn carries the ECU address in the low byte; the request
            # puts it in the *second* byte with the tester's 0xF1 in the low byte.
            (0x18DAF100, 0x18DA00F1),
            (0x18DAF110, 0x18DA10F1),
            (0x18DAF1E9, 0x18DAE9F1),
        ],
    )
    def test_target_and_source_swap(self, rx_id: int, expected_tx: int) -> None:
        address = EcuAddress.from_response_id(rx_id, extended=True)
        assert address.tx_id == expected_tx
        assert address.extended

    @pytest.mark.parametrize("rx_id", [0x18DB33F1, 0x18DA10F1, 0x7E8, 0x1FFFFFFF])
    def test_rejects_ids_without_the_reply_prefix(self, rx_id: int) -> None:
        with pytest.raises(ValueError, match="not an OBD-II 29-bit response ID"):
            EcuAddress.from_response_id(rx_id, extended=True)

    def test_ecu_number_is_the_low_byte(self) -> None:
        assert EcuAddress.from_response_id(0x18DAF10E, extended=True).ecu_number == 0x0E

    def test_functional_address(self) -> None:
        assert EcuAddress.functional(extended=True).tx_id == FUNCTIONAL_REQUEST_29BIT
        assert EcuAddress.functional(extended=True).tx_id == 0x18DB33F1
