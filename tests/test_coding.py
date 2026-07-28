"""The write path, and the guarantees that hold it in place.

This is the only part of car-pi that can change a car, so the tests here are less about
features than about refusals. Each one corresponds to a way somebody could damage a
vehicle, and asserts that the tool stops them.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
import sys
from collections.abc import Iterator
from pathlib import Path

import can
import pytest

from carpi.coding import (
    CodingRefused,
    CodingSession,
    LoginFailed,
    apply_plan,
    build_plan,
    load_restore_point,
)
from carpi.coding.plan import MAXIMUM_VOLTAGE, MINIMUM_VOLTAGE
from carpi.core.protocol.kwp2000 import KwpError
from carpi.core.transport.canbus import CanLink
from carpi.core.transport.tp20 import open_tp20_channel
from carpi.sim.tp20 import Tp20Responder
from carpi.sim.vag import kwp2000_era_modules

COMFORT = 0x46
AIRBAG = 0x15
COMFORT_LOGIN = 13861
CODING_ID = 0x00
ORIGINAL = bytes.fromhex("0A1B2C")
CHANGED = bytes.fromhex("0A1B2D")


@pytest.fixture
def car() -> Iterator[tuple[Tp20Responder, CanLink]]:
    bus = can.interface.Bus(interface="virtual", channel="carpi-coding-test")
    responder = Tp20Responder(bus, kwp2000_era_modules())
    responder.start()
    try:
        with CanLink.open("virtual", "carpi-coding-test") as link:
            yield responder, link
    finally:
        responder.stop()
        bus.shutdown()


@pytest.fixture
def session(car) -> Iterator[CodingSession]:
    _, link = car
    channel = open_tp20_channel(link, COMFORT, timeout=1.0)
    try:
        yield CodingSession(channel, timeout=1.0)
    finally:
        channel.close()


class TestIsolation:
    """The separation that makes every other guarantee credible."""

    @pytest.mark.parametrize("package", ["carpi.core", "carpi.report", "carpi.sim", "carpi.server"])
    def test_the_inspection_path_cannot_reach_the_coding_package(self, package: str) -> None:
        """If any of these could import carpi.coding, the read-only claim is hollow.

        The server in particular: it has no authentication, so a reachable write would
        mean anybody on the hotspot could reconfigure a module.
        """
        # This test module imports carpi.coding itself, so the check has to be made
        # against a module table that does not already contain it. Snapshotted and put
        # back, because leaving it removed would break every later test in the session.
        removed = {
            name: module for name, module in sys.modules.items() if name.startswith("carpi.coding")
        }
        for name in removed:
            del sys.modules[name]
        try:
            importlib.import_module(package)
            for _, name, _ in pkgutil.walk_packages(
                importlib.import_module(package).__path__, prefix=f"{package}."
            ):
                importlib.import_module(name)
            leaked = sorted(m for m in sys.modules if m.startswith("carpi.coding"))
        finally:
            sys.modules.update(removed)

        assert leaked == [], f"{package} pulled in {leaked}"

    def test_the_server_defines_no_write_route(self) -> None:
        from carpi.core.database import Database
        from carpi.server import SimulatedProvider, VehicleGateway, create_app

        app = create_app(VehicleGateway(SimulatedProvider("healthy"), Database.load()))
        methods = {
            (method, getattr(route, "path", ""))
            for route in app.routes
            for method in getattr(route, "methods", set()) or set()
        }
        # POST /api/scans is the only mutation, and it mutates the server's own job list.
        writes = {path for method, path in methods if method in {"PUT", "PATCH", "DELETE"}}
        assert writes == set()
        posts = {path for method, path in methods if method == "POST"}
        assert posts == {"/api/scans"}


class TestSafetyCriticalRefusal:
    def test_an_airbag_module_is_refused(self, car) -> None:
        responder, link = car
        channel = open_tp20_channel(link, AIRBAG, timeout=1.0)
        try:
            airbag_session = CodingSession(channel, timeout=1.0)
            # Logged in successfully, so the refusal below cannot be the module's doing.
            airbag_session.login(20103)
            assert airbag_session.logged_in

            with pytest.raises(CodingRefused, match="Airbag"):
                build_plan(
                    airbag_session,
                    module_address=AIRBAG,
                    identifier=CODING_ID,
                    intended=bytes.fromhex("000103"),
                    voltage=12.6,
                    vehicle_speed=0,
                )
        finally:
            channel.close()

        airbag = next(m for m in responder.modules if m.logical_address == AIRBAG)
        assert airbag.write_attempts == [], "a write reached a safety-critical module"

    def test_the_refusal_has_no_override(self) -> None:
        """Checked by signature: a flag is exactly what gets passed at eleven at night."""
        import inspect

        parameters = set(inspect.signature(build_plan).parameters)
        assert not any(
            word in name for name in parameters for word in ("force", "override", "unsafe")
        )

    def test_apply_refuses_again_even_if_a_plan_is_built_by_other_means(
        self, session: CodingSession
    ) -> None:
        """The last gate before the bus, in case a plan arrives from elsewhere."""
        from carpi.coding.plan import CodingPlan

        smuggled = CodingPlan(
            module_address=AIRBAG,
            module_name="Airbags",
            identifier=CODING_ID,
            previous=bytes.fromhex("000102"),
            intended=bytes.fromhex("000103"),
        )
        with pytest.raises(CodingRefused, match="safety-critical"):
            apply_plan(session, smuggled, confirmation="Airbags")


class TestPreconditions:
    def test_low_voltage_refuses(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        with pytest.raises(CodingRefused, match="below the"):
            build_plan(
                session,
                module_address=COMFORT,
                identifier=CODING_ID,
                intended=CHANGED,
                voltage=MINIMUM_VOLTAGE - 0.1,
            )

    def test_implausible_voltage_refuses(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        with pytest.raises(CodingRefused, match="implausible"):
            build_plan(
                session,
                module_address=COMFORT,
                identifier=CODING_ID,
                intended=CHANGED,
                voltage=MAXIMUM_VOLTAGE + 1,
            )

    def test_a_moving_vehicle_refuses(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        with pytest.raises(CodingRefused, match="moving"):
            build_plan(
                session,
                module_address=COMFORT,
                identifier=CODING_ID,
                intended=CHANGED,
                voltage=12.6,
                vehicle_speed=30,
            )

    def test_unchecked_conditions_are_warned_about_not_silently_skipped(
        self, session: CodingSession
    ) -> None:
        session.login(COMFORT_LOGIN)
        plan = build_plan(session, module_address=COMFORT, identifier=CODING_ID, intended=CHANGED)
        assert any("voltage" in warning for warning in plan.warnings)
        assert any("speed" in warning for warning in plan.warnings)

    def test_an_empty_value_refuses(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        with pytest.raises(CodingRefused, match="no value"):
            build_plan(session, module_address=COMFORT, identifier=CODING_ID, intended=b"")

    def test_an_absurdly_long_value_refuses(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        with pytest.raises(CodingRefused, match="longer than"):
            build_plan(session, module_address=COMFORT, identifier=CODING_ID, intended=bytes(64))


class TestPlanning:
    def test_a_plan_changes_nothing(self, car, session: CodingSession) -> None:
        responder, _ = car
        session.login(COMFORT_LOGIN)
        build_plan(
            session,
            module_address=COMFORT,
            identifier=CODING_ID,
            intended=CHANGED,
            voltage=12.6,
            vehicle_speed=0,
        )
        comfort = next(m for m in responder.modules if m.logical_address == COMFORT)
        assert comfort.write_attempts == []

    def test_the_plan_reads_the_current_value(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        plan = build_plan(
            session, module_address=COMFORT, identifier=CODING_ID, intended=CHANGED, voltage=12.6
        )
        assert plan.previous == ORIGINAL

    def test_the_diff_marks_the_changed_byte(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        plan = build_plan(
            session, module_address=COMFORT, identifier=CODING_ID, intended=CHANGED, voltage=12.6
        )
        text = "\n".join(plan.diff_lines())
        assert "0a 1b 2c" in text
        assert "0a 1b 2d" in text
        assert "byte 2: 0x2C -> 0x2D" in text

    def test_writing_the_same_value_is_a_noop(self, session: CodingSession) -> None:
        session.login(COMFORT_LOGIN)
        plan = build_plan(
            session, module_address=COMFORT, identifier=CODING_ID, intended=ORIGINAL, voltage=12.6
        )
        assert plan.is_noop
        with pytest.raises(CodingRefused, match="already holds"):
            apply_plan(session, plan, confirmation=plan.confirmation_phrase)


class TestConfirmation:
    def _plan(self, session: CodingSession):
        session.login(COMFORT_LOGIN)
        return build_plan(
            session,
            module_address=COMFORT,
            identifier=CODING_ID,
            intended=CHANGED,
            voltage=12.6,
            vehicle_speed=0,
        )

    @pytest.mark.parametrize("phrase", ["", "y", "yes", "Y", "ok", "Comfort"])
    def test_anything_but_the_module_name_is_refused(
        self, car, session: CodingSession, phrase: str
    ) -> None:
        """A y/n prompt can be answered without reading it; a name cannot."""
        responder, _ = car
        plan = self._plan(session)
        with pytest.raises(CodingRefused, match="confirmation did not match"):
            apply_plan(session, plan, confirmation=phrase)
        comfort = next(m for m in responder.modules if m.logical_address == COMFORT)
        assert comfort.write_attempts == []

    def test_the_module_name_is_accepted_case_insensitively(
        self, session: CodingSession, tmp_path: Path
    ) -> None:
        plan = self._plan(session)
        _, took = apply_plan(
            session,
            plan,
            confirmation="central CONVENIENCE",
            restore_dir=tmp_path,
        )
        assert took


class TestRestorePoints:
    def _plan(self, session: CodingSession):
        session.login(COMFORT_LOGIN)
        return build_plan(
            session,
            module_address=COMFORT,
            identifier=CODING_ID,
            intended=CHANGED,
            voltage=12.6,
            vehicle_speed=0,
            login_code=COMFORT_LOGIN,
        )

    def test_the_previous_value_is_archived_before_the_write(
        self, session: CodingSession, tmp_path: Path
    ) -> None:
        plan = self._plan(session)
        path, took = apply_plan(
            session, plan, confirmation=plan.confirmation_phrase, restore_dir=tmp_path
        )
        assert took
        document = json.loads(path.read_text(encoding="utf-8"))
        assert document["schema"] == "carpi.restore/1"
        assert document["previous"] == ORIGINAL.hex()
        assert document["intended"] == CHANGED.hex()

    def test_an_unwritable_archive_aborts_the_write(
        self, car, session: CodingSession, tmp_path: Path
    ) -> None:
        """A change you cannot undo is not permitted, so this has to stop the write."""
        responder, _ = car
        plan = self._plan(session)

        blocker = tmp_path / "blocked"
        blocker.write_text("not a directory", encoding="utf-8")

        with pytest.raises(CodingRefused, match="restore point"):
            apply_plan(session, plan, confirmation=plan.confirmation_phrase, restore_dir=blocker)
        comfort = next(m for m in responder.modules if m.logical_address == COMFORT)
        assert comfort.write_attempts == [], "wrote without an archive"

    def test_a_restore_point_round_trips(self, session: CodingSession, tmp_path: Path) -> None:
        plan = self._plan(session)
        path, _ = apply_plan(
            session, plan, confirmation=plan.confirmation_phrase, restore_dir=tmp_path
        )
        point = load_restore_point(path)
        assert point.previous_bytes == ORIGINAL
        assert point.module_address == COMFORT
        assert point.login_code == COMFORT_LOGIN

    def test_restoring_puts_the_value_back(self, session: CodingSession, tmp_path: Path) -> None:
        """The whole point of the archive, exercised in one process."""
        plan = self._plan(session)
        path, took = apply_plan(
            session, plan, confirmation=plan.confirmation_phrase, restore_dir=tmp_path
        )
        assert took
        assert session.read_raw(CODING_ID) == CHANGED

        point = load_restore_point(path)
        undo = build_plan(
            session,
            module_address=COMFORT,
            identifier=CODING_ID,
            intended=point.previous_bytes,
            voltage=12.6,
            vehicle_speed=0,
        )
        _, restored = apply_plan(
            session, undo, confirmation=undo.confirmation_phrase, restore_dir=tmp_path
        )
        assert restored
        assert session.read_raw(CODING_ID) == ORIGINAL

    def test_a_foreign_file_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "not-ours.json"
        path.write_text(json.dumps({"schema": "something.else/1"}), encoding="utf-8")
        with pytest.raises(CodingRefused, match="not a car-pi restore point"):
            load_restore_point(path)


class TestLogin:
    def test_a_wrong_code_is_refused_and_does_not_retry(self, session: CodingSession) -> None:
        """Modules limit attempts, so guessing would lock the operator out."""
        with pytest.raises(LoginFailed, match="rejected login"):
            session.login(11111)
        assert session.logged_in is False

    def test_an_out_of_range_code_is_rejected_locally(self, session: CodingSession) -> None:
        with pytest.raises(LoginFailed, match="out of range"):
            session.login(999_999)

    def test_writing_without_a_login_is_refused(self, car, session: CodingSession) -> None:
        responder, _ = car
        with pytest.raises(KwpError, match="without a login"):
            session.write_raw(CODING_ID, CHANGED)
        comfort = next(m for m in responder.modules if m.logical_address == COMFORT)
        assert comfort.write_attempts == []

    def test_apply_refuses_without_a_login(self, car, session: CodingSession) -> None:
        from carpi.coding.plan import CodingPlan

        plan = CodingPlan(
            module_address=COMFORT,
            module_name="Central convenience",
            identifier=CODING_ID,
            previous=ORIGINAL,
            intended=CHANGED,
        )
        with pytest.raises(CodingRefused, match="not logged in"):
            apply_plan(session, plan, confirmation="Central convenience")
