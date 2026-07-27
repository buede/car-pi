"""End-to-end scans against the simulator.

These run the entire stack -- ISO-TP framing, OBD-II modes, decoding, fact building
and rule evaluation -- over a real CAN transport. Only the vehicle is simulated.
"""

from __future__ import annotations

import json

import pytest

from carpi.core.protocol.obd2 import Obd2Client
from carpi.core.scan import build_facts
from carpi.report.text import render_json, render_text
from carpi.sim import SCENARIOS, EcuSpec, Scenario
from carpi.sim import encode as enc
from carpi.sim.scenarios import SIM_VIN


@pytest.mark.parametrize("name", sorted(SCENARIOS))
class TestScenarioExpectations:
    def test_findings_match_exactly(self, name: str, run_scenario) -> None:
        """Each scenario declares what it should provoke; nothing more, nothing less.

        Asserting equality rather than containment matters: a change that starts
        reporting spurious findings is as much a regression as one that stops
        reporting real ones. A tool that cries wolf gets ignored.
        """
        run = run_scenario(name)
        assert run.fired == tuple(sorted(run.scenario.expect_findings))

    def test_the_scan_never_asks_an_ecu_to_clear_codes(self, name: str, run_scenario) -> None:
        """The strongest form of this guarantee: no Mode 04 request reaches the bus.

        Clearing codes destroys the permanent-DTC and monitor-readiness evidence the
        inspection depends on. Checking at the wire rather than at the API means no
        future refactor can reintroduce it unnoticed.
        """
        run = run_scenario(name)
        assert run.vehicle.clear_code_requests == []
        for ecu in run.vehicle.ecus:
            modes = {request[0] for request in ecu.received}
            assert 0x04 not in modes, f"{ecu.label} was asked to clear codes"

    def test_every_responding_module_was_scanned(self, name: str, run_scenario) -> None:
        run = run_scenario(name)
        assert len(run.result.ecus) == len(run.scenario.ecus)

    def test_no_request_errors(self, name: str, run_scenario) -> None:
        run = run_scenario(name)
        errors = [error for ecu in run.result.ecus for error in ecu.errors]
        assert errors == []


class TestClearCodesIsAbsentFromTheApi:
    def test_client_exposes_no_clear_method(self) -> None:
        """Belt and braces alongside the wire-level check above."""
        suspicious = [
            name
            for name in dir(Obd2Client)
            if any(word in name.lower() for word in ("clear", "erase", "reset", "write"))
        ]
        assert suspicious == []


class TestRecentlyCleared:
    """The flagship detection: a seller who wiped the codes before the viewing."""

    def test_permanent_code_survives_the_wipe(self, run_scenario) -> None:
        run = run_scenario("recently-cleared")
        assert run.result.all_permanent_dtcs == ["P0420"]
        assert run.result.all_stored_dtcs == []

    def test_both_the_permanent_code_and_the_wipe_are_reported(self, run_scenario) -> None:
        run = run_scenario("recently-cleared")
        assert "permanent-dtcs-present" in run.fired
        assert "recent-code-clear" in run.fired

    def test_the_permanent_finding_is_critical(self, run_scenario) -> None:
        run = run_scenario("recently-cleared")
        finding = next(f for f in run.evaluation.findings if f.rule_id == "permanent-dtcs-present")
        assert finding.severity == "critical"

    def test_incomplete_monitors_are_counted(self, run_scenario) -> None:
        run = run_scenario("recently-cleared")
        facts = build_facts(run.result)
        assert facts["readiness.incomplete_count"] == 4
        assert facts["readiness.complete_count"] == 7


class TestMultipleModules:
    def test_faults_are_collected_from_every_module(self, run_scenario) -> None:
        run = run_scenario("failing-catalyst")
        assert sorted(run.result.all_stored_dtcs) == ["P0420", "P0730"]

    def test_fault_counts_stay_comparable_across_modules(self, run_scenario) -> None:
        """Both sides of the disagreement check must sum the same set of modules.

        Comparing one module's counter against every module's codes would flag every
        multi-ECU car as tampered with.
        """
        run = run_scenario("failing-catalyst")
        facts = build_facts(run.result)
        assert facts["status.dtc_count"] == facts["dtc.stored_count"] == 2
        assert "dtc-count-disagreement" not in run.fired

    def test_engine_module_is_chosen_for_live_data(self, run_scenario) -> None:
        run = run_scenario("failing-catalyst")
        assert run.result.primary is not None
        assert run.result.primary.address.rx_id == 0x7E8

    def test_freeze_frame_is_captured_when_a_fault_stored_one(self, run_scenario) -> None:
        run = run_scenario("failing-catalyst")
        engine = run.result.primary
        assert engine is not None
        assert engine.freeze_frame["freeze_frame_dtc"].value["dtc"] == "P0420"
        assert engine.freeze_frame["engine_rpm"].value == 2180.0

    def test_freeze_frame_is_not_read_on_a_healthy_car(self, run_scenario) -> None:
        run = run_scenario("healthy")
        assert run.result.primary is not None
        assert run.result.primary.freeze_frame == {}


class TestMonitorTestResults:
    def test_margin_is_computed_from_raw_counts(self, run_scenario) -> None:
        """Scale-invariant: (value - min) / (max - min) needs no unit table."""
        run = run_scenario("failing-catalyst")
        engine = run.result.primary
        assert engine is not None
        catalyst = next(t for t in engine.monitor_results if t.monitor_id == 0x21)
        assert catalyst.passed
        assert catalyst.margin == pytest.approx(121 / 128)

    def test_healthy_catalyst_sits_well_inside_its_range(self, run_scenario) -> None:
        run = run_scenario("healthy")
        engine = run.result.primary
        assert engine is not None
        catalyst = next(t for t in engine.monitor_results if t.monitor_id == 0x21)
        assert catalyst.margin == pytest.approx(40 / 128)


class TestVehicleInformation:
    def test_vin_is_read(self, run_scenario) -> None:
        run = run_scenario("healthy")
        assert run.result.vin == SIM_VIN

    def test_calibration_id_and_cvn_are_recorded(self, run_scenario) -> None:
        run = run_scenario("healthy")
        engine = run.result.primary
        assert engine is not None
        assert engine.calibration_ids == ("CARPISIM-CAL-001",)
        assert engine.calibration_verification_numbers == ("A1B2C3D4",)

    def test_ecu_name_is_read(self, run_scenario) -> None:
        run = run_scenario("healthy")
        assert run.result.primary is not None
        assert run.result.primary.ecu_name == "CARPI-SIM-ECM"


class TestSilenceIsNotHealth:
    """A car that will not answer must never be reported as having passed."""

    @staticmethod
    def _single_bank_car() -> Scenario:
        """An inline four-cylinder engine, which genuinely has no second bank."""
        return Scenario(
            name="single-bank",
            summary="Four-cylinder engine with only one cylinder bank.",
            ecus=(
                EcuSpec(
                    response_id=0x7E8,
                    label="Engine control module",
                    pids={
                        0x01: enc.monitor_status(mil_on=False, dtc_count=0),
                        0x05: enc.temperature(90),
                        0x07: enc.trim_percent(2.0),
                        0x1F: enc.seconds(900),
                        0x31: enc.distance_km(6000),
                        0x42: enc.control_module_voltage(14.2),
                    },
                ),
            ),
        )

    def test_absent_bank_two_makes_the_rule_skipped_not_passed(self, run_scenario) -> None:
        run = run_scenario(self._single_bank_car())
        skipped = {s.rule_id for s in run.evaluation.skipped if s.missing}

        assert "fuel-trim-excessive-bank2" in skipped
        assert "fuel-trim-excessive-bank2" not in run.evaluation.passed
        assert "fuel-trim-banks-diverge" in skipped

    def test_bank_one_is_still_checked(self, run_scenario) -> None:
        run = run_scenario(self._single_bank_car())
        assert "fuel-trim-excessive-bank1" in run.evaluation.passed

    def test_skipped_rules_name_the_facts_they_needed(self, run_scenario) -> None:
        run = run_scenario(self._single_bank_car())
        skipped = next(
            s for s in run.evaluation.skipped if s.rule_id == "fuel-trim-excessive-bank2"
        )
        assert skipped.missing == ("pid.ltft_bank2",)

    def test_report_lists_what_could_not_be_assessed(self, run_scenario) -> None:
        run = run_scenario(self._single_bank_car())
        text = render_text(run.result, run.evaluation)
        assert "Not assessed" in text
        assert "not the same as passing" in text


class TestOdometerCrossCheck:
    def test_disagreement_with_advertised_mileage_is_critical(self, run_scenario) -> None:
        run = run_scenario("mileage-tampered")
        finding = next(
            f for f in run.evaluation.findings if f.rule_id == "odometer-disagrees-with-advertised"
        )
        assert finding.severity == "critical"
        assert finding.evidence["pid.odometer"] == 285_400.0

    def test_no_finding_without_an_advertised_figure(self, run_scenario) -> None:
        """The operator has to supply the claim; we cannot invent one."""
        run = run_scenario("mileage-tampered", claimed_odometer_km=None)
        assert "odometer-disagrees-with-advertised" not in run.fired
        skipped = {s.rule_id for s in run.evaluation.skipped if s.missing}
        assert "odometer-disagrees-with-advertised" in skipped


class TestReportRendering:
    def test_text_report_leads_with_findings(self, run_scenario) -> None:
        run = run_scenario("recently-cleared")
        text = render_text(run.result, run.evaluation)
        assert text.index("Findings") < text.index("Fault codes")
        assert "[CRITICAL]" in text

    def test_healthy_car_says_so_plainly(self, run_scenario) -> None:
        run = run_scenario("healthy")
        text = render_text(run.result, run.evaluation)
        assert "No findings" in text

    def test_json_is_valid_and_carries_raw_payloads(self, run_scenario) -> None:
        """Raw bytes are what make a scan re-analysable when a definition is corrected."""
        run = run_scenario("failing-catalyst")
        document = json.loads(render_json(run.result, run.evaluation))

        assert document["schema"] == "carpi.inspection/1"
        assert document["scan"]["vin"] == SIM_VIN
        engine = document["ecus"][0]
        assert engine["readings"]["engine_rpm"]["raw"]
        assert engine["dtcs"]["permanent"] == ["P0420"]
        assert {f["rule_id"] for f in document["findings"]} == set(run.scenario.expect_findings)

    def test_verbose_text_includes_live_data(self, run_scenario) -> None:
        run = run_scenario("healthy")
        text = render_text(run.result, run.evaluation, verbose=True)
        assert "Live data" in text
        assert "Engine RPM" in text
