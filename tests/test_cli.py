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

    @pytest.mark.parametrize(
        ("args", "option"),
        [
            (
                (
                    "uds",
                    "read",
                    "--request-id",
                    "oops",
                    "--response-id",
                    "0x77E",
                    "--did",
                    "0xF190",
                ),
                "--request-id",
            ),
            (("uds", "identify", "--request-id", "0x714", "--response-id", "zzz"), "--response-id"),
            (("uds", "discover", "--low", "notahexnumber"), "--low"),
        ],
    )
    def test_mistyped_hex_names_the_option(self, args: tuple[str, ...], option: str) -> None:
        """Every arbitration ID here is copied by hand, so a typo is the expected mistake.

        Unguarded, ``int(value, 16)`` reports it as a Python traceback, which does not say
        which of the four hex options was wrong.
        """
        result = _run(*args, expect_success=False)
        assert result.returncode != 0
        assert option in result.stderr
        assert "Traceback" not in result.stderr


class TestDiscoveryFeedsTheNextCommand:
    """`uds discover` prints `714/77E`; the next command has to accept that.

    Retyping the two halves as separate hex options is where a digit gets transposed, and
    a wrong response ID does not fail cleanly -- it listens to the wrong module.
    """

    def test_the_label_discover_prints_is_accepted(self) -> None:
        result = _run("uds", "identify", "--transport", "sim", "--request-id", "714/77E")
        assert json.loads(result.stdout)["vin"]["text"] == "CARPI0SIMULATED01"

    def test_the_obd_range_infers_its_reply_address(self) -> None:
        """ISO 15765-4 fixes it at request plus eight. The only pairing any standard gives."""
        result = _run(
            "uds", "read", "--transport", "sim", "--request-id", "0x7E0", "--did", "0xF190"
        )
        assert "CARPI0SIMULATED01" in result.stdout

    def test_outside_that_range_it_refuses_rather_than_guesses(self) -> None:
        result = _run(
            "uds", "identify", "--transport", "sim", "--request-id", "0x714", expect_success=False
        )
        assert result.returncode != 0
        assert "--response-id is required" in result.stderr


@pytest.fixture(scope="module")
def contribution(tmp_path_factory) -> tuple[Path, Path, str]:
    """One report reduced to one contribution, shared by the tests that only read it.

    Each of these is a subprocess running a simulated scan, and most of that time is spent
    waiting out timeouts for modes the simulator does not implement -- which is correct
    behaviour rather than something to tune away. So it happens once.
    """
    directory = tmp_path_factory.mktemp("contribute")
    report = directory / "car.json"
    report.write_text(
        _run("demo", "--scenario", "cluster-tampered", "--format", "json").stdout,
        encoding="utf-8",
    )
    out = directory / "contribution.json"
    shown = _run("defs", "contribute", str(report), "--yes", "-o", str(out))
    return report, out, shown.stdout + shown.stderr


class TestContributing:
    """The share path. Nothing may be uploaded, and nothing identifying may be written."""

    def test_it_writes_a_contribution_and_offers_a_link(self, contribution) -> None:
        _, out, shown = contribution
        assert out.is_file()
        assert "github.com/buede/car-pi/issues/new" in shown

    def test_it_says_plainly_that_nothing_was_sent(self, contribution) -> None:
        """The device must never publish on somebody's behalf without them deciding to."""
        assert "Nothing has been sent" in contribution[2]

    def test_it_names_the_licence_and_stops_if_declined(self, contribution) -> None:
        """Sharing grants a licence that cannot be withdrawn, so it has to be a decision.

        And declining must leave nothing behind -- a file written anyway is a file that gets
        attached to an issue later by somebody who assumed it was meant to exist.
        """
        report, out, _ = contribution
        declined = out.parent / "declined.json"
        result = subprocess.run(
            [sys.executable, "-m", _MODULE, "defs", "contribute", str(report), "-o", str(declined)],
            input="n\n",
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        assert "CC-BY-SA-4.0" in result.stderr
        assert "ask the owner" in result.stderr
        assert not declined.exists()

    @pytest.mark.parametrize(
        ("secret", "what"),
        [
            ("CARPI0SIMULATED01", "the VIN"),
            ("145000", "the cluster odometer"),
            ("285400", "the engine odometer"),
            ("CARPI-CLUSTER-01", "a cluster part number"),
            ("CLU-0000001", "a module serial number"),
        ],
    )
    def test_the_contribution_carries_no_vehicle_content(
        self, contribution, secret: str, what: str
    ) -> None:
        published = contribution[1].read_text(encoding="utf-8")
        assert secret not in published, f"{what} was published"

    def test_the_contribution_keeps_the_platform_prefix_and_addresses(self, contribution) -> None:
        document = json.loads(contribution[1].read_text(encoding="utf-8"))
        assert document["vin_prefix"] == "CARPI0SI"
        assert "0x714/0x77E" in [module["address"] for module in document["modules"]]

    def test_the_link_goes_to_stdout_so_it_can_be_piped(self, contribution) -> None:
        """Everything else it says is commentary, and belongs on stderr."""
        report, out, _ = contribution
        result = _run(
            "defs", "contribute", str(report), "--yes", "-o", str(out.parent / "piped.json")
        )
        assert result.stdout.strip().startswith("https://")
        assert "\n" not in result.stdout.strip()


class TestGuide:
    """The guided menu. Driven with scripted input, so it needs no hardware.

    The menu is only worth having if it stays honest about what it runs, so the printed
    equivalent command is asserted rather than treated as decoration.
    """

    def _guide(self, answers: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", _MODULE, "guide"],
            input=answers,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )

    def test_quitting_does_nothing(self) -> None:
        result = self._guide("quit\n")
        assert result.returncode == 0

    def test_the_simulated_path_produces_a_report(self) -> None:
        result = self._guide("try it with no car\nrecently-cleared\n")
        assert result.returncode == 0
        assert "car-pi vehicle inspection" in result.stdout
        assert "Permanent fault codes are stored" in result.stdout

    def test_it_prints_the_command_it_runs(self) -> None:
        result = self._guide("try it with no car\nhealthy\n")
        assert "carpi demo --scenario healthy" in result.stderr

    def test_the_report_still_goes_to_stdout_alone(self) -> None:
        """The guide talks on stderr, so its prose cannot corrupt a piped report."""
        result = self._guide("try it with no car\nhealthy\n")
        assert "What would you like to do" not in result.stdout
        assert "Equivalent command" not in result.stdout

    def test_an_unknown_answer_is_rejected(self) -> None:
        result = self._guide("polish the car\nquit\n")
        assert "not one of" in result.stderr or "invalid choice" in result.stderr.lower()
