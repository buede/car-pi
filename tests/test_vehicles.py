"""Vehicle profiles, the decode DSL, and the DID scanner.

The theme running through these is that a wrong definition must fail visibly. A
manufacturer identifier cannot be verified without the car, so the machinery around one
has to assume it might be wrong: short payloads raise, implausible values are excluded
rather than reported, and a fictional profile can never attach itself to a real vehicle.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from carpi.core.database import Database, DefinitionError
from carpi.core.didscan import (
    STATUS_DATA,
    STATUS_PROTECTED,
    STATUS_UNSUPPORTED,
    DidObservation,
    parse_ranges,
    scan_dids,
)
from carpi.core.protocol.uds import UdsClient
from carpi.core.scan import build_facts, scan_vehicle
from carpi.core.transport.canbus import CanLink
from carpi.core.vehicles import DecodeSpec, read_module
from carpi.sim import SimulatedVehicle, get_scenario

PROFILE_ID = "example-simulated"


@pytest.fixture(scope="module")
def tampered(database: Database) -> Iterator[tuple[object, object]]:
    scenario = get_scenario("cluster-tampered")
    channel = "carpi-vehicles-tests"
    vehicle = SimulatedVehicle.from_scenario(scenario, channel=channel)
    with vehicle, CanLink.open("virtual", channel) as link:
        result = scan_vehicle(
            link,
            database,
            claimed_odometer_km=scenario.claimed_odometer_km,
            timeout=0.3,
            discovery_timeout=0.3,
            profile=database.profile(PROFILE_ID),
        )
        yield result, link


class TestDecodeSpec:
    @pytest.mark.parametrize(
        ("spec", "payload", "expected"),
        [
            (DecodeSpec(type="uint", length=3), bytes.fromhex("023668"), 145_000),
            (DecodeSpec(type="uint", length=2, scale=0.1), bytes.fromhex("00C8"), 20.0),
            (DecodeSpec(type="int", length=2), bytes.fromhex("FFFF"), -1),
            (DecodeSpec(type="uint", length=1, add=-40), bytes.fromhex("7B"), 83),
            (DecodeSpec(type="ascii"), b"ABC-123\x00", "ABC-123"),
            (DecodeSpec(type="bcd", length=4), bytes.fromhex("20250903"), "20250903"),
            (DecodeSpec(type="raw", length=2), bytes.fromhex("DEAD"), "dead"),
            (DecodeSpec(type="uint", offset=2, length=2), bytes.fromhex("00000539"), 1337),
        ],
    )
    def test_decodes(self, spec: DecodeSpec, payload: bytes, expected: object) -> None:
        assert spec.decode(payload) == expected

    def test_short_payload_raises_rather_than_padding(self) -> None:
        """Zero-extending would turn a wrong definition into a confident wrong number."""
        with pytest.raises(ValueError, match="need 3 byte"):
            DecodeSpec(type="uint", length=3).decode(bytes.fromhex("0102"))

    def test_offset_past_the_end_raises(self) -> None:
        with pytest.raises(ValueError):
            DecodeSpec(type="uint", offset=8, length=2).decode(bytes.fromhex("0102"))

    def test_range_check(self) -> None:
        spec = DecodeSpec(type="uint", length=3, value_range=(0, 2_000_000))
        assert spec.plausible(145_000)
        assert not spec.plausible(9_000_000)


class TestProfileSelection:
    def test_the_example_profile_loads(self, database: Database) -> None:
        profile = database.profile(PROFILE_ID)
        assert profile.fictional is True
        assert [ecu.name for ecu in profile.ecus] == [
            "Instrument cluster",
            "Engine control module",
        ]

    def test_unknown_profile_raises(self, database: Database) -> None:
        with pytest.raises(DefinitionError, match="no vehicle profile"):
            database.profile("no-such-platform")

    @pytest.mark.parametrize("vin", ["CARPI0SIMULATED01", "WVWZZZ1KZAW000001", None])
    def test_a_fictional_profile_never_matches_a_real_vehicle(
        self, database: Database, vin: str | None
    ) -> None:
        """Otherwise a fixture's invented identifiers get reported as a real car's data."""
        assert database.profile_for_vin(vin) is None

    def test_the_cluster_is_outside_the_obd_range(self, database: Database) -> None:
        """Which is the whole reason the manufacturer path exists."""
        cluster = database.profile(PROFILE_ID).ecus[0]
        assert not 0x7E8 <= cluster.response_id <= 0x7EF


class TestModuleReads:
    def test_reads_the_cluster(self, tampered) -> None:
        result, _ = tampered
        cluster = next(r for r in result.module_readings if r.ecu.name == "Instrument cluster")
        assert cluster.reached
        assert cluster.values["odometer_km"] == 145_000
        assert cluster.values["cluster_part_number"] == "CARPI-CLUSTER-01"

    def test_a_locked_identifier_is_recorded_as_protected(self, database: Database) -> None:
        """Not as missing: it is a positive statement about what the module holds."""
        scenario = get_scenario("cluster-tampered")
        with (
            SimulatedVehicle.from_scenario(scenario, channel="carpi-locked") as _vehicle,
            CanLink.open("virtual", "carpi-locked") as link,
        ):
            from carpi.core.vehicles import DecodeSpec as Spec
            from carpi.core.vehicles import EcuProfile, VehicleRead

            profile = EcuProfile(
                name="Instrument cluster",
                request_id=0x714,
                response_id=0x77E,
                reads=(VehicleRead(id="locked", did=0xCAFB, decode=Spec(type="uint", length=1)),),
            )
            reading = read_module(UdsClient(link.channel(profile.address), timeout=0.3), profile)

        assert reading.protected == ("locked",)
        assert reading.unavailable == ()

    def test_an_unreachable_module_is_not_reported_as_empty(self, database: Database) -> None:
        """ "Did not answer" and "answered with nothing" must not look the same."""
        from carpi.core.vehicles import EcuProfile, VehicleRead

        scenario = get_scenario("healthy")
        with (
            SimulatedVehicle.from_scenario(scenario, channel="carpi-absent") as _vehicle,
            CanLink.open("virtual", "carpi-absent") as link,
        ):
            profile = EcuProfile(
                name="Nothing here",
                request_id=0x733,
                response_id=0x7A3,
                reads=(
                    VehicleRead(
                        id="whatever", did=0xCAFE, decode=DecodeSpec(type="uint", length=1)
                    ),
                ),
            )
            reading = read_module(UdsClient(link.channel(profile.address), timeout=0.2), profile)

        assert reading.reached is False
        assert reading.values == {}


class TestCrossModuleOdometer:
    def test_both_modules_report_and_they_disagree(self, tampered) -> None:
        result, _ = tampered
        odometers = result.odometer_by_module
        assert odometers == {
            "Instrument cluster": 145_000.0,
            "Engine control module": 285_400.0,
        }

    def test_the_spread_becomes_a_fact(self, tampered) -> None:
        result, _ = tampered
        facts = build_facts(result)
        assert facts["vehicle.odometer_spread_km"] == 140_400
        assert facts["vehicle.odometer_module_count"] == 2

    def test_the_finding_fires_and_is_critical(self, tampered, database: Database) -> None:
        result, _ = tampered
        evaluation = result.evaluate(database)
        finding = next(f for f in evaluation.findings if f.rule_id == "cross-ecu-odometer-mismatch")
        assert finding.severity == "critical"

    def test_one_module_alone_produces_no_spread(self, database: Database) -> None:
        """A single source has nothing to disagree with, and a spread of zero would
        imply an agreement that was never established."""
        scenario = get_scenario("healthy")
        with (
            SimulatedVehicle.from_scenario(scenario, channel="carpi-single") as _vehicle,
            CanLink.open("virtual", "carpi-single") as link,
        ):
            result = scan_vehicle(link, database, timeout=0.3, discovery_timeout=0.3)
        facts = build_facts(result)
        assert "vehicle.odometer_spread_km" not in facts
        evaluation = result.evaluate(database)
        skipped = {s.rule_id for s in evaluation.skipped if s.missing}
        assert "cross-ecu-odometer-mismatch" in skipped


class TestDidScanner:
    def test_finds_what_exists_and_distinguishes_locked(self, tampered) -> None:
        _, link = tampered
        from carpi.core.transport.base import EcuAddress

        client = UdsClient(link.channel(EcuAddress(tx_id=0x714, rx_id=0x77E)), timeout=0.2)
        client.start_session()
        report = scan_dids(client, [(0xCAFA, 0xCAFF)], delay=0.0)

        by_did = {item.did: item for item in report.observations}
        assert by_did[0xCAFE].status == STATUS_DATA
        assert by_did[0xCAFB].status == STATUS_PROTECTED
        assert by_did[0xCAFA].status == STATUS_UNSUPPORTED
        # Both count as "exists" -- a locked identifier is a discovery, not a miss.
        assert {item.did for item in report.found} >= {0xCAFB, 0xCAFE}

    def test_report_serialises(self, tampered) -> None:
        _, link = tampered
        from carpi.core.transport.base import EcuAddress

        client = UdsClient(link.channel(EcuAddress(tx_id=0x714, rx_id=0x77E)), timeout=0.2)
        client.start_session()
        report = scan_dids(client, [(0xCAFC, 0xCAFE)], delay=0.0, vin="CARPI0SIMULATED01")
        document = json.loads(json.dumps(report.as_dict()))
        assert document["schema"] == "carpi.didscan/1"
        assert document["counts"]["data"] >= 1

    def test_anonymise_redacts_the_vin(self, tampered) -> None:
        """A scan posted publicly identifies one physical car, and through it a person."""
        _, link = tampered
        from carpi.core.transport.base import EcuAddress

        client = UdsClient(link.channel(EcuAddress(tx_id=0x714, rx_id=0x77E)), timeout=0.2)
        client.start_session()
        report = scan_dids(client, [(0xF190, 0xF190)], delay=0.0, vin="CARPI0SIMULATED01")

        clear = json.dumps(report.as_dict(anonymise=False))
        assert "CARPI0SIMULATED01" in clear

        hidden = json.dumps(report.as_dict(anonymise=True))
        assert "CARPI0SIMULATED01" not in hidden
        assert report.vin is not None, "the report itself keeps the VIN for the owner"

    def test_standard_identifiers_are_named(self) -> None:
        assert DidObservation(did=0xF190, status=STATUS_DATA).standard_name == "vin"
        assert DidObservation(did=0xCAFE, status=STATUS_DATA).standard_name is None


class TestParseRanges:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("0xf190", ((0xF190, 0xF190),)),
            ("0x2200-0x22ff", ((0x2200, 0x22FF),)),
            ("0x10-0x20,0xf190", ((0x10, 0x20), (0xF190, 0xF190))),
            (" 0x10 - 0x20 ", ((0x10, 0x20),)),
        ],
    )
    def test_parses(self, text: str, expected: tuple) -> None:
        assert parse_ranges(text) == expected

    @pytest.mark.parametrize("text", ["", "0x20-0x10", "0x10000", "nonsense", ","])
    def test_rejects_bad_input(self, text: str) -> None:
        with pytest.raises(ValueError):
            parse_ranges(text)
