"""The hardware bench, exercised over a virtual bus.

This can only prove the bench command's own plumbing. Its actual purpose -- catching bit
timing and TP2.0 keepalive problems -- requires two real controllers, because a virtual
interface has no timing to get wrong. That is stated in the command's help so nobody
mistakes a green run here for hardware validation.

The SocketCAN-marked tests below run the same benches over vcan in CI, which at least puts
a real kernel CAN stack underneath.
"""

from __future__ import annotations

import json
import os

import pytest
from click.testing import CliRunner

from carpi.cli.bench import bench


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _run(runner: CliRunner, *args: str):
    return runner.invoke(bench, list(args), catch_exceptions=False)


class TestOverVirtualBus:
    def test_obd_bench_passes(self, runner: CliRunner) -> None:
        result = _run(
            runner,
            "obd",
            "--kind",
            "virtual",
            "--responder",
            "bench-obd-test",
            "--tester",
            "bench-obd-test",
            "--timeout",
            "0.6",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["passed"] is True
        names = {check["name"] for check in report["checks"]}
        assert "multi-frame ISO-TP read (VIN)" in names

    def test_tp20_bench_passes(self, runner: CliRunner) -> None:
        result = _run(
            runner,
            "tp20",
            "--kind",
            "virtual",
            "--responder",
            "bench-tp20-test",
            "--tester",
            "bench-tp20-test",
            "--timeout",
            "1.0",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.output
        report = json.loads(result.output)
        assert report["passed"] is True
        assert all(check["passed"] for check in report["checks"])

    def test_tp20_bench_covers_the_checks_that_need_hardware(self, runner: CliRunner) -> None:
        """These are the reasons the bench exists; losing one would be silent."""
        result = _run(
            runner,
            "tp20",
            "--kind",
            "virtual",
            "--responder",
            "bench-tp20-cover",
            "--tester",
            "bench-tp20-cover",
            "--timeout",
            "1.0",
            "--format",
            "json",
        )
        names = {check["name"] for check in json.loads(result.output)["checks"]}
        assert "TP2.0 channel setup to Instruments (0x17)" in names
        assert "segmented reply reassembled (identification)" in names
        assert "30 sequential requests stay sequenced" in names
        assert "channel survives an idle period" in names
        assert "absent module stays silent" in names

    def test_latency_is_reported(self, runner: CliRunner) -> None:
        """Timing is the point of running on hardware, so it is reported not just used."""
        result = _run(
            runner,
            "obd",
            "--kind",
            "virtual",
            "--responder",
            "bench-latency",
            "--tester",
            "bench-latency",
            "--timeout",
            "0.6",
            "--format",
            "json",
        )
        latency = json.loads(result.output)["latency_ms"]
        assert latency["count"] > 0
        assert latency["min"] <= latency["median"] <= latency["max"]

    def test_text_output_is_readable(self, runner: CliRunner) -> None:
        result = _run(
            runner,
            "obd",
            "--kind",
            "virtual",
            "--responder",
            "bench-text",
            "--tester",
            "bench-text",
            "--timeout",
            "0.6",
        )
        assert "[ok  ]" in result.output
        assert "PASSED" in result.output


@pytest.mark.socketcan
class TestOverSocketCan:
    """The same benches with a real kernel CAN stack underneath."""

    interface = os.environ.get("CARPI_TEST_SOCKETCAN")

    @pytest.mark.skipif(
        not os.environ.get("CARPI_TEST_SOCKETCAN"),
        reason="set CARPI_TEST_SOCKETCAN to a vcan interface",
    )
    def test_obd_bench_over_vcan(self, runner: CliRunner) -> None:
        result = _run(
            runner,
            "obd",
            "--kind",
            "socketcan",
            "--responder",
            self.interface or "vcan0",
            "--tester",
            self.interface or "vcan0",
            "--timeout",
            "0.8",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["passed"] is True

    @pytest.mark.skipif(
        not os.environ.get("CARPI_TEST_SOCKETCAN"),
        reason="set CARPI_TEST_SOCKETCAN to a vcan interface",
    )
    def test_tp20_bench_over_vcan(self, runner: CliRunner) -> None:
        """The closest this project gets to hardware validation without hardware."""
        result = _run(
            runner,
            "tp20",
            "--kind",
            "socketcan",
            "--responder",
            self.interface or "vcan0",
            "--tester",
            self.interface or "vcan0",
            "--timeout",
            "1.2",
            "--format",
            "json",
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["passed"] is True
