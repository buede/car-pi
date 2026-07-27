"""The UDS layer, against a simulated vehicle.

The most important test here is :class:`TestNothingCanWrite`. UDS is the protocol that
*can* reprogram a car, so the guarantee that this implementation cannot is worth more
than any feature in it. It is asserted three ways: the write services are absent from
the API, the client refuses to emit them if one is ever routed through it, and no
simulated module ever receives one.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from carpi.core.discovery import observe_traffic, sweep_addresses
from carpi.core.protocol.uds import (
    FORBIDDEN_SERVICES,
    STANDARD_DIDS,
    DiagnosticSession,
    UdsClient,
    UdsDtc,
    UdsError,
    UdsNegativeResponse,
)
from carpi.core.transport.base import EcuAddress
from carpi.core.transport.canbus import CanLink
from carpi.sim import SimulatedVehicle, get_scenario

CLUSTER = EcuAddress(tx_id=0x714, rx_id=0x77E)
ENGINE = EcuAddress(tx_id=0x7E0, rx_id=0x7E8)


@pytest.fixture(scope="module")
def cluster_car() -> Iterator[tuple[SimulatedVehicle, CanLink]]:
    """The cluster-tampered scenario, shared across this module's tests."""
    scenario = get_scenario("cluster-tampered")
    channel = "carpi-uds-tests"
    vehicle = SimulatedVehicle.from_scenario(scenario, channel=channel)
    with vehicle, CanLink.open("virtual", channel) as link:
        yield vehicle, link


@pytest.fixture
def cluster(cluster_car) -> UdsClient:
    _, link = cluster_car
    return UdsClient(link.channel(CLUSTER), timeout=0.3)


class TestNothingCanWrite:
    """See the module docstring. Three independent guarantees."""

    def test_the_client_exposes_no_write_method(self) -> None:
        suspicious = [
            name
            for name in dir(UdsClient)
            if any(
                word in name.lower()
                for word in ("write", "clear", "erase", "reset", "security", "routine", "transfer")
            )
        ]
        assert suspicious == []

    @pytest.mark.parametrize("service", sorted(FORBIDDEN_SERVICES))
    def test_the_client_refuses_to_emit_a_write_service(
        self, cluster: UdsClient, service: int
    ) -> None:
        """A backstop: if a future refactor routes a write through _exchange, it fails."""
        with pytest.raises(UdsError, match="read-only"):
            cluster._exchange(bytes([service, 0x00]))

    def test_no_module_ever_receives_a_write_service(self, cluster_car) -> None:
        vehicle, _ = cluster_car
        for ecu in vehicle.ecus:
            services = {request[0] for request in ecu.received if request}
            forbidden = services & set(FORBIDDEN_SERVICES)
            assert forbidden == set(), (
                f"{ecu.label} received {[f'0x{s:02X}' for s in sorted(forbidden)]}"
            )

    def test_a_programming_session_cannot_be_requested(self, cluster: UdsClient) -> None:
        """Session 0x02 is the gateway to reflashing and is refused before it is sent."""
        with pytest.raises(UdsError, match="not permitted"):
            cluster.start_session(0x02)


class TestSessionAndPresence:
    def test_tester_present(self, cluster: UdsClient) -> None:
        assert cluster.tester_present() is True

    def test_absent_module_does_not_answer(self, cluster_car) -> None:
        _, link = cluster_car
        nobody = UdsClient(link.channel(EcuAddress(tx_id=0x730, rx_id=0x7A0)), timeout=0.2)
        assert nobody.tester_present() is False

    def test_extended_session_is_accepted(self, cluster: UdsClient) -> None:
        assert cluster.start_session(DiagnosticSession.EXTENDED) is True

    def test_identifier_needing_the_extended_session(self, cluster_car) -> None:
        """In the default session it looks exactly like an identifier that is absent.

        Which is why profiles request the extended session before reading anything --
        without it, a perfectly correct definition appears to be wrong.
        """
        _, link = cluster_car
        fresh = UdsClient(link.channel(CLUSTER), timeout=0.3)
        fresh.start_session(DiagnosticSession.DEFAULT)
        with pytest.raises(UdsNegativeResponse) as info:
            fresh.read_did(0xCAFC)
        assert info.value.nrc == 0x7F

        fresh.start_session(DiagnosticSession.EXTENDED)
        assert fresh.read_did(0xCAFC) == b"CARPI-CLUSTER-01"


class TestReadDataByIdentifier:
    def test_reads_a_manufacturer_identifier(self, cluster: UdsClient) -> None:
        cluster.start_session()
        assert int.from_bytes(cluster.read_did(0xCAFE), "big") == 145_000

    def test_unsupported_identifier_is_reported_as_such(self, cluster: UdsClient) -> None:
        cluster.start_session()
        with pytest.raises(UdsNegativeResponse) as info:
            cluster.read_did(0x1234)
        assert info.value.is_unsupported
        assert not info.value.is_protected

    def test_protected_identifier_is_distinguished_from_absent(self, cluster: UdsClient) -> None:
        """NRC 0x33 is a positive discovery: the identifier exists and is locked."""
        cluster.start_session()
        with pytest.raises(UdsNegativeResponse) as info:
            cluster.read_did(0xCAFB)
        assert info.value.nrc == 0x33
        assert info.value.is_protected
        assert not info.value.is_unsupported

    def test_out_of_range_identifier_is_rejected_locally(self, cluster: UdsClient) -> None:
        with pytest.raises(UdsError, match="out of range"):
            cluster.read_did(0x1FFFF)

    def test_read_dids_skips_what_is_unavailable(self, cluster: UdsClient) -> None:
        cluster.start_session()
        found = cluster.read_dids([0xCAFE, 0x1234, 0xCAFD])
        assert set(found) == {0xCAFE, 0xCAFD}


class TestIdentification:
    def test_reads_the_standard_block(self, cluster: UdsClient) -> None:
        cluster.start_session()
        identity = cluster.identification()
        assert identity["vin"]["text"] == "CARPI0SIMULATED01"
        assert identity["ecu_serial_number"]["text"] == "CLU-0000001"

    def test_did_is_reported_in_hex(self, cluster: UdsClient) -> None:
        """0xF190 is how people quote it; 61840 is not."""
        cluster.start_session()
        assert cluster.identification()["vin"]["did"] == "0xF190"

    def test_binary_payloads_are_not_forced_into_text(self, cluster: UdsClient) -> None:
        """A BCD date is not ASCII, and mojibake would be worse than the hex."""
        cluster.start_session()
        date = cluster.identification()["programming_date"]
        assert date["text"] is None
        assert date["raw"] == "20250903"

    def test_standard_dids_are_the_iso_block(self) -> None:
        assert STANDARD_DIDS[0xF190] == "vin"
        assert STANDARD_DIDS[0xF199] == "programming_date"
        assert all(0xF180 <= did <= 0xF19F for did in STANDARD_DIDS)


class TestDtcDecoding:
    @pytest.mark.parametrize(
        ("high", "middle", "low", "expected"),
        [
            (0x04, 0x20, 0x08, "P0420-08"),
            (0xD0, 0x12, 0x08, "U1012-08"),  # top two bits 11 -> network
            (0x90, 0x12, 0x08, "B1012-08"),  # 10 -> body
            (0x04, 0x20, 0x00, "P0420-00"),
            (0x81, 0x43, 0x1A, "B0143-1A"),
        ],
    )
    def test_three_byte_codes(self, high: int, middle: int, low: int, expected: str) -> None:
        """The failure-type byte is what the two-byte OBD-II format cannot express."""
        assert UdsDtc(high, middle, low, 0x00).code == expected

    def test_status_flags(self) -> None:
        dtc = UdsDtc(0x04, 0x20, 0x08, 0x08)
        assert dtc.confirmed is True
        assert dtc.warning_requested is False
        assert "confirmed" in dtc.flags

    def test_warning_indicator_bit(self) -> None:
        assert UdsDtc(0x04, 0x20, 0x08, 0x80).warning_requested is True

    def test_reads_dtcs_from_a_module_obd_cannot_reach(self, cluster: UdsClient) -> None:
        """A cluster fault is invisible to generic OBD-II, which only sees powertrain."""
        cluster.start_session()
        codes = cluster.read_dtcs()
        assert [dtc.code for dtc in codes] == ["U1012-08"]
        assert codes[0].confirmed

    def test_count_matches_the_list(self, cluster: UdsClient) -> None:
        cluster.start_session()
        assert cluster.count_dtcs() == len(cluster.read_dtcs())

    def test_status_mask_filters(self, cluster: UdsClient) -> None:
        cluster.start_session()
        # 0x40 is testNotCompletedThisCycle, which the fixture's DTC does not set.
        assert cluster.read_dtcs(0x40) == []


class TestAddressDiscovery:
    def test_obd_discovery_cannot_see_the_cluster(self, cluster_car) -> None:
        """The premise of the whole sweep: a cluster implements no OBD-II."""
        _, link = cluster_car
        found = {address.rx_id for address in link.discover_ecus(timeout=0.4)}
        assert found == {0x7E8}
        assert 0x77E not in found

    def test_sweep_finds_the_cluster(self, cluster_car) -> None:
        _, link = cluster_car
        stats = sweep_addresses(
            link, low=0x700, high=0x7FF, request_delay=0.0, response_window=0.03
        )
        pairs = {(module.request_id, module.response_id) for module in stats.modules}
        assert (0x714, 0x77E) in pairs

    def test_sweep_reports_the_physical_obd_address_not_the_broadcast(self, cluster_car) -> None:
        """0x7DF is a broadcast, not a module, and crediting it there hides 0x7E0."""
        _, link = cluster_car
        stats = sweep_addresses(
            link, low=0x7D0, high=0x7EF, request_delay=0.0, response_window=0.03
        )
        pairs = {(module.request_id, module.response_id) for module in stats.modules}
        assert (0x7E0, 0x7E8) in pairs
        assert not any(request == 0x7DF for request, _ in pairs)

    def test_sweep_marks_which_modules_obd_would_have_found(self, cluster_car) -> None:
        _, link = cluster_car
        stats = sweep_addresses(
            link, low=0x700, high=0x7FF, request_delay=0.0, response_window=0.03
        )
        by_id = {module.response_id: module for module in stats.modules}
        assert by_id[0x7E8].is_obd_address is True
        assert by_id[0x77E].is_obd_address is False

    def test_a_write_service_cannot_be_used_as_a_probe(self, cluster_car) -> None:
        """Discovery sends to addresses whose purpose is unknown."""
        _, link = cluster_car
        with pytest.raises(ValueError, match="never be used as a probe"):
            sweep_addresses(link, low=0x700, high=0x701, probe=bytes([0x02, 0x2E, 0x00]))

    def test_observe_traffic_sends_nothing(self, cluster_car) -> None:
        """Passive mapping is the safest possible first contact with a vehicle."""
        vehicle, link = cluster_car
        before = sum(len(ecu.received) for ecu in vehicle.ecus)
        observe_traffic(link, 0.2)
        assert sum(len(ecu.received) for ecu in vehicle.ecus) == before
