"""The UI must work with no internet at all.

This is not a style preference. The unit is used in a driveway, a car park, or at a
kerb, connected to a Pi's own hotspot which has no route to anywhere. A single
stylesheet pulled from a CDN means an unstyled page; a font from Google means a
several-second stall on every load while the request times out; an analytics script
means the page may not become interactive.

None of that is visible in local development, where the laptop has working internet
and every external request quietly succeeds. So it is asserted here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from carpi.server.app import static_dir

ASSETS = sorted(static_dir().glob("*"))

# Anything that would leave the Pi. Also catches protocol-relative "//cdn.example".
_EXTERNAL = re.compile(
    r"""(?:https?:)?//(?!localhost|127\.0\.0\.1)[a-z0-9]""",
    re.IGNORECASE,
)

# Namespace and spec URLs appear inside XML and JSON-LD style attributes and are never
# fetched, so they are not network dependencies.
_ALLOWED = (
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xlink",
)


def _lines(path: Path) -> list[tuple[int, str]]:
    text = path.read_text(encoding="utf-8")
    return list(enumerate(text.splitlines(), start=1))


class TestNoExternalRequests:
    def test_assets_exist(self) -> None:
        names = {path.name for path in ASSETS}
        assert {"index.html", "app.js", "style.css", "sw.js", "icon.svg"} <= names

    @pytest.mark.parametrize("asset", ASSETS, ids=lambda p: p.name)
    def test_no_asset_references_an_external_host(self, asset: Path) -> None:
        offenders = []
        for number, line in _lines(asset):
            stripped = line
            for allowed in _ALLOWED:
                stripped = stripped.replace(allowed, "")
            if _EXTERNAL.search(stripped):
                offenders.append(f"{asset.name}:{number}: {line.strip()}")
        assert offenders == [], (
            "these lines reference a host outside the unit, which will not resolve "
            "on a hotspot with no internet:\n" + "\n".join(offenders)
        )

    @pytest.mark.parametrize(
        "forbidden",
        ["cdn.", "googleapis", "unpkg", "jsdelivr", "cdnjs", "fonts.g"],
    )
    def test_no_known_cdn_is_named(self, forbidden: str) -> None:
        for asset in ASSETS:
            text = asset.read_text(encoding="utf-8").lower()
            assert forbidden not in text, f"{asset.name} references {forbidden}"

    def test_html_loads_only_local_scripts_and_styles(self) -> None:
        html = (static_dir() / "index.html").read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)="([^"]+)"', html):
            target = match.group(1)
            assert not target.startswith(("http:", "https:", "//")), target


class TestServiceWorker:
    def test_precaches_the_whole_shell(self) -> None:
        """A missing entry means that asset is unavailable on an offline reload."""
        source = (static_dir() / "sw.js").read_text(encoding="utf-8")
        for name in ("index.html", "style.css", "app.js", "icon.svg"):
            assert f"'{name}'" in source, f"{name} is not precached"

    def test_never_caches_vehicle_data(self) -> None:
        """Serving a previous car's report from cache would be actively dangerous.

        A stale report is worse than an error: an error is obvious, whereas last week's
        fault codes shown for the car in front of you look exactly like a real result.
        """
        source = (static_dir() / "sw.js").read_text(encoding="utf-8")
        assert "/api" in source
        assert "startsWith('/api')" in source.replace('"', "'")


class TestManifest:
    def test_is_valid_and_self_contained(self) -> None:
        manifest = json.loads((static_dir() / "manifest.webmanifest").read_text("utf-8"))
        assert manifest["start_url"] == "./"
        assert manifest["display"] == "standalone"
        for icon in manifest["icons"]:
            assert not icon["src"].startswith(("http", "//"))
            assert (static_dir() / icon["src"]).is_file(), icon["src"]


class TestAccessibilityBasics:
    """Cheap checks for the things that make a page unusable outdoors on a phone."""

    def test_viewport_is_set(self) -> None:
        html = (static_dir() / "index.html").read_text(encoding="utf-8")
        assert 'name="viewport"' in html

    def test_both_colour_schemes_are_styled(self) -> None:
        """The unit gets used in daylight and after dark."""
        css = (static_dir() / "style.css").read_text(encoding="utf-8")
        assert "prefers-color-scheme: dark" in css
        assert "color-scheme: light dark" in css

    def test_severity_is_not_conveyed_by_colour_alone(self) -> None:
        """Roughly one man in twelve cannot reliably distinguish red from green."""
        app = (static_dir() / "app.js").read_text(encoding="utf-8")
        # Every finding carries its severity as text, not just a coloured border.
        assert "class: 'severity', text: finding.severity" in app

    def test_reduced_motion_is_respected(self) -> None:
        css = (static_dir() / "style.css").read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in css


def _without_comments(source: str) -> str:
    """Strip JS comments, so a check examines code rather than prose about the code."""
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"^\s*//.*$", "", source, flags=re.MULTILINE)


class TestNoUnsafeRendering:
    """Fault codes and module names come off a vehicle; they are rendered as text.

    Nothing on a CAN bus is attacker-controlled today, but an aftermarket module or a
    future manufacturer-specific string field is exactly the sort of thing that quietly
    stops being true, and ``textContent`` costs nothing.

    Patterns are matched rather than bare names, so discussing ``innerHTML`` in a
    comment does not fail the build.
    """

    @pytest.mark.parametrize(
        "pattern",
        [
            r"\.innerHTML\s*[+]?=",
            r"\.outerHTML\s*[+]?=",
            r"insertAdjacentHTML\s*\(",
            r"document\.write\s*\(",
            r"\beval\s*\(",
            r"new\s+Function\s*\(",
        ],
    )
    def test_dangerous_pattern_is_absent(self, pattern: str) -> None:
        for name in ("app.js", "sw.js"):
            code = _without_comments((static_dir() / name).read_text(encoding="utf-8"))
            match = re.search(pattern, code)
            assert match is None, f"{name} uses {match.group(0)!r}"
