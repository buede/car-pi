"""Rules evaluated directly against fact dictionaries.

The end-to-end scenarios cover most rules, but some describe vehicle states that are
awkward to simulate as a whole car. Those are exercised here instead, which keeps every
rule in the database under test -- see :class:`TestEveryRuleIsExercised`, which fails
the build if a rule is added with no coverage at all. An untested rule is worse than a
missing one: it looks like protection and provides none.
"""

from __future__ import annotations

from typing import Any

import pytest

from carpi.core.database import Database
from carpi.core.rules import evaluate
from carpi.sim import SCENARIOS

# Rules whose triggering condition is asserted in this module rather than by a
# whole-vehicle scenario.
DIRECTLY_TESTED = frozenset(
    {
        "dtc-count-disagreement",
        "mil-off-but-faults-stored",
        "fuel-trim-excessive-bank2",
        "control-module-voltage-high",
        "module-vin-mismatch",
    }
)

_BASELINE: dict[str, Any] = {
    "status.mil_on": False,
    "status.dtc_count": 0,
    "status.ignition": "spark",
    "dtc.stored_count": 0,
    "dtc.pending_count": 0,
    "dtc.permanent_count": 0,
    "dtc.stored_unique_count": 0,
    "readiness.supported_count": 11,
    "readiness.complete_count": 11,
    "readiness.incomplete_count": 0,
    "pid.coolant_temp": 90.0,
    "pid.run_time_since_start": 900.0,
    "pid.control_module_voltage": 14.2,
    "pid.ltft_bank1": 1.5,
    "pid.ltft_bank2": 2.0,
    "pid.distance_since_codes_cleared": 6000.0,
    "pid.distance_with_mil_on": 0.0,
    "vehicle.ecu_count": 1,
    "mode06.result_count": 0,
    "mode06.failing_count": 0,
}


def _fired(database: Database, **overrides: Any) -> set[str]:
    facts = dict(_BASELINE) | overrides
    return {finding.rule_id for finding in evaluate(database, facts).findings}


class TestBaselineIsQuiet:
    def test_a_healthy_fact_set_produces_no_findings(self, database: Database) -> None:
        """Everything below is measured against this, so it must be silent."""
        assert _fired(database) == set()


class TestTamperSignals:
    def test_fault_counter_disagreeing_with_the_listed_codes(self, database: Database) -> None:
        """The ECU claims three faults but listed one -- a filter or a reflashed module."""
        fired = _fired(database, **{"status.dtc_count": 3, "dtc.stored_count": 1})
        assert "dtc-count-disagreement" in fired

    def test_matching_counts_do_not_fire(self, database: Database) -> None:
        fired = _fired(database, **{"status.dtc_count": 2, "dtc.stored_count": 2})
        assert "dtc-count-disagreement" not in fired

    def test_faults_stored_with_the_lamp_off(self, database: Database) -> None:
        """How a car with real problems is made to look clean to a casual buyer."""
        fired = _fired(
            database,
            **{"status.mil_on": False, "dtc.stored_count": 2, "status.dtc_count": 2},
        )
        assert "mil-off-but-faults-stored" in fired

    def test_lamp_on_with_faults_is_not_suspicious(self, database: Database) -> None:
        fired = _fired(
            database,
            **{"status.mil_on": True, "dtc.stored_count": 2, "status.dtc_count": 2},
        )
        assert "mil-off-but-faults-stored" not in fired
        assert "mil-on" in fired


class TestFuelTrim:
    def test_bank_two_lean(self, database: Database) -> None:
        fired = _fired(database, **{"pid.ltft_bank2": 15.5, "pid.ltft_bank1": 14.0})
        assert "fuel-trim-excessive-bank2" in fired

    def test_negative_trim_also_fires(self, database: Database) -> None:
        """Running rich is a fault too; the rule uses magnitude, not sign."""
        fired = _fired(database, **{"pid.ltft_bank1": -18.0, "pid.ltft_bank2": -17.0})
        assert "fuel-trim-excessive-bank1" in fired

    @pytest.mark.parametrize("trim", [9.9, -9.9, 0.0])
    def test_within_tolerance_stays_quiet(self, database: Database, trim: float) -> None:
        fired = _fired(database, **{"pid.ltft_bank1": trim, "pid.ltft_bank2": trim})
        assert "fuel-trim-excessive-bank1" not in fired
        assert "fuel-trim-banks-diverge" not in fired

    def test_divergence_fires_even_when_both_banks_are_individually_acceptable(
        self, database: Database
    ) -> None:
        fired = _fired(database, **{"pid.ltft_bank1": 8.0, "pid.ltft_bank2": -6.0})
        assert "fuel-trim-banks-diverge" in fired
        assert "fuel-trim-excessive-bank1" not in fired


class TestCrossModuleChecks:
    def test_a_module_reporting_a_different_vin(self, database: Database) -> None:
        """A module holding another car's VIN came out of another car.

        Sometimes an honest repair with a second-hand part, sometimes a cluster fitted
        specifically to show a lower mileage. Either way it is worth asking about.
        """
        fired = _fired(database, **{"uds.module_vin_count": 3, "uds.vin_mismatch_count": 1})
        assert "module-vin-mismatch" in fired

    def test_agreeing_vins_do_not_fire(self, database: Database) -> None:
        fired = _fired(database, **{"uds.module_vin_count": 3, "uds.vin_mismatch_count": 0})
        assert "module-vin-mismatch" not in fired

    def test_the_check_is_skipped_when_no_module_reports_a_vin(self, database: Database) -> None:
        """Most modules do not implement UDS. Silence is not agreement."""
        evaluation = evaluate(database, dict(_BASELINE))
        skipped = {s.rule_id for s in evaluation.skipped if s.missing}
        assert "module-vin-mismatch" in skipped


class TestChargingSystem:
    def test_overcharging(self, database: Database) -> None:
        fired = _fired(database, **{"pid.control_module_voltage": 15.8})
        assert "control-module-voltage-high" in fired

    def test_undercharging(self, database: Database) -> None:
        fired = _fired(database, **{"pid.control_module_voltage": 12.3})
        assert "control-module-voltage-low" in fired

    def test_voltage_is_not_judged_before_the_engine_has_run(self, database: Database) -> None:
        """At 5 seconds the alternator has not taken over, so the reading means nothing."""
        fired = _fired(
            database,
            **{"pid.control_module_voltage": 12.3, "pid.run_time_since_start": 5.0},
        )
        assert "control-module-voltage-low" not in fired


class TestSkippedNotPassed:
    def test_absent_fact_skips_the_rule(self, database: Database) -> None:
        facts = {key: value for key, value in _BASELINE.items() if key != "pid.ltft_bank2"}
        evaluation = evaluate(database, facts)
        skipped = {s.rule_id for s in evaluation.skipped if s.missing}
        assert "fuel-trim-excessive-bank2" in skipped
        assert "fuel-trim-excessive-bank2" not in evaluation.passed

    def test_an_empty_fact_set_passes_nothing(self, database: Database) -> None:
        """No data must mean no conclusions -- not fifteen clean bills of health."""
        evaluation = evaluate(database, {})
        assert evaluation.findings == ()
        assert evaluation.passed == ()
        assert len(evaluation.skipped) == len(database.rules)


def _bare_scan():
    """A scan that reached no module, so the rendering under test is the evaluation."""
    from carpi.core.scan import ScanResult

    return ScanResult(
        started_at="2026-07-29T10:00:00", finished_at="2026-07-29T10:00:20", transport="virtual"
    )


class TestABrokenRuleIsNotSilent:
    """A rule that raises is in none of the three counts, so it must have its own section.

    Without one it simply disappears: not failed, not passed, and not even reported as
    unassessable. That is the same failure the whole engine exists to prevent, arriving
    through a bug in a definition file rather than through a quiet vehicle.
    """

    def test_the_error_reaches_the_text_report(self) -> None:
        from carpi.core.rules import Evaluation
        from carpi.report.text import render_text

        evaluation = Evaluation(errors=(("odometer-disagrees-with-advertised", "boom"),))
        rendered = render_text(_bare_scan(), evaluation)

        assert "could not be evaluated" in rendered
        assert "odometer-disagrees-with-advertised" in rendered
        assert "boom" in rendered

    def test_the_error_reaches_the_json_report(self) -> None:
        from carpi.core.rules import Evaluation
        from carpi.report.text import to_dict

        evaluation = Evaluation(errors=(("some-rule", "boom"),))
        payload = to_dict(_bare_scan(), evaluation)

        assert payload["rule_errors"] == [{"rule_id": "some-rule", "error": "boom"}]

    def test_a_broken_rule_is_never_counted_as_passed(self) -> None:
        from carpi.core.rules import Evaluation

        evaluation = Evaluation(errors=(("some-rule", "boom"),))
        assert evaluation.passed == ()
        assert evaluation.findings == ()


class TestEveryRuleIsExercised:
    def test_no_rule_is_left_untested(self, database: Database) -> None:
        """Adding a rule without coverage fails here.

        A rule nobody tests may reference a mistyped fact, or a threshold that can
        never be met, and it will sit in the database looking like a safeguard while
        never firing on any car.
        """
        covered = DIRECTLY_TESTED.union(
            *(set(scenario.expect_findings) for scenario in SCENARIOS.values())
        )
        untested = {rule.id for rule in database.rules} - covered
        assert untested == set(), (
            f"these rules are never triggered by a scenario or a direct test: "
            f"{sorted(untested)}. Add a scenario in carpi/sim/scenarios.py, or a case "
            f"here plus its id to DIRECTLY_TESTED."
        )

    def test_directly_tested_ids_all_exist(self, database: Database) -> None:
        """Guards against a rule being renamed and the coverage claim going stale."""
        known = {rule.id for rule in database.rules}
        assert known >= DIRECTLY_TESTED, sorted(DIRECTLY_TESTED - known)
