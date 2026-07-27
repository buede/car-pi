"""The command-line surface, driven as a real subprocess.

These run the installed entry point rather than importing it, which is what makes them
worth their runtime: they also prove the definition database is actually packaged. A
packaging mistake there passes every other test and fails on a real Raspberry Pi.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from carpi.sim import SCENARIOS

_MODULE = "carpi.cli.main"


def _run(*args: str, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, "-m", _MODULE, *args],
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if expect_success and result.returncode != 0:
        pytest.fail(
            f"carpi {' '.join(args)} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


class TestStdoutIsMachineReadable:
    """`--format json` must emit *only* JSON on stdout.

    This is pinned because it regressed once already: the simulated-scenario banner was
    printed to stdout, so `carpi demo --format json | jq` failed on the very first
    character. Anything that is not the report belongs on stderr.
    """

    def test_json_output_parses(self) -> None:
        result = _run("demo", "--scenario", "healthy", "--format", "json")
        document = json.loads(result.stdout)
        assert document["schema"] == "carpi.inspection/1"

    def test_scenario_banner_goes_to_stderr(self) -> None:
        result = _run("demo", "--scenario", "healthy", "--format", "json")
        assert "scenario: healthy" in result.stderr
        assert "scenario:" not in result.stdout

    def test_written_to_message_goes_to_stderr(self, tmp_path: Path) -> None:
        target = tmp_path / "report.json"
        result = _run("demo", "--scenario", "healthy", "--format", "json", "--out", str(target))
        assert result.stdout.strip() == ""
        assert "written to" in result.stderr
        assert json.loads(target.read_text(encoding="utf-8"))["schema"] == "carpi.inspection/1"


class TestScenariosListing:
    def test_names_are_unindented(self) -> None:
        """CI greps `^[a-z]` to enumerate scenarios, so the layout is load-bearing."""
        result = _run("scenarios")
        listed = [line for line in result.stdout.splitlines() if line and not line[0].isspace()]
        assert set(listed) == set(SCENARIOS)


class TestDefsCommands:
    def test_check_succeeds_on_the_bundled_database(self) -> None:
        result = _run("defs", "check")
        assert "OK" in result.stdout

    def test_check_reports_a_broken_database_and_exits_nonzero(self, tmp_path: Path) -> None:
        broken = tmp_path / "defs"
        (broken / "schema").mkdir(parents=True)
        result = _run("defs", "check", "--path", str(broken), expect_success=False)
        assert result.returncode != 0
        assert "error" in result.stderr.lower()

    def test_facts_lists_every_referenced_fact(self) -> None:
        result = _run("defs", "facts")
        facts = json.loads(result.stdout)
        assert "dtc.permanent_count" in facts
        assert facts["dtc.permanent_count"] == ["permanent-dtcs-present"]


class TestScanErrorHandling:
    def test_unavailable_interface_fails_with_a_useful_message(self) -> None:
        """The message has to say how to fix it -- this fires in a car park, not an IDE."""
        result = _run(
            "scan", "--transport", "socketcan", "--channel", "carpi0", expect_success=False
        )
        assert result.returncode != 0
        assert "ip link" in result.stderr

    def test_unknown_scenario_is_rejected(self) -> None:
        result = _run("demo", "--scenario", "no-such-car", expect_success=False)
        assert result.returncode != 0
