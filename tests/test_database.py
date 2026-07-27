"""Definition loading.

Loading is strict on purpose. A typo in a definition file should stop the tool at
startup rather than produce a plausible-looking wrong number in a report that somebody
uses to decide whether to buy a car.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest

from carpi.core.database import Database, DefinitionError, defs_root

_MINIMAL_RULES = """\
meta:
  id: test-rules
  title: Test rules
  confidence: community
rules:
  - id: always-fine
    title: A rule
    severity: info
    when: "pid.coolant_temp > 500"
    explain: Never mind.
"""


def _write_defs(tmp_path: Path, pids: str, rules: str = _MINIMAL_RULES) -> Path:
    root = tmp_path / "defs"
    (root / "generic" / "rules").mkdir(parents=True)
    shutil.copytree(defs_root() / "schema", root / "schema")
    (root / "generic" / "mode01-pids.yaml").write_text(textwrap.dedent(pids), encoding="utf-8")
    (root / "generic" / "rules" / "test.yaml").write_text(textwrap.dedent(rules), encoding="utf-8")
    return root


_ONE_PID = """\
meta:
  id: test-pids
  title: Test PIDs
  confidence: official
pids:
  - pid: 0x05
    name: coolant_temp
    label: Coolant temperature
    bytes: 1
    unit: degC
    formula: "A - 40"
"""


class TestBundledDatabase:
    def test_loads(self, database: Database) -> None:
        assert database.pids_by_number
        assert database.rules

    def test_pid_lookup_by_number_and_name_agree(self, database: Database) -> None:
        assert database.pid(0x0C) is database.pid("engine_rpm")

    def test_unknown_pid_raises(self, database: Database) -> None:
        with pytest.raises(DefinitionError, match="no PID definition"):
            database.pid("no_such_pid")

    def test_rules_are_ordered_worst_first(self, database: Database) -> None:
        severities = [rule.severity for rule in database.rules_by_severity()]
        order = ["critical", "high", "medium", "low", "info"]
        assert severities == sorted(severities, key=order.index)

    def test_every_pid_fact_a_rule_references_actually_exists(self, database: Database) -> None:
        """Catches a typo in a rule, which would otherwise just make it never fire.

        A rule referencing `pid.ltft_bank_1` instead of `pid.ltft_bank1` is skipped as
        inapplicable on every vehicle forever, and nothing else would notice.
        """
        known_namespaces = (
            "status.",
            "readiness.",
            "dtc.",
            "vehicle.",
            "mode06.",
            "uds.",
            "profile.",
        )
        for rule in database.rules:
            for fact in rule.required_facts:
                if fact.startswith("pid."):
                    pid_name = fact.split(".")[1]
                    assert pid_name in database.pids_by_name, (
                        f"rule {rule.id!r} references {fact!r}, but no PID is named {pid_name!r}"
                    )
                else:
                    assert fact.startswith(known_namespaces), (
                        f"rule {rule.id!r} references {fact!r}, which is in no known fact namespace"
                    )


class TestPidValidation:
    def test_minimal_file_loads(self, tmp_path: Path) -> None:
        database = Database.load(_write_defs(tmp_path, _ONE_PID))
        assert database.pid("coolant_temp").length == 1

    def test_duplicate_pid_number_is_rejected(self, tmp_path: Path) -> None:
        pids = (
            _ONE_PID
            + """\
  - pid: 0x05
    name: something_else
    label: Duplicate number
    bytes: 1
    formula: "A"
"""
        )
        with pytest.raises(DefinitionError, match="defined twice"):
            Database.load(_write_defs(tmp_path, pids))

    def test_duplicate_pid_name_is_rejected(self, tmp_path: Path) -> None:
        pids = (
            _ONE_PID
            + """\
  - pid: 0x06
    name: coolant_temp
    label: Duplicate name
    bytes: 1
    formula: "A"
"""
        )
        with pytest.raises(DefinitionError, match="used for both"):
            Database.load(_write_defs(tmp_path, pids))

    def test_unknown_decoder_is_rejected(self, tmp_path: Path) -> None:
        pids = _ONE_PID.replace('formula: "A - 40"', "decoder: no_such_decoder")
        with pytest.raises(DefinitionError, match="no builtin decoder"):
            Database.load(_write_defs(tmp_path, pids))

    def test_formula_referencing_a_byte_beyond_the_payload_is_rejected(
        self, tmp_path: Path
    ) -> None:
        """Caught at load time, not during a scan in somebody's driveway."""
        pids = _ONE_PID.replace('formula: "A - 40"', 'formula: "D - 40"')
        with pytest.raises(DefinitionError, match="not available for a 1-byte payload"):
            Database.load(_write_defs(tmp_path, pids))

    def test_both_formula_and_decoder_is_rejected(self, tmp_path: Path) -> None:
        pids = _ONE_PID + "    decoder: monitor_status\n"
        with pytest.raises(DefinitionError):
            Database.load(_write_defs(tmp_path, pids))

    def test_neither_formula_nor_decoder_is_rejected(self, tmp_path: Path) -> None:
        pids = _ONE_PID.replace('    formula: "A - 40"\n', "")
        with pytest.raises(DefinitionError):
            Database.load(_write_defs(tmp_path, pids))

    def test_malformed_yaml_is_reported_with_its_path(self, tmp_path: Path) -> None:
        root = _write_defs(tmp_path, _ONE_PID)
        (root / "generic" / "mode01-pids.yaml").write_text("pids: [oh: no: bad", encoding="utf-8")
        with pytest.raises(DefinitionError, match="invalid YAML"):
            Database.load(root)


class TestRuleValidation:
    def test_bad_expression_is_rejected_at_load_time(self, tmp_path: Path) -> None:
        rules = _MINIMAL_RULES.replace(
            'when: "pid.coolant_temp > 500"', "when: \"__import__('os')\""
        )
        with pytest.raises(DefinitionError):
            Database.load(_write_defs(tmp_path, _ONE_PID, rules))

    def test_duplicate_rule_id_is_rejected(self, tmp_path: Path) -> None:
        root = _write_defs(tmp_path, _ONE_PID)
        (root / "generic" / "rules" / "other.yaml").write_text(_MINIMAL_RULES, encoding="utf-8")
        with pytest.raises(DefinitionError, match="already defined"):
            Database.load(root)

    def test_unknown_severity_is_rejected(self, tmp_path: Path) -> None:
        rules = _MINIMAL_RULES.replace("severity: info", "severity: catastrophic")
        with pytest.raises(DefinitionError):
            Database.load(_write_defs(tmp_path, _ONE_PID, rules))

    def test_explain_whitespace_is_normalised(self, tmp_path: Path) -> None:
        database = Database.load(_write_defs(tmp_path, _ONE_PID))
        assert database.rules[0].explain == "Never mind."


class TestDefsRootOverride:
    def test_env_var_selects_an_external_database(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _write_defs(tmp_path, _ONE_PID)
        monkeypatch.setenv("CARPI_DEFS_PATH", str(root))
        assert defs_root() == root.resolve()
        assert len(Database.load().pids_by_number) == 1

    def test_missing_directory_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CARPI_DEFS_PATH", "/nonexistent/carpi-defs")
        with pytest.raises(DefinitionError, match="not a directory"):
            defs_root()
