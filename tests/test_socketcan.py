"""End-to-end scan over real SocketCAN, against a virtual CAN interface.

The rest of the suite runs on python-can's in-process virtual bus, which covers all
the protocol logic but not the transport a real car actually uses. This module runs the
same scan over SocketCAN, so a regression in the path that matters on a Raspberry Pi is
caught in CI rather than in a car park.

Skipped unless ``CARPI_TEST_SOCKETCAN`` names a usable interface. Set it up with::

    sudo modprobe vcan
    sudo ip link add dev vcan0 type vcan
    sudo ip link set up vcan0
"""

from __future__ import annotations

import os

import pytest

from carpi.core.database import Database
from carpi.core.scan import scan_vehicle
from carpi.core.transport.canbus import CanLink, open_bus
from carpi.sim import SimulatedVehicle, VirtualEcu, get_scenario

pytestmark = pytest.mark.socketcan

_INTERFACE = os.environ.get("CARPI_TEST_SOCKETCAN")

requires_socketcan = pytest.mark.skipif(
    not _INTERFACE,
    reason="set CARPI_TEST_SOCKETCAN to a vcan interface name to run these",
)


@pytest.fixture
def socketcan_scan(database: Database):
    """Scan a scenario over SocketCAN.

    Two separate sockets on the same vcan interface: the simulator's and the scanner's.
    A raw CAN socket does not receive its own transmissions by default, so each side
    hears only the other -- which is exactly the topology of a real bus.
    """

    def run(name: str):
        scenario = get_scenario(name)
        assert _INTERFACE is not None
        sim_bus = open_bus("socketcan", _INTERFACE)
        vehicle = SimulatedVehicle([VirtualEcu(spec) for spec in scenario.ecus], bus=sim_bus)
        with vehicle, CanLink.open("socketcan", _INTERFACE) as link:
            result = scan_vehicle(link, database, claimed_odometer_km=scenario.claimed_odometer_km)
        return scenario, result, result.evaluate(database), vehicle

    return run


@requires_socketcan
class TestOverSocketCan:
    def test_discovery_finds_every_module(self, socketcan_scan) -> None:
        scenario, result, _, _ = socketcan_scan("failing-catalyst")
        assert len(result.ecus) == len(scenario.ecus)
        assert {ecu.address.rx_id for ecu in result.ecus} == {0x7E8, 0x7E9}

    def test_multi_frame_vin_is_reassembled(self, socketcan_scan) -> None:
        """A 17-character VIN does not fit one CAN frame, so this proves ISO-TP works."""
        _, result, _, _ = socketcan_scan("healthy")
        assert result.vin == "CARPI0SIMULATED01"

    def test_findings_match_the_virtual_bus_result(self, socketcan_scan) -> None:
        """The transport must not change the conclusion."""
        scenario, _, evaluation, _ = socketcan_scan("recently-cleared")
        fired = tuple(sorted(f.rule_id for f in evaluation.findings))
        assert fired == tuple(sorted(scenario.expect_findings))

    def test_no_clear_codes_request_reaches_the_bus(self, socketcan_scan) -> None:
        _, _, _, vehicle = socketcan_scan("failing-catalyst")
        assert vehicle.clear_code_requests == []
