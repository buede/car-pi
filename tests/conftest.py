"""Shared fixtures.

Each simulated scan gets its own virtual bus channel. python-can's virtual bus is
shared by name within a process, so reusing one channel would let a leftover ECU from
a finished test answer the next one -- and the resulting failure would appear in a
test that is entirely innocent.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from carpi.core.database import Database
from carpi.core.rules import Evaluation
from carpi.core.scan import ScanResult, scan_vehicle
from carpi.core.transport.canbus import CanLink
from carpi.sim import Scenario, SimulatedVehicle, get_scenario

_channels = itertools.count()


@dataclass
class SimRun:
    """Everything a test needs from one simulated scan."""

    scenario: Scenario
    result: ScanResult
    evaluation: Evaluation
    vehicle: SimulatedVehicle

    @property
    def fired(self) -> tuple[str, ...]:
        return tuple(sorted(f.rule_id for f in self.evaluation.findings))


@pytest.fixture(scope="session")
def database() -> Database:
    return Database.load()


@pytest.fixture(scope="session")
def _scan_cache() -> dict[tuple[str, float | None], SimRun]:
    """Memoises scans across the session.

    A scan is deterministic, but it spends real time waiting out timeouts for modes a
    module does not implement -- which is correct behaviour, not something to tune away.
    Several tests assert different things about the same scenario, so scanning once and
    sharing the result keeps the suite quick without weakening any assertion.
    """
    return {}


@pytest.fixture
def run_scenario(
    database: Database, _scan_cache: dict[tuple[str, float | None], SimRun]
) -> Callable[..., SimRun]:
    """Run a scenario end to end over a virtual bus, by name or as an object."""

    def run(
        scenario: Scenario | str,
        *,
        claimed_odometer_km: float | None | object = ...,
    ) -> SimRun:
        if isinstance(scenario, str):
            scenario = get_scenario(scenario)
        claimed = (
            scenario.claimed_odometer_km if claimed_odometer_km is ... else claimed_odometer_km
        )
        key = (scenario.name, claimed)  # type: ignore[assignment]
        cached = _scan_cache.get(key)
        if cached is not None:
            return cached

        channel = f"carpi-test-{next(_channels)}"
        vehicle = SimulatedVehicle.from_scenario(scenario, channel=channel)
        with vehicle, CanLink.open("virtual", channel) as link:
            result = scan_vehicle(
                link,
                database,
                claimed_odometer_km=claimed,  # type: ignore[arg-type]
                # The simulator answers instantly, so the production one-second timeout
                # would just be idle waiting. Kept non-zero so the no-answer path -- how
                # an unsupported mode is detected -- is still exercised.
                timeout=0.3,
                discovery_timeout=0.3,
            )
        run_result = SimRun(
            scenario=scenario,
            result=result,
            evaluation=result.evaluate(database),
            vehicle=vehicle,
        )
        _scan_cache[key] = run_result
        return run_result

    return run
