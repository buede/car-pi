"""TP2.0 and KWP2000, against a simulated VAG car of the KWP2000 era.

A limit worth restating: both sides of this were written from the same protocol
description by the same author, so these tests establish internal consistency and catch
regressions. They do not establish that a 2006 Passat agrees. Only the car does that.
"""

from __future__ import annotations

from collections.abc import Iterator

import can
import pytest

from carpi.core.protocol.kwp2000 import (
    FORBIDDEN_SERVICES,
    KwpClient,
    KwpError,
    KwpNegativeResponse,
    MeasuringValue,
)
from carpi.core.transport.base import NoResponse
from carpi.core.transport.canbus import CanLink
from carpi.core.transport.tp20 import (
    SAFETY_CRITICAL_MODULES,
    VAG_MODULES,
    Tp20Error,
    Tp20Params,
    open_tp20_channel,
)
from carpi.sim.tp20 import Tp20Responder
from carpi.sim.vag import passat_b6_modules

ENGINE = 0x01
CLUSTER = 0x17
COMFORT = 0x46
AIRBAG = 0x15


@pytest.fixture
def passat() -> Iterator[tuple[Tp20Responder, CanLink]]:
    """A simulated Passat-era car. Fresh per test, so writes do not leak between them."""
    bus = can.interface.Bus(interface="virtual", channel="carpi-vag-test")
    responder = Tp20Responder(bus, passat_b6_modules())
    responder.start()
    try:
        with CanLink.open("virtual", "carpi-vag-test") as link:
            yield responder, link
    finally:
        responder.stop()
        bus.shutdown()


def _client(link: CanLink, address: int) -> tuple[object, KwpClient]:
    channel = open_tp20_channel(link, address, timeout=1.0)
    client = KwpClient(channel, timeout=1.0)
    client.start_session()
    return channel, client


class TestChannelSetup:
    def test_opens_a_channel_to_a_present_module(self, passat) -> None:
        _, link = passat
        channel = open_tp20_channel(link, CLUSTER, timeout=1.0)
        try:
            assert channel.logical_address == CLUSTER
            assert "Instruments" in channel.address.label
        finally:
            channel.close()

    def test_an_absent_module_does_not_answer(self, passat) -> None:
        """Silence, exactly as on a real bus -- which is why a scan has to try each one."""
        _, link = passat
        with pytest.raises((NoResponse, Tp20Error)):
            open_tp20_channel(link, 0x37, timeout=0.4)

    def test_ids_are_negotiated_not_fixed(self, passat) -> None:
        """The reason an arbitration-ID sweep cannot find these modules."""
        _, link = passat
        first = open_tp20_channel(link, ENGINE, timeout=1.0)
        second = open_tp20_channel(link, CLUSTER, timeout=1.0)
        try:
            assert first.address.tx_id != second.address.tx_id
        finally:
            first.close()
            second.close()

    def test_out_of_range_address_is_rejected_locally(self, passat) -> None:
        _, link = passat
        with pytest.raises(Tp20Error, match="out of range"):
            open_tp20_channel(link, 0x1FF, timeout=0.2)

    def test_timing_decoding(self) -> None:
        """Top two bits select the unit, low six are the multiplier."""
        assert Tp20Params.decode_timing(0x8A) == pytest.approx(0.1)
        assert Tp20Params.decode_timing(0x01) == pytest.approx(0.0001)
        assert Tp20Params.decode_timing(0x4A) == pytest.approx(0.01)


class TestSegmentation:
    def test_a_multi_frame_reply_is_reassembled(self, passat) -> None:
        """Identification is around 32 bytes, so it cannot fit one frame."""
        _, link = passat
        channel, client = _client(link, CLUSTER)
        try:
            identity = client.identification()
            assert "KOMBIINSTRUMENT" in identity["text"]
            assert len(bytes.fromhex(identity["raw"])) > 8
        finally:
            channel.close()

    def test_a_short_reply_still_works(self, passat) -> None:
        _, link = passat
        channel, client = _client(link, CLUSTER)
        try:
            assert client.tester_present() is True
        finally:
            channel.close()

    def test_sequential_requests_stay_in_step(self, passat) -> None:
        """Sequence numbers advance, so a desync would show up as a wrong answer."""
        _, link = passat
        channel, client = _client(link, ENGINE)
        try:
            for _ in range(6):
                block = client.read_measuring_block(1)
                assert block.group == 1
                assert len(block.values) == 4
        finally:
            channel.close()


class TestReads:
    def test_measuring_block_decoding(self, passat) -> None:
        _, link = passat
        channel, client = _client(link, ENGINE)
        try:
            block = client.read_measuring_block(1)
        finally:
            channel.close()
        # Formula 0x01 is rpm as a*b*0.2: 0x40 * 0x50 * 0.2.
        assert block.values[0].unit == "rpm"
        assert block.values[0].value == pytest.approx(0x40 * 0x50 * 0.2)
        assert all(value.decoded for value in block.values)

    def test_an_unknown_formula_is_not_guessed(self) -> None:
        """A plausible wrong engineering value is worse than an honest unknown."""
        value = MeasuringValue(formula=0xEE, a=0x12, b=0x34)
        assert value.decoded is False
        assert value.value is None
        assert "not decoded" in str(value)

    def test_vag_fault_codes_are_five_digit(self, passat) -> None:
        """VAG shows 16486, not P0300, so they need their own presentation."""
        _, link = passat
        channel, client = _client(link, ENGINE)
        try:
            assert client.read_dtcs() == ["16486"]
        finally:
            channel.close()

    def test_a_module_with_no_faults_returns_nothing(self, passat) -> None:
        _, link = passat
        channel, client = _client(link, CLUSTER)
        try:
            assert client.read_dtcs() == []
        finally:
            channel.close()

    def test_unknown_identifier_is_refused(self, passat) -> None:
        _, link = passat
        channel, client = _client(link, CLUSTER)
        try:
            with pytest.raises(KwpNegativeResponse) as info:
                client.read_local_identifier(0xEE)
            assert info.value.is_unsupported
        finally:
            channel.close()


class TestCrossModuleOdometer:
    def test_the_cluster_and_engine_disagree(self, passat) -> None:
        """The tampering signature: only the cluster gets rewritten."""
        _, link = passat
        readings = {}
        for address in (ENGINE, CLUSTER):
            channel, client = _client(link, address)
            try:
                readings[address] = int.from_bytes(client.read_local_identifier(0x22), "big")
            finally:
                channel.close()

        assert readings[ENGINE] == 285_400
        assert readings[CLUSTER] == 145_000
        assert readings[ENGINE] - readings[CLUSTER] == 140_400


class TestNothingHereCanWrite:
    """The read-only client's guarantee, same three ways as the UDS one."""

    def test_no_write_method_exists(self) -> None:
        suspicious = [
            name
            for name in dir(KwpClient)
            if any(
                word in name.lower()
                for word in ("write", "clear", "erase", "reset", "security", "login", "routine")
            )
        ]
        assert suspicious == []

    @pytest.mark.parametrize("service", sorted(FORBIDDEN_SERVICES))
    def test_the_client_refuses_to_emit_a_write_service(self, passat, service: int) -> None:
        _, link = passat
        channel, client = _client(link, COMFORT)
        try:
            with pytest.raises(KwpError, match="read-only"):
                client._exchange(bytes([service, 0x00]))
        finally:
            channel.close()

    def test_no_module_receives_a_write_during_a_read_session(self, passat) -> None:
        responder, link = passat
        channel, client = _client(link, COMFORT)
        try:
            client.identification()
            client.read_measuring_block(1)
            client.read_dtcs()
        finally:
            channel.close()

        for module in responder.modules:
            assert module.write_attempts == []
            services = {request[0] for request in module.received if request}
            assert services & set(FORBIDDEN_SERVICES) == set()


class TestModuleMap:
    def test_the_well_known_addresses(self) -> None:
        assert VAG_MODULES[0x01] == "Engine"
        assert VAG_MODULES[0x17] == "Instruments"
        assert VAG_MODULES[0x46] == "Central convenience"

    @pytest.mark.parametrize("address", [0x03, 0x15, 0x16, 0x25, 0x2B, 0x44, 0x53])
    def test_dangerous_modules_are_flagged(self, address: int) -> None:
        """Advisory for reads; the coding path refuses these outright."""
        assert address in SAFETY_CRITICAL_MODULES

    @pytest.mark.parametrize("address", [0x01, 0x17, 0x46, 0x09])
    def test_ordinary_modules_are_not_flagged(self, address: int) -> None:
        assert address not in SAFETY_CRITICAL_MODULES
