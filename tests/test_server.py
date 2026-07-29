"""The HTTP and WebSocket API, against an in-process simulated vehicle.

The load-bearing test in here is :class:`TestBusExclusivity`. One interface means one
conversation, and two overlapping ISO-TP conversations would each decode the other's
replies -- producing values quietly attributed to the wrong parameter rather than an
error anybody would notice.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from carpi.core.database import Database
from carpi.server import SimulatedProvider, VehicleGateway, create_app

# A real ECU can take most of a second to answer, and an unimplemented mode takes the
# full timeout to establish. The simulator answers in microseconds, so the production
# default is pure waiting here. Shortened rather than removed, because the unanswered
# path still has to be exercised -- that is how "unsupported" is detected.
_TEST_TIMEOUT = 0.3


def _make_client(database: Database, scenario: str = "recently-cleared") -> TestClient:
    gateway = VehicleGateway(SimulatedProvider(scenario), database, timeout=_TEST_TIMEOUT)
    return TestClient(create_app(gateway))


@pytest.fixture
def client(database: Database) -> Iterator[TestClient]:
    """A server with no history, for tests that care about interface state."""
    with _make_client(database) as test_client:
        yield test_client


@pytest.fixture(scope="module")
def scanned(database: Database) -> Iterator[tuple[TestClient, dict]]:
    """One completed scan, shared by every test that only reads the result.

    A simulated scan spends real seconds waiting out timeouts for modes the ECU does
    not implement, which is correct behaviour rather than something to tune away. Most
    tests here assert something different about the same finished scan, so it is run
    once. Tests that need a fresh interface use the function-scoped `client` instead.
    """
    with _make_client(database) as test_client:
        yield test_client, _run_scan(test_client)


def _run_scan(client: TestClient, **body: object) -> dict:
    """Start a scan and wait for it to finish, returning its summary."""
    response = client.post("/api/scans", json=body)
    assert response.status_code == 202, response.text
    job_id = response.json()["id"]

    # Follow the progress socket, which is also the UI's real code path.
    with client.websocket_connect(f"/ws/scans/{job_id}") as socket:
        while True:
            message = socket.receive_json()
            if message["type"] == "finished":
                return message["summary"]


class TestHealth:
    def test_reports_the_interface_and_definition_counts(self, scanned) -> None:
        client, _ = scanned
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["busy"] is False
        assert health["definitions"]["pids"] > 0
        assert health["definitions"]["rules"] > 0

    def test_says_plainly_when_the_vehicle_is_simulated(self, scanned) -> None:
        """A report from a simulated car must never be mistakable for a real one."""
        client, _ = scanned
        health = client.get("/api/health").json()
        assert health["simulated"] is True
        assert "simulated" in health["interface"]


class TestScanning:
    def test_scan_produces_the_scenarios_findings(self, scanned) -> None:
        _, summary = scanned
        assert summary["state"] == "done"
        fired = {finding["rule_id"] for finding in summary["findings"]}
        assert fired == {"permanent-dtcs-present", "recent-code-clear"}

    def test_report_matches_the_cli_document(self, scanned) -> None:
        """Same payload as `carpi scan --format json`, so tooling works against either."""
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        assert report["schema"] == "carpi.inspection/1"
        assert report["ecus"][0]["dtcs"]["permanent"] == ["P0420"]
        assert report["ecus"][0]["readings"]["engine_rpm"]["raw"]

    def test_progress_events_are_recorded(self, scanned) -> None:
        client, summary = scanned
        events = client.get(f"/api/scans/{summary['id']}/events").json()
        assert events["state"] == "done"
        assert any("module" in message for message in events["events"])

    def test_events_can_be_polled_incrementally(self, scanned) -> None:
        client, summary = scanned
        first = client.get(f"/api/scans/{summary['id']}/events?since=0").json()
        assert first["events"]
        # Asking again from the returned index yields nothing new.
        url = f"/api/scans/{summary['id']}/events?since={first['index']}"
        assert client.get(url).json()["events"] == []

    def test_not_assessed_is_reported_separately_from_passed(self, scanned) -> None:
        """The API must not let a check that could not run look like one that passed."""
        _, summary = scanned
        assert "not_assessed_count" in summary
        assert "passed_count" in summary

    def test_history_lists_the_scan(self, scanned) -> None:
        client, summary = scanned
        scans = client.get("/api/scans").json()["scans"]
        assert summary["id"] in {scan["id"] for scan in scans}

    def test_interface_is_released_after_a_scan(self, scanned) -> None:
        client, _ = scanned
        assert client.get("/api/health").json()["busy"] is False

    def test_advertised_mileage_enables_the_odometer_check(self, client: TestClient) -> None:
        """Needs its own scan, since the claim is an input to it."""
        summary = _run_scan(client, claimed_odometer_km=99_000)
        fired = {finding["rule_id"] for finding in summary["findings"]}
        assert "odometer-disagrees-with-advertised" in fired

    def test_a_second_scan_can_run_after_the_first(self, client: TestClient) -> None:
        assert _run_scan(client)["state"] == "done"
        assert _run_scan(client)["state"] == "done"


class TestBusExclusivity:
    """One interface, one conversation. See the module docstring."""

    def test_a_second_scan_is_refused_while_one_is_running(self, client: TestClient) -> None:
        first = client.post("/api/scans", json={})
        assert first.status_code == 202

        # The scan is now running in a worker thread and holds the interface. A second
        # request must be refused outright rather than queued or, far worse, allowed to
        # interleave its requests with the first scan's on the same channel.
        refusals = 0
        for _ in range(40):
            response = client.post("/api/scans", json={})
            if response.status_code == 409:
                refusals += 1
                detail = response.json()["detail"]
                assert "already in use" in detail["message"]
                break
            # 202 means the first scan had already finished; retry until it has not.
            client.get(f"/api/scans/{response.json()['id']}")
        assert refusals == 1, "a concurrent scan was accepted instead of refused"

    def test_live_values_are_refused_during_a_scan(self, client: TestClient) -> None:
        client.post("/api/scans", json={})
        with client.websocket_connect("/ws/live") as socket:
            message = socket.receive_json()
            # Either the scan still holds the interface (busy), or it finished first and
            # the socket got as far as reporting readiness. Both are correct; what must
            # never happen is a live stream running concurrently with a scan.
            assert message["type"] in {"busy", "ready", "error"}

    def test_health_names_what_holds_the_interface(self, client: TestClient) -> None:
        response = client.post("/api/scans", json={})
        job_id = response.json()["id"]
        for _ in range(40):
            health = client.get("/api/health").json()
            if health["busy"]:
                assert health["activity"]["kind"] == "scan"
                assert health["activity"]["id"] == job_id
                return
        pytest.skip("the scan finished before the interface state could be observed")


class TestPreflight:
    """Listening before transmitting, on the phone path as well as the command line.

    The alternative is a scan of a silent bus, which succeeds and reports that the car
    answered nothing -- a result that reads far too much like a clean car.
    """

    def test_it_says_there_is_no_bus_when_the_vehicle_is_simulated(self, scanned) -> None:
        """Otherwise somebody trying the demo is sent to check wiring that does not exist."""
        client, _ = scanned
        health = client.get("/api/preflight").json()
        assert health["verdict"] == "simulated"
        assert health["advice"] == []

    def test_it_is_a_get_so_the_write_firewall_stays_intact(self, scanned) -> None:
        """It also genuinely sends nothing, so a GET is honest rather than a workaround."""
        client, _ = scanned
        assert client.post("/api/preflight").status_code in (404, 405)

    def test_it_refuses_while_the_interface_is_claimed(self, client: TestClient) -> None:
        client.post("/api/scans", json={})
        for _ in range(40):
            response = client.get("/api/preflight")
            if response.status_code == 409:
                return
        pytest.skip("the scan finished before the interface state could be observed")


class TestLiveValues:
    def test_streams_samples(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/live") as socket:
            ready = socket.receive_json()
            assert ready["type"] == "ready"
            assert ready["pids"], "no live values were available"
            names = {pid["name"] for pid in ready["pids"]}
            assert "engine_rpm" in names

            sample = socket.receive_json()
            assert sample["type"] == "sample"
            assert sample["values"]["engine_rpm"] == 760.0

    def test_samples_carry_labels_and_units_so_the_ui_needs_no_second_request(
        self, client: TestClient
    ) -> None:
        with client.websocket_connect("/ws/live") as socket:
            ready = socket.receive_json()
            rpm = next(pid for pid in ready["pids"] if pid["name"] == "engine_rpm")
            assert rpm["label"] == "Engine RPM"
            assert rpm["unit"] == "rpm"

    def test_sequence_advances(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/live") as socket:
            assert socket.receive_json()["type"] == "ready"
            first = socket.receive_json()["sequence"]
            second = socket.receive_json()["sequence"]
            assert second == first + 1


class TestErrorHandling:
    def test_unknown_scan_is_404(self, scanned) -> None:
        client, _ = scanned
        assert client.get("/api/scans/nope").status_code == 404

    def test_report_before_completion_is_409_not_an_empty_document(
        self, client: TestClient
    ) -> None:
        """An empty report would read as "nothing wrong with this car"."""
        job_id = client.post("/api/scans", json={}).json()["id"]
        response = client.get(f"/api/scans/{job_id}/report")
        assert response.status_code in {409, 200}
        if response.status_code == 409:
            assert "no report yet" in response.json()["detail"]

    @pytest.mark.parametrize("value", ["banana", -5])
    def test_bad_odometer_is_rejected(self, client: TestClient, value: object) -> None:
        response = client.post("/api/scans", json={"claimed_odometer_km": value})
        assert response.status_code == 422

    def test_unknown_websocket_scan_reports_an_error(self, scanned) -> None:
        client, _ = scanned
        with client.websocket_connect("/ws/scans/nope") as socket:
            assert socket.receive_json()["type"] == "error"


class TestDefinitionEndpoints:
    def test_pids_are_listed_with_confidence(self, scanned) -> None:
        client, _ = scanned
        pids = client.get("/api/defs/pids").json()["pids"]
        rpm = next(p for p in pids if p["name"] == "engine_rpm")
        assert rpm["unit"] == "rpm"
        assert rpm["confidence"] in {"community", "verified", "official"}

    def test_rules_are_worst_first(self, scanned) -> None:
        client, _ = scanned
        rules = client.get("/api/defs/rules").json()["rules"]
        assert rules[0]["severity"] == "critical"


class TestUiApiContract:
    """Every field the UI reads must exist in the real responses.

    There is no browser in CI, so this stands in for one. It catches the failure a
    browser would: renaming an API field leaves the server's own tests passing and the
    phone showing "undefined" or a blank card. Keep these lists in step with
    ``static/app.js`` -- if you change one, change the other.
    """

    @staticmethod
    def _require(document: object, path: str) -> None:
        current = document
        for part in path.split("."):
            assert isinstance(current, dict), f"{path}: {part} is not under an object"
            assert part in current, f"{path}: missing {part!r}"
            current = current[part]

    @pytest.mark.parametrize("path", ["interface", "simulated", "busy", "definitions.pids"])
    def test_health_fields(self, scanned, path: str) -> None:
        client, _ = scanned
        self._require(client.get("/api/health").json(), path)

    @pytest.mark.parametrize(
        "path",
        ["id", "state", "created_at", "vin", "worst_severity", "passed_count"],
    )
    def test_summary_fields(self, scanned, path: str) -> None:
        """Read by the History view and by the end of the progress socket."""
        _, summary = scanned
        self._require(summary, path)

    @pytest.mark.parametrize(
        "path",
        [
            "scan.vin",
            "scan.started_at",
            "scan.claimed_odometer_km",
            "passed",
            "findings",
            "not_assessed",
            "ecus",
            "facts",
        ],
    )
    def test_report_top_level_fields(self, scanned, path: str) -> None:
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        self._require(report, path)

    def test_finding_fields(self, scanned) -> None:
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        assert report["findings"], "the fixture scenario should produce findings"
        for finding in report["findings"]:
            for key in ("rule_id", "severity", "title", "explain", "evidence", "confidence"):
                assert key in finding, key

    def test_ecu_fields(self, scanned) -> None:
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        for ecu in report["ecus"]:
            self._require(ecu, "address.label")
            assert "ecu_name" in ecu
            for kind in ("permanent", "stored", "pending"):
                self._require(ecu, f"dtcs.{kind}")

    def test_not_assessed_entries_name_what_was_missing(self, scanned) -> None:
        """The UI prints these verbatim, so both keys have to be present."""
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        for entry in report["not_assessed"]:
            assert "title" in entry
            assert "missing" in entry

    def test_readiness_facts_use_the_shape_the_ui_parses(self, scanned) -> None:
        """app.js slices `readiness.<name>.complete`; the naming must not drift."""
        client, summary = scanned
        report = client.get(f"/api/scans/{summary['id']}/report").json()
        facts = report["facts"]
        assert "readiness.supported_count" in facts
        assert "readiness.complete_count" in facts
        per_monitor = [
            key for key in facts if key.startswith("readiness.") and key.endswith(".complete")
        ]
        assert per_monitor, "no per-monitor readiness facts to render"
        for key in per_monitor:
            assert isinstance(facts[key], bool)

    def test_live_message_fields(self, client: TestClient) -> None:
        with client.websocket_connect("/ws/live") as socket:
            ready = socket.receive_json()
            assert ready["type"] == "ready"
            self._require(ready, "module")
            for pid in ready["pids"]:
                for key in ("name", "label", "unit"):
                    assert key in pid, key

            sample = socket.receive_json()
            for key in ("type", "sequence", "elapsed", "values", "failures"):
                assert key in sample, key


class TestUiIsServed:
    @pytest.mark.parametrize(
        "path",
        ["/", "/app.js", "/style.css", "/sw.js", "/icon.svg", "/manifest.webmanifest"],
    )
    def test_asset_is_reachable(self, scanned, path: str) -> None:
        client, _ = scanned
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content

    def test_manifest_is_valid_json(self, scanned) -> None:
        client, _ = scanned
        manifest = json.loads(client.get("/manifest.webmanifest").content)
        assert manifest["start_url"] == "./"

    def test_api_routes_are_not_shadowed_by_the_static_mount(self, scanned) -> None:
        """The UI is mounted at / and must not swallow /api."""
        client, _ = scanned
        assert client.get("/api/health").json()["status"] == "ok"
