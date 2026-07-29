"""Turning sweeps of a real car into a definition somebody could trust.

The definition database is the product. Everything else in car-pi is a few thousand lines
of published standards work, and this module is about the step where a contribution is
actually made -- and where, until now, it was lost.

``carpi uds scan-dids`` produces a list of identifiers a module holds. Nothing said what
any of them *mean*. The documented method for finding out is:

    Record a sweep. Change one thing about the car. Sweep again and compare. The
    identifier that moved by the right amount is your candidate.

Steps one to three are already commands. Step four -- comparing several hundred
identifiers across two files and noticing which moved by roughly the right amount -- is
arithmetic, and it is the part a person does badly. So it is done here.

What this deliberately does **not** do
--------------------------------------
It does not decide anything. It produces *ranked candidates*, and the ranking is a
statement about arithmetic, not about the car. Confirmation still needs what the
documentation has always said it needs: a second car of the same platform whose true
state is independently known. Nothing here can produce ``confidence: verified``.

That distinction is the whole reason the shipped database is nearly empty. A wrong
odometer identifier does not fail loudly -- it returns plausible bytes that decode to a
plausible mileage, and somebody buys a car on the strength of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Candidate",
    "DraftError",
    "HexInt",
    "LeakedValue",
    "compare_sweeps",
    "draft_profile",
    "dump_yaml",
    "issue_url",
    "observe",
]

# Where a contribution is offered. Issues rather than an ingest service: the database's
# whole value is that a person looked at every entry before it was trusted, so the
# transport should make review the default rather than something bolted on afterwards.
PROJECT_ISSUES = "https://github.com/buede/car-pi/issues/new"

# How much of a VIN identifies a platform without identifying a car. Characters 1-3 are
# the manufacturer and 4-8 describe the model, body and engine. Character 9 is a check
# digit, 10 the model year, 11 the assembly plant, and 12-17 the serial -- so everything
# from 9 onward narrows towards one physical vehicle and none of it is kept.
_VIN_PLATFORM_CHARS = 8

# Below these lengths a value from a source could coincide with structural text in the
# observation, so the leak check would cry wolf. Above them a match means a real value
# survived the reduction.
_MIN_TEXT_NEEDLE = 4
_MIN_HEX_NEEDLE = 6

# Fields carrying what a module actually said, as opposed to the structure of the exchange.
# Everything beneath one of these is vehicle content and must not survive into a
# contribution. Naming them explicitly is the point: a new value-bearing field added to a
# report is a deliberate decision to add it here too.
_VALUE_BEARING = frozenset(
    {
        "raw",
        "text",
        "value",
        "values",
        "vin",
        "uds_vin",
        "ecu_name",
        "calibration_ids",
        "calibration_verification_numbers",
        "facts",
        "evidence",
        "odometer_by_module",
        "claimed_odometer_km",
    }
)


class HexInt(int):
    """An integer that serialises as ``0x714`` rather than ``1812``.

    The schema wants integers for addresses and identifiers, and YAML parses ``0x714`` as
    one. Decimal would validate identically and be unreadable: every other source -- the
    shipped example, the command line, a manufacturer's own documentation -- writes these
    in hex, and a draft exists to be read and corrected by a person.
    """


def dump_yaml(document: Any) -> str:
    """Serialise a drafted document, keeping addresses and identifiers in hex."""
    import yaml

    class _Dumper(yaml.SafeDumper):
        pass

    _Dumper.add_representer(
        HexInt,
        lambda dumper, value: dumper.represent_scalar("tag:yaml.org,2002:int", f"0x{value:02X}"),
    )
    return yaml.dump(document, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=88)


# Scales worth naming when an implied one lands close to them. A module counting whole
# kilometres and one counting tenths are both common, and telling them apart is exactly
# what a second sweep is for.
_FAMILIAR_SCALES = (1.0, 0.1, 10.0, 0.5, 0.01, 100.0)

_SCALE_TOLERANCE = 0.02


def _iso_names() -> frozenset[str]:
    """The ISO 14229 Annex C names, so a definition's own names are not mistaken for them."""
    from carpi.core.protocol.uds import STANDARD_DIDS

    return frozenset(STANDARD_DIDS.values())


_ISO_NAMES = _iso_names()


class DraftError(ValueError):
    """The input files were not what this expects."""


class LeakedValue(AssertionError):
    """A value from the source survived into something about to be published.

    Raised rather than warned about, and it aborts the contribution. This is the last check
    before a file is offered for upload under an irrevocable licence, and the failure it
    guards against -- publishing a serial number, or a VIN, or a stranger's odometer
    reading -- is not one an apology afterwards can undo.
    """


@dataclass(frozen=True)
class Candidate:
    """One identifier that changed between two sweeps."""

    did: int
    before: str
    after: str
    length: int
    delta: int
    expected: float | None = None

    @property
    def label(self) -> str:
        return f"0x{self.did:04X}"

    @property
    def implied_scale(self) -> float | None:
        """Units of the expected change per count, if an expected change was given.

        Reported rather than guessed at. If the car moved 1.2 km and an identifier went up
        by 12, then either it counts tenths of a kilometre or the match is a coincidence --
        and saying "0.1 per count" lets the reader make that judgement instead of hiding it
        behind a decision this module is not entitled to make.
        """
        if self.expected is None or self.delta == 0:
            return None
        return self.expected / self.delta

    @property
    def familiar_scale(self) -> float | None:
        """The implied scale, when it is close to one modules actually use."""
        scale = self.implied_scale
        if scale is None:
            return None
        for familiar in _FAMILIAR_SCALES:
            if abs(scale - familiar) <= _SCALE_TOLERANCE * familiar:
                return familiar
        return None

    @property
    def rank(self) -> tuple[int, float]:
        """Sort key. Lower is a better candidate.

        A recognisable scale comes first, then anything that moved in the same direction as
        the change, then everything else. Within a group, smaller changes first -- an
        identifier that moved by a plausible amount is more interesting than one that
        jumped by thousands.
        """
        if self.expected is None:
            return (0, abs(self.delta))
        if self.familiar_scale is not None:
            return (0, abs(self.implied_scale or 0.0))
        same_direction = (self.delta > 0) == (self.expected > 0)
        return (1 if same_direction else 2, abs(self.delta))

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "did": self.label,
            "length": self.length,
            "before": self.before,
            "after": self.after,
            "delta": self.delta,
        }
        if self.implied_scale is not None:
            payload["implied_scale"] = round(self.implied_scale, 6)
        if self.familiar_scale is not None:
            payload["familiar_scale"] = self.familiar_scale
        return payload


def _observations(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    if report.get("schema") != "carpi.didscan/1":
        raise DraftError(
            f"expected a carpi.didscan/1 document, got {report.get('schema')!r}. "
            f"These files come from 'carpi uds scan-dids --out'."
        )
    found: dict[int, dict[str, Any]] = {}
    for entry in report.get("observations", []):
        raw = entry.get("raw")
        # A redacted payload carries no number to compare. Anonymising is the right thing
        # to do before sharing a sweep, and it does cost this comparison.
        if not raw or not _is_hex(raw):
            continue
        try:
            did = int(str(entry["did"]), 16)
        except (KeyError, ValueError):
            continue
        found[did] = entry
    return found


def _is_hex(text: str) -> bool:
    return bool(text) and all(character in "0123456789abcdefABCDEF" for character in text)


def compare_sweeps(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    expected: float | None = None,
) -> list[Candidate]:
    """Identifiers whose value changed between two sweeps of the same module.

    *expected* is how much the thing you changed actually changed by -- 1.2 for a car
    driven 1.2 km. Given it, candidates are ranked by whether the change they show is
    consistent with it at a scale modules actually use.

    Identifiers of differing length between the two sweeps are skipped: that is a module
    answering differently rather than a value moving, and treating it as a delta would
    manufacture a candidate out of a protocol quirk.
    """
    old = _observations(before)
    new = _observations(after)

    candidates: list[Candidate] = []
    for did, entry in new.items():
        previous = old.get(did)
        if previous is None:
            continue
        before_hex, after_hex = previous["raw"], entry["raw"]
        if before_hex == after_hex or len(before_hex) != len(after_hex):
            continue
        try:
            before_value = int(before_hex, 16)
            after_value = int(after_hex, 16)
        except ValueError:
            continue
        candidates.append(
            Candidate(
                did=did,
                before=before_hex,
                after=after_hex,
                length=len(bytes.fromhex(before_hex)),
                delta=after_value - before_value,
                expected=expected,
            )
        )

    return sorted(candidates, key=lambda candidate: candidate.rank)


def _shape_of(entry: dict[str, Any]) -> dict[str, Any]:
    """The structure of one observed identifier, and none of its contents.

    Length and type are what a definition file needs and what the next person cannot
    discover without a car in front of them. The bytes themselves are what identifies a
    vehicle, so they stop here.
    """
    raw = str(entry.get("raw", ""))
    shape: dict[str, Any] = {"status": entry.get("status", "data")}
    if _is_hex(raw):
        shape["length"] = len(bytes.fromhex(raw))
        shape["type"] = "ascii" if entry.get("text") else "uint"
    if entry.get("standard_name"):
        # Naming this is a citation of ISO 14229, not an observation about the car.
        shape["standard_name"] = entry["standard_name"]
    return shape


def _canonical_address(text: str) -> str:
    """One spelling for an address pair, whichever form it arrived in.

    A sweep records its module as ``714/77E`` and a report records ``0x714/0x77E``. Left
    alone, the same physical module appears twice in a contribution -- inflating the module
    count and splitting identifiers that belong together, which is worse than useless to
    somebody reading the result.

    Anything that is not an address pair is returned unchanged, because a module named by a
    profile has no pair to canonicalise.
    """
    left, separator, right = str(text).strip().partition("/")
    if not separator:
        return str(text).strip()
    try:
        return f"0x{int(left, 16):03X}/0x{int(right, 16):03X}"
    except ValueError:
        return str(text).strip()


def _vin_platform_prefix(vin: str | None) -> str | None:
    if not vin:
        return None
    prefix = str(vin).strip().upper()[:_VIN_PLATFORM_CHARS]
    return prefix or None


def _needles(documents: list[dict[str, Any]], allow: set[str]) -> set[str]:
    """Every piece of vehicle content in the sources, which must not be published.

    Targeted at the fields that carry what a module actually said, rather than at every
    string in the document. Walking everything would flag the word "data" -- a status in
    the source and a status in the observation -- and a check that cries wolf gets an
    exception added to it, which is how it stops working.
    """
    found: set[str] = set()

    def collect(node: Any) -> None:
        """Everything beneath a value-bearing key, at any depth."""
        if isinstance(node, dict):
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
        elif isinstance(node, str):
            text = node.strip()
            threshold = _MIN_HEX_NEEDLE if _is_hex(text) else _MIN_TEXT_NEEDLE
            if text not in allow and len(text) >= threshold:
                found.add(text)
        elif isinstance(node, int | float) and not isinstance(node, bool):
            # A number can identify a car too -- an odometer reading is the obvious one.
            # Short ones are skipped for the same reason as short strings: the observation
            # legitimately contains payload lengths, and "2" would collide with every one
            # of them. Anything long enough to identify a vehicle is long enough to check.
            text = str(node)
            if len(text) >= _MIN_TEXT_NEEDLE:
                found.add(text)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in _VALUE_BEARING:
                    collect(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    for document in documents:
        walk(document)
    return found


def _reject_leaks(observation: dict[str, Any], documents: list[dict[str, Any]]) -> None:
    """Refuse to hand back an observation that still carries a value from the source.

    The reduction below only ever copies structural fields, so this should never fire. It
    exists because "should never" is not a property anyone can check by reading, and the
    cost of being wrong once is a stranger's car published under CC-BY-SA.
    """
    import json

    allow = {str(observation.get("vin_prefix") or "")}
    serialised = json.dumps(observation)
    for needle in _needles(documents, allow):
        if needle in serialised:
            raise LeakedValue(
                f"a value from the source survived into the contribution: {needle!r}. "
                f"Nothing has been written. This is a bug in car-pi -- please report it."
            )


def observe(documents: list[dict[str, Any]]) -> dict[str, Any]:
    """Reduce scans and sweeps to what is worth sharing, and nothing more.

    A contribution answers one question: on this platform, which modules exist and which
    identifiers do they hold, of what length and type. That is the part nobody can work out
    without a car, it is what makes the database grow, and it describes a platform rather
    than a vehicle.

    Values are dropped entirely -- not scrubbed, dropped. Scrubbing a VIN out of a sweep
    still leaves the module serial numbers, the part numbers and the programming dates, and
    those together identify one physical car and, through it, a person. So the reduction
    keeps shapes and discards contents, which needs no judgement about which values happen
    to be sensitive.
    """
    if not documents:
        raise DraftError("no files given")

    vin_prefix: str | None = None
    obd_modules: list[dict[str, Any]] = []
    uds_modules: dict[str, dict[str, Any]] = {}
    sources: list[str] = []

    for document in documents:
        schema = document.get("schema")
        if schema == "carpi.didscan/1":
            sources.append("sweep")
            address = _canonical_address(document.get("module", ""))
            identifiers = uds_modules.setdefault(address, {})
            for entry in document.get("observations", []):
                if entry.get("status") not in ("data", "protected"):
                    continue
                identifiers[str(entry.get("did"))] = _shape_of(entry)
            vin_prefix = vin_prefix or _vin_platform_prefix(document.get("vin"))

        elif schema == "carpi.inspection/1":
            sources.append("inspection")
            vin_prefix = vin_prefix or _vin_platform_prefix(document.get("scan", {}).get("vin"))
            for ecu in document.get("ecus", []):
                obd_modules.append(
                    {
                        "address": ecu.get("address", {}).get("label"),
                        # Which parameters a module supports is platform data and carries
                        # no identity, unlike the readings themselves.
                        "supported_pids": list(ecu.get("supported_pids", [])),
                    }
                )
            for reading in document.get("module_readings", []):
                # The pair, not the label: a label may be the module's name, and a name is
                # meaningless to somebody trying to reach the same module on their own car.
                request = reading.get("request_id")
                response = reading.get("response_id")
                address = _canonical_address(
                    f"{request}/{response}" if request and response else reading.get("address", "")
                )
                identifiers = uds_modules.setdefault(address, {})
                for name in reading.get("values", {}):
                    # A scan reports these by name rather than by number, and the name has
                    # one of two origins. `--discover` uses ISO 14229's own names; a vehicle
                    # profile uses whatever the definition file called the read. Only the
                    # first is a standard name, and labelling the second as one would claim
                    # ISO status for something a contributor invented.
                    key = "standard_name" if name in _ISO_NAMES else "read_id"
                    identifiers.setdefault(name, {"status": "data", key: name})

        else:
            raise DraftError(
                f"expected a carpi.didscan/1 or carpi.inspection/1 document, got "
                f"{schema!r}. These come from 'carpi uds scan-dids --out' and "
                f"'carpi scan --format json -o'."
            )

    observation = {
        "schema": "carpi.observation/1",
        "vin_prefix": vin_prefix,
        "sources": sorted(set(sources)),
        "obd_modules": obd_modules,
        "modules": [
            {"address": address, "identifiers": dict(sorted(identifiers.items()))}
            for address, identifiers in sorted(uds_modules.items())
        ],
    }
    _reject_leaks(observation, documents)
    return observation


def issue_url(observation: dict[str, Any], *, base: str = PROJECT_ISSUES) -> str:
    """A prefilled issue for offering an observation, with nothing sent yet.

    A summary goes in the body rather than the whole document, because a full sweep would
    overflow a URL. The file itself is attached by the person submitting it, which is also
    the moment they get to look at what they are publishing.
    """
    from urllib.parse import urlencode

    prefix = observation.get("vin_prefix") or "unknown platform"
    modules = observation.get("modules", [])
    counted = sum(len(module.get("identifiers", {})) for module in modules)

    lines = [
        f"Platform (VIN prefix): `{prefix}`",
        "",
        "**Please fill in:** make, model, year, engine, and how you confirmed the car is "
        "what you say it is.",
        "",
        f"{len(modules)} module(s), {counted} identifier(s) that exist:",
        "",
    ]
    for module in modules:
        lines.append(f"- `{module.get('address')}` -- {len(module.get('identifiers', {}))}")
    lines += [
        "",
        "Attach the observation file to this issue. It contains no values, no serial "
        "numbers and no VIN -- only which identifiers exist, and their length and type.",
        "",
        "Nothing here is confirmed. Identifiers still need proving against this car and "
        "then a second one of the same platform, per docs/contribute-vehicle-data.md.",
    ]

    query = urlencode(
        {
            "labels": "vehicle-data",
            "title": f"Vehicle data: {prefix}",
            "body": "\n".join(lines),
        }
    )
    return f"{base}?{query}"


def _decode_for(entry: dict[str, Any]) -> dict[str, Any]:
    """A starting-point decode block for one observed identifier.

    Reflects only what was observed: the payload's length, and whether it was printable.
    The type is a suggestion for a human to correct, which is why every drafted read is
    marked with a TODO rather than a name.
    """
    raw = str(entry.get("raw", ""))
    length = len(bytes.fromhex(raw)) if _is_hex(raw) else 0
    if entry.get("text"):
        return {"type": "ascii"}
    return {"type": "uint", "length": length or 1}


def _address_from(module: str) -> tuple[int, int]:
    """Parse the ``714/77E`` label a sweep records for its module."""
    left, _, right = str(module).partition("/")
    try:
        return int(left, 16), int(right, 16)
    except ValueError:
        raise DraftError(
            f"could not read an address pair from {module!r}. A sweep records it as "
            f"'714/77E'; a sweep of a named module cannot be drafted from."
        ) from None


def draft_profile(
    sweeps: list[dict[str, Any]],
    *,
    profile_id: str,
    make: str,
    platform: str,
) -> dict[str, Any]:
    """Build a candidate vehicle profile from one or more module sweeps.

    Every read is emitted at ``confidence: community`` with a ``TODO`` name, because a
    sweep proves an identifier exists and nothing at all about what it holds. Naming a
    read ``odometer_km`` is a claim, and this function is not in a position to make it --
    the person with the car is.
    """
    if not sweeps:
        raise DraftError("no sweeps given")

    ecus = []
    for sweep in sweeps:
        request_id, response_id = _address_from(sweep.get("module", ""))
        reads = []
        for entry in sweep.get("observations", []):
            if entry.get("status") != "data":
                continue
            did = int(str(entry["did"]), 16)
            standard = entry.get("standard_name")
            reads.append(
                {
                    # A standardised identifier can be named, because ISO 14229 names it.
                    # Everything else is a TODO: the sweep found bytes, not a meaning.
                    # Lowercase because the schema's id pattern requires it. The TODO is
                    # carried in the label too, which is what a report would print.
                    "id": standard or f"todo_rename_{did:04x}",
                    "label": (
                        standard.replace("_", " ") if standard else f"TODO identify 0x{did:04X}"
                    ),
                    "did": HexInt(did),
                    "confidence": "official" if standard else "community",
                    "decode": _decode_for(entry),
                }
            )
        ecus.append(
            {
                "name": f"TODO name this module ({request_id:03X}/{response_id:03X})",
                "request_id": HexInt(request_id),
                "response_id": HexInt(response_id),
                "session": "extended",
                "reads": reads,
            }
        )

    return {
        "meta": {
            "id": profile_id,
            "make": make,
            "platform": platform,
            "confidence": "community",
            "note": (
                "DRAFT, generated from a sweep by 'carpi defs draft'. Every read marked "
                "TODO was found to exist and is otherwise unidentified. Nothing here is "
                "confirmed. Do not submit this until each read has been proven against "
                "the car, and then against a second car of the same platform whose true "
                "state was independently known."
            ),
        },
        "ecus": ecus,
    }
