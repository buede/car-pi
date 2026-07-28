"""Guard the documentation's structure.

The docs are split across a root README, CONTRIBUTING.md and a docs/ folder, cross-linked
throughout. That is navigable for a reader and fragile under renames: a moved file leaves
dead links that nobody notices, because nothing imports a Markdown file.

So the three things a restructure breaks are asserted here -- links resolve, the index is
complete, and the root README stays a router rather than growing back into a manual.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
INDEX = DOCS / "README.md"

# The root README is the front door, and it regressed into 300 lines once already.
README_MAX_LINES = 130

_SKIP_DIRS = {
    ".git",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
}

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_FENCE = re.compile(r"^```", re.MULTILINE)


def _markdown_files() -> list[Path]:
    """Every Markdown file that is part of the project, build artefacts excluded."""
    found = []
    for path in REPO.rglob("*.md"):
        parts = set(path.relative_to(REPO).parts)
        if parts & _SKIP_DIRS or any(p.endswith(".egg-info") for p in parts):
            continue
        found.append(path)
    return sorted(found)


def _without_code_blocks(text: str) -> str:
    """Drop fenced blocks, so a path inside a shell example is not read as a link."""
    return "".join(text.split("```")[::2])


def _relative_links(path: Path) -> list[str]:
    """Link targets that point at another file in the repo, anchors and URLs removed."""
    body = _without_code_blocks(path.read_text(encoding="utf-8"))
    targets = []
    for target in _LINK.findall(body):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        targets.append(target.split("#", 1)[0])
    return [t for t in targets if t]


def test_markdown_files_were_found() -> None:
    """Guard against the walk silently matching nothing and every test passing vacuously."""
    assert len(_markdown_files()) >= 15


@pytest.mark.parametrize("doc", _markdown_files(), ids=lambda p: str(p.relative_to(REPO)))
def test_every_relative_link_resolves(doc: Path) -> None:
    """A renamed doc must not leave a link pointing at nothing."""
    broken = [
        target for target in _relative_links(doc) if not (doc.parent / target).resolve().exists()
    ]
    assert broken == [], f"{doc.relative_to(REPO)} links to missing files: {broken}"


def test_every_doc_is_in_the_index() -> None:
    """docs/README.md is the one file that claims to be complete, so hold it to that."""
    linked = {(INDEX.parent / target).resolve() for target in _relative_links(INDEX)}
    missing = sorted(
        path.name for path in DOCS.glob("*.md") if path != INDEX and path.resolve() not in linked
    )
    assert missing == [], f"not listed in docs/README.md: {missing}"


def test_the_readme_stays_a_router() -> None:
    """The root README's job is to route, not to hold the manual."""
    lines = (REPO / "README.md").read_text(encoding="utf-8").splitlines()
    assert len(lines) <= README_MAX_LINES, (
        f"README.md is {len(lines)} lines, over the {README_MAX_LINES} cap. "
        "Move the detail into docs/ and link it instead."
    )
