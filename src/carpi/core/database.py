"""Loading and validating the definition database.

Everything vehicle-specific in car-pi is data. This module turns that YAML into
frozen dataclasses, validating it against the JSON Schemas in ``defs/schema/`` and
failing loudly on anything malformed. Loading is strict on purpose: a definition file
with a typo should stop the tool at startup, not produce a plausible-looking but wrong
number in a report somebody uses to buy a car.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema
import yaml

from carpi.core.expr import Expression, ExpressionError, compile_expression
from carpi.core.protocol.decoders import DECODERS
from carpi.core.vehicles import DecodeSpec, EcuProfile, VehicleProfile, VehicleRead

__all__ = [
    "SEVERITY_ORDER",
    "Database",
    "DefinitionError",
    "PidDef",
    "Rule",
    "defs_root",
]

# Ordered worst-first, which is the order findings are presented in.
SEVERITY_ORDER: dict[str, int] = {
    "critical": 0,
    "high": 1,
    "medium": 2,
    "low": 3,
    "info": 4,
}


class DefinitionError(Exception):
    """A definition file is missing, malformed, or internally inconsistent."""


def defs_root() -> Path:
    """Directory holding the definition database.

    ``CARPI_DEFS_PATH`` overrides the bundled copy, so a contributor can point the
    tool at a working checkout of the community database without reinstalling.
    """
    override = os.environ.get("CARPI_DEFS_PATH")
    if override:
        path = Path(override).expanduser()
        if not path.is_dir():
            raise DefinitionError(f"CARPI_DEFS_PATH is not a directory: {path}")
        return path.resolve()
    # car-pi is never loaded from a zip import, so treating the package as a real
    # directory is safe and keeps the rest of this module working with plain Paths.
    return Path(str(resources.files("carpi.defs")))


def _load_yaml(path: Path) -> Any:
    try:
        with path.open("rb") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError:
        raise DefinitionError(f"definition file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise DefinitionError(f"invalid YAML in {path}: {exc}") from exc


def _validate(document: Any, schema_path: Path, source: Path) -> None:
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    try:
        jsonschema.validate(document, schema)
    except jsonschema.ValidationError as exc:
        location = "/".join(str(part) for part in exc.absolute_path) or "<root>"
        raise DefinitionError(f"{source}: at {location}: {exc.message}") from exc


@dataclass(frozen=True)
class PidDef:
    """One Mode 01/02 parameter, as defined in ``generic/mode01-pids.yaml``."""

    pid: int
    name: str
    label: str
    length: int
    unit: str | None = None
    formula: Expression | None = None
    decoder: str | None = None
    value_range: tuple[float, float] | None = None
    confidence: str = "official"
    note: str | None = None

    @property
    def is_numeric(self) -> bool:
        """True when this PID decodes to a single number a rule can compare."""
        return self.formula is not None

    def in_range(self, value: float) -> bool:
        """Whether *value* is physically plausible, per the definition's range."""
        if self.value_range is None:
            return True
        low, high = self.value_range
        return low <= value <= high


@dataclass(frozen=True)
class Rule:
    """One inspection finding, as defined in ``generic/rules/*.yaml``."""

    id: str
    title: str
    severity: str
    when: Expression
    explain: str
    requires: tuple[str, ...] = ()
    confidence: str = "community"
    applies_to: dict[str, str] | None = None
    references: tuple[str, ...] = ()

    @property
    def required_facts(self) -> frozenset[str]:
        """Every fact that must exist for this rule to be applicable."""
        return self.when.names | frozenset(self.requires)


@dataclass(frozen=True)
class Database:
    """The loaded definition database."""

    root: Path
    pids_by_number: dict[int, PidDef]
    pids_by_name: dict[str, PidDef]
    rules: tuple[Rule, ...]
    vehicles: tuple[VehicleProfile, ...] = ()

    @classmethod
    def load(cls, root: Path | None = None) -> Database:
        """Load and validate the whole database."""
        base = root or defs_root()
        schema_dir = base / "schema"
        if not schema_dir.is_dir():
            raise DefinitionError(f"no schema/ directory under {base}")

        pids = _load_pids(base / "generic" / "mode01-pids.yaml", schema_dir)
        rules = _load_rules(base / "generic" / "rules", schema_dir)
        vehicles = _load_vehicles(base / "vehicles", schema_dir)
        return cls(
            root=base,
            pids_by_number={p.pid: p for p in pids},
            pids_by_name={p.name: p for p in pids},
            rules=rules,
            vehicles=vehicles,
        )

    def pid(self, key: int | str) -> PidDef:
        """Look up a PID by number or by name."""
        table = self.pids_by_name if isinstance(key, str) else self.pids_by_number
        try:
            return table[key]  # type: ignore[index]
        except KeyError:
            shown = key if isinstance(key, str) else f"0x{key:02X}"
            raise DefinitionError(f"no PID definition for {shown}") from None

    def rules_by_severity(self) -> tuple[Rule, ...]:
        """Rules ordered worst-first, then by id for a stable presentation."""
        return tuple(sorted(self.rules, key=lambda r: (SEVERITY_ORDER[r.severity], r.id)))

    def profile(self, profile_id: str) -> VehicleProfile:
        """Look up a vehicle profile by id."""
        for candidate in self.vehicles:
            if candidate.id == profile_id:
                return candidate
        available = ", ".join(sorted(v.id for v in self.vehicles)) or "none"
        raise DefinitionError(f"no vehicle profile {profile_id!r}. Available: {available}")

    def profile_for_vin(self, vin: str | None) -> VehicleProfile | None:
        """The profile matching *vin*, or ``None``.

        Prefers the longest matching VIN prefix, so a platform-specific profile wins
        over a make-wide one. Fictional profiles never match -- see
        :meth:`VehicleProfile.matches_vin`.
        """
        candidates = [profile for profile in self.vehicles if profile.matches_vin(vin)]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda profile: max(len(prefix) for prefix in profile.vin_prefixes),
        )


def _load_pids(path: Path, schema_dir: Path) -> list[PidDef]:
    document = _load_yaml(path)
    _validate(document, schema_dir / "mode01-pids.schema.json", path)

    default_confidence = document["meta"]["confidence"]
    pids: list[PidDef] = []
    seen_numbers: dict[int, str] = {}
    seen_names: dict[str, int] = {}

    for entry in document["pids"]:
        number, name = entry["pid"], entry["name"]
        # Duplicates are the failure mode a schema cannot catch, and they silently
        # shadow each other in the lookup tables, so check explicitly.
        if number in seen_numbers:
            raise DefinitionError(
                f"{path}: PID 0x{number:02X} defined twice ({seen_numbers[number]!r} and {name!r})"
            )
        if name in seen_names:
            raise DefinitionError(
                f"{path}: name {name!r} used for both PID "
                f"0x{seen_names[name]:02X} and 0x{number:02X}"
            )
        seen_numbers[number] = name
        seen_names[name] = number

        formula = None
        if "formula" in entry:
            try:
                formula = compile_expression(entry["formula"])
            except ExpressionError as exc:
                raise DefinitionError(f"{path}: PID {name}: {exc}") from exc
            unknown = formula.names - _formula_variables(entry["bytes"])
            if unknown:
                raise DefinitionError(
                    f"{path}: PID {name}: formula uses {sorted(unknown)}, which is not "
                    f"available for a {entry['bytes']}-byte payload"
                )

        decoder = entry.get("decoder")
        if decoder is not None and decoder not in DECODERS:
            raise DefinitionError(
                f"{path}: PID {name}: no builtin decoder named {decoder!r}. "
                f"Available: {', '.join(sorted(DECODERS))}"
            )

        value_range = entry.get("range")
        pids.append(
            PidDef(
                pid=number,
                name=name,
                label=entry["label"],
                length=entry["bytes"],
                unit=entry.get("unit"),
                formula=formula,
                decoder=decoder,
                value_range=(float(value_range[0]), float(value_range[1])) if value_range else None,
                confidence=entry.get("confidence", default_confidence),
                note=entry.get("note"),
            )
        )
    return pids


def _formula_variables(length: int) -> frozenset[str]:
    """Names a formula may use for a payload of *length* bytes.

    Individual bytes A, B, C... up to the payload length, plus U and S for the whole
    window read as unsigned and signed big-endian. Restricting per-length means a
    formula that reads byte D of a two-byte PID is rejected at load time rather than
    raising during a scan in somebody's driveway.
    """
    letters = {chr(ord("A") + index) for index in range(length)}
    return frozenset(letters | {"U", "S"})


def _load_rules(directory: Path, schema_dir: Path) -> tuple[Rule, ...]:
    if not directory.is_dir():
        raise DefinitionError(f"no rules directory at {directory}")

    schema_path = schema_dir / "rules.schema.json"
    rules: list[Rule] = []
    seen: dict[str, Path] = {}

    for path in _yaml_files(directory):
        document = _load_yaml(path)
        _validate(document, schema_path, path)
        default_confidence = document["meta"]["confidence"]

        for entry in document["rules"]:
            rule_id = entry["id"]
            if rule_id in seen:
                raise DefinitionError(
                    f"{path}: rule id {rule_id!r} already defined in {seen[rule_id]}"
                )
            seen[rule_id] = path
            try:
                when = compile_expression(entry["when"])
            except ExpressionError as exc:
                raise DefinitionError(f"{path}: rule {rule_id}: {exc}") from exc
            rules.append(
                Rule(
                    id=rule_id,
                    title=entry["title"],
                    severity=entry["severity"],
                    when=when,
                    explain=" ".join(entry["explain"].split()),
                    requires=tuple(entry.get("requires", ())),
                    confidence=entry.get("confidence", default_confidence),
                    applies_to=entry.get("applies_to"),
                    references=tuple(entry.get("references", ())),
                )
            )

    if not rules:
        raise DefinitionError(f"no rules found under {directory}")
    return tuple(rules)


def _yaml_files(directory: Path) -> Iterator[Path]:
    yield from sorted(directory.glob("*.yaml"))


def _load_vehicles(directory: Path, schema_dir: Path) -> tuple[VehicleProfile, ...]:
    """Load ``vehicles/<make>/<platform>.yaml``.

    An absent or empty directory is not an error. The shipped set is nearly empty on
    purpose -- manufacturer identifiers cannot be verified without the vehicle, and a
    plausible-looking wrong one is worse than nothing at all.
    """
    if not directory.is_dir():
        return ()

    schema_path = schema_dir / "vehicle.schema.json"
    profiles: list[VehicleProfile] = []
    seen: dict[str, Path] = {}

    for path in sorted(directory.glob("*/*.yaml")):
        document = _load_yaml(path)
        _validate(document, schema_path, path)
        meta = document["meta"]
        profile_id = meta["id"]
        if profile_id in seen:
            raise DefinitionError(
                f"{path}: vehicle profile id {profile_id!r} already defined in {seen[profile_id]}"
            )
        seen[profile_id] = path

        years = meta.get("years")
        profiles.append(
            VehicleProfile(
                id=profile_id,
                make=meta["make"],
                platform=meta["platform"],
                confidence=meta["confidence"],
                vin_prefixes=tuple(document.get("match", {}).get("vin_prefix", ())),
                years=(int(years[0]), int(years[1])) if years else None,
                fictional=bool(meta.get("fictional", False)),
                source=meta.get("source"),
                note=meta.get("note"),
                ecus=tuple(_load_ecu(entry, path, profile_id) for entry in document["ecus"]),
            )
        )
    return tuple(profiles)


def _load_ecu(entry: dict[str, Any], path: Path, profile_id: str) -> EcuProfile:
    seen: set[str] = set()
    reads: list[VehicleRead] = []

    for item in entry["reads"]:
        read_id = item["id"]
        if read_id in seen:
            raise DefinitionError(
                f"{path}: {profile_id}/{entry['name']}: read id {read_id!r} defined twice"
            )
        seen.add(read_id)

        spec = item["decode"]
        value_range = spec.get("range")
        reads.append(
            VehicleRead(
                id=read_id,
                did=item["did"],
                label=item.get("label"),
                unit=item.get("unit"),
                confidence=item.get("confidence", "community"),
                verified_on=tuple(item.get("verified_on", ())),
                note=item.get("note"),
                decode=DecodeSpec(
                    type=spec["type"],
                    offset=spec.get("offset", 0),
                    length=spec.get("length"),
                    scale=float(spec.get("scale", 1.0)),
                    add=float(spec.get("add", 0.0)),
                    value_range=(float(value_range[0]), float(value_range[1]))
                    if value_range
                    else None,
                ),
            )
        )

    return EcuProfile(
        name=entry["name"],
        request_id=entry["request_id"],
        response_id=entry["response_id"],
        extended=bool(entry.get("extended", False)),
        session=entry.get("session", "extended"),
        safety_critical=bool(entry.get("safety_critical", False)),
        reads=tuple(reads),
    )


@lru_cache(maxsize=1)
def _cached_database(root: str | None) -> Database:
    return Database.load(Path(root) if root else None)


def load_database(root: Path | None = None) -> Database:
    """Load the database, memoised per root path.

    Validation is cheap but not free, and a scan touches the database repeatedly.
    """
    return _cached_database(str(root) if root else None)
