"""Vehicle data on disk must not be readable by the rest of the machine.

A scan carries the VIN, which identifies one physical car and, through it, a person. A
coding restore point additionally carries the module's login code, which is the secret that
permits writing to that car. Written with the default umask those land as ``-rw-r--r--``.

These are cheap checks for a failure nobody notices: the file is there, the tool worked,
and the exposure is invisible until it matters.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from carpi.core.storage import PRIVATE_DIR_MODE, PRIVATE_FILE_MODE, write_private

# Permissions are a POSIX concept; the deployment target is a Raspberry Pi and development
# happens on Linux and macOS.
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class TestWritePrivate:
    def test_the_file_is_owner_only(self, tmp_path: Path) -> None:
        target = write_private(tmp_path / "scan.json", "{}")
        assert _mode(target) == PRIVATE_FILE_MODE

    def test_a_created_directory_is_owner_only(self, tmp_path: Path) -> None:
        """A world-traversable directory advertises which cars have been scanned."""
        target = write_private(tmp_path / "nested" / "deeper" / "scan.json", "{}")
        assert _mode(target.parent) == PRIVATE_DIR_MODE

    def test_an_existing_loose_file_is_tightened(self, tmp_path: Path) -> None:
        """The case that matters: a file an earlier version of car-pi already wrote."""
        target = tmp_path / "scan.json"
        target.write_text("old", encoding="utf-8")
        target.chmod(0o644)
        write_private(target, "new")
        assert _mode(target) == PRIVATE_FILE_MODE
        assert target.read_text(encoding="utf-8") == "new"

    def test_it_is_never_briefly_world_readable(self, tmp_path: Path) -> None:
        """The mode is set as the file is created, not afterwards.

        Chmod-after-write leaves a window in which the content exists and anyone can read
        it, which is exactly long enough on a busy machine.
        """
        target = tmp_path / "scan.json"
        write_private(target, "x")
        # A umask that would otherwise widen the mode must not be able to.
        previous = os.umask(0)
        try:
            target.unlink()
            write_private(target, "x")
            assert _mode(target) == PRIVATE_FILE_MODE
        finally:
            os.umask(previous)

    def test_the_content_round_trips(self, tmp_path: Path) -> None:
        payload = {"vin": "WVWZZZ1KZAW123456"}
        target = write_private(tmp_path / "scan.json", json.dumps(payload))
        assert json.loads(target.read_text(encoding="utf-8")) == payload


class TestRestorePointsAreNotReadableByOthers:
    """The most sensitive file car-pi writes: it holds a VIN and a module login code."""

    def test_the_archive_is_owner_only(self, tmp_path: Path) -> None:
        from carpi.coding.plan import RestorePoint

        point = RestorePoint(
            created_at="2026-07-29T10:00:00",
            module_address=0x46,
            module_name="Central convenience",
            identifier=0x01,
            previous="0A1B2C",
            intended="0A1B2D",
            vin="WVWZZZ1KZAW123456",
            login_code=13861,
        )
        path = point.write(tmp_path / "restore")

        assert _mode(path) == PRIVATE_FILE_MODE
        assert _mode(path.parent) == PRIVATE_DIR_MODE

    def test_the_login_code_is_still_archived(self, tmp_path: Path) -> None:
        """Restoring needs it, so it is kept -- which is why the file mode matters."""
        from carpi.coding.plan import RestorePoint

        point = RestorePoint(
            created_at="2026-07-29T10:00:00",
            module_address=0x46,
            module_name="Central convenience",
            identifier=0x01,
            previous="0A1B2C",
            intended="0A1B2D",
            login_code=13861,
        )
        archived = json.loads(point.write(tmp_path / "r").read_text(encoding="utf-8"))
        assert archived["login_code"] == 13861


class TestDefaultsDoNotLandInACheckout:
    """A bare filename lands wherever the terminal is, which for a contributor is a repo.

    A real car's VIN one `git add -A` away from being published is not a theoretical risk;
    it is the ordinary way this goes wrong.
    """

    def test_the_guide_suggests_a_path_outside_the_working_directory(self) -> None:
        from carpi.cli.guide import _default_scan_path  # noqa: PLC2701

        suggested = _default_scan_path("WVWZZZ1KZAW123456")
        assert suggested.is_absolute()
        assert Path.home() in suggested.parents

    def test_the_suggested_name_carries_no_more_than_the_platform(self) -> None:
        """A directory listing should not identify the car."""
        from carpi.cli.guide import _default_scan_path  # noqa: PLC2701

        assert "WVWZZZ1KZAW123456".lower() not in _default_scan_path("WVWZZZ1KZAW123456").name
        assert "wvwzzz1k" in _default_scan_path("WVWZZZ1KZAW123456").name

    def test_a_missing_vin_still_produces_a_usable_path(self) -> None:
        from carpi.cli.guide import _default_scan_path  # noqa: PLC2701

        assert _default_scan_path(None).name.endswith(".json")

    @pytest.mark.parametrize("pattern", ["carpi-scan-*.json", "contribution-*.json", "scans/"])
    def test_the_names_the_tool_suggests_are_gitignored(self, pattern: str) -> None:
        """A backstop for the case where somebody types a path into a checkout anyway."""
        root = Path(__file__).resolve().parent.parent
        ignored = (root / ".gitignore").read_text(encoding="utf-8")
        assert pattern in ignored


class TestSecretsStayOutOfArgv:
    def test_the_login_code_can_be_prompted_instead_of_passed(self) -> None:
        """As an option it is recorded in shell history and visible in `ps` to others."""
        from carpi.cli.vag import coding_apply

        option = next(p for p in coding_apply.params if p.name == "login")
        assert option.prompt is not None
        assert option.hide_input is True
