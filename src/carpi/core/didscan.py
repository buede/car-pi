"""Sweeping a module's data identifiers to find out what it will tell you.

This is the tool that makes the definition database grow. Manufacturers publish nothing,
but a module will answer ``ReadDataByIdentifier`` for whatever it holds, so the contents
can be enumerated by asking for every identifier in turn and recording the replies.

The three outcomes are all useful:

``data``
    The identifier exists and returned bytes. What they *mean* still has to be worked
    out, usually by changing something on the car and watching which value moves.
``protected``
    ``NRC 0x33``/``0x34``. The identifier exists and the manufacturer locked it. This is
    a positive finding, and often marks the interesting ones.
``unsupported``
    Nothing there. The overwhelming majority.

A sweep of the full 16-bit space is 65,536 requests and will take a long time; it is
also completely read-only, and the worst it can do is make a module log that a tester
talked to it. Some manufacturers do record that. It is your car, but if it is under
warranty, be aware the record may exist.

The output is designed to be shareable: enough context that somebody else can make sense
of it, and a warning that it contains the VIN before it goes anywhere public.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Event
from typing import Any

from carpi.core.protocol.uds import STANDARD_DIDS, UdsClient, UdsError, UdsNegativeResponse
from carpi.core.transport.base import NoResponse

__all__ = [
    "INTERESTING_RANGES",
    "DidObservation",
    "DidScanReport",
    "parse_ranges",
    "scan_dids",
]

log = logging.getLogger(__name__)

STATUS_DATA = "data"
STATUS_PROTECTED = "protected"
STATUS_UNSUPPORTED = "unsupported"
STATUS_CONDITIONS = "conditions-not-correct"
STATUS_ERROR = "error"

# Where manufacturer data tends to live, for a first pass that finishes in minutes
# rather than hours. The standardised F18x/F19x identification block is included
# because it is cheap and anchors the rest: it names the module and its software.
INTERESTING_RANGES: tuple[tuple[int, int], ...] = (
    (0x0100, 0x02FF),
    (0x1000, 0x10FF),
    (0x2000, 0x22FF),
    (0xF180, 0xF1A0),
)

# A sweep that cannot reach the bus at all should stop rather than grind through 65,536
# timeouts. Distinguished from a module simply refusing, which is expected and common.
_MAX_CONSECUTIVE_TRANSPORT_ERRORS = 25


@dataclass(frozen=True)
class DidObservation:
    """What one identifier did when asked."""

    did: int
    status: str
    raw: bytes | None = None
    nrc: int | None = None
    text: str | None = None

    @property
    def exists(self) -> bool:
        """Whether the module acknowledged holding something here."""
        return self.status in (STATUS_DATA, STATUS_PROTECTED)

    @property
    def standard_name(self) -> str | None:
        """The ISO 14229 name, if this is a standardised identifier."""
        return STANDARD_DIDS.get(self.did)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"did": f"0x{self.did:04X}", "status": self.status}
        if self.standard_name:
            payload["standard_name"] = self.standard_name
        if self.raw is not None:
            payload["raw"] = self.raw.hex()
            payload["length"] = len(self.raw)
        if self.text:
            payload["text"] = self.text
        if self.nrc is not None:
            payload["nrc"] = f"0x{self.nrc:02X}"
        return payload

    def __str__(self) -> str:
        name = f" ({self.standard_name})" if self.standard_name else ""
        if self.status == STATUS_DATA and self.raw is not None:
            shown = self.text or self.raw.hex(" ")
            return f"0x{self.did:04X}{name}: {shown}"
        return f"0x{self.did:04X}{name}: {self.status}"


@dataclass
class DidScanReport:
    """A sweep of one module, ready to be shared."""

    module: str
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    ranges: tuple[tuple[int, int], ...] = ()
    observations: list[DidObservation] = field(default_factory=list)
    vin: str | None = None
    aborted: str | None = None

    @property
    def found(self) -> list[DidObservation]:
        """Only the identifiers that exist -- what a contribution is actually about."""
        return [item for item in self.observations if item.exists]

    def as_dict(self, *, anonymise: bool = False) -> dict[str, Any]:
        """Serialise the report.

        With *anonymise*, the VIN is removed and any payload containing it is redacted.
        A VIN identifies one physical car and, through it, a person -- so a scan posted
        to a public issue tracker should not carry one.
        """
        observations = [item.as_dict() for item in self.observations]
        if anonymise and self.vin:
            needle = self.vin.encode("ascii", errors="ignore").hex().lower()
            for entry in observations:
                raw = entry.get("raw")
                if raw and needle and needle in raw.lower():
                    entry["raw"] = "<redacted: contained the VIN>"
                    entry.pop("text", None)
                elif entry.get("text") and self.vin in str(entry["text"]):
                    entry["text"] = "<redacted: contained the VIN>"
                    entry["raw"] = "<redacted: contained the VIN>"

        return {
            "schema": "carpi.didscan/1",
            "module": self.module,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ranges": [f"0x{low:04X}-0x{high:04X}" for low, high in self.ranges],
            "vin": None if anonymise else self.vin,
            "anonymised": anonymise,
            "aborted": self.aborted,
            "counts": {
                "probed": len(self.observations),
                "data": sum(1 for o in self.observations if o.status == STATUS_DATA),
                "protected": sum(1 for o in self.observations if o.status == STATUS_PROTECTED),
            },
            "observations": observations,
        }


def parse_ranges(text: str) -> tuple[tuple[int, int], ...]:
    """Parse ``"0x2200-0x22ff,0xf190"`` into inclusive ranges."""
    ranges: list[tuple[int, int]] = []
    for chunk in text.split(","):
        piece = chunk.strip()
        if not piece:
            continue
        if "-" in piece:
            start, _, end = piece.partition("-")
            low, high = int(start, 16), int(end, 16)
        else:
            low = high = int(piece, 16)
        if not (0 <= low <= high <= 0xFFFF):
            raise ValueError(f"{piece!r} is not a valid identifier range")
        ranges.append((low, high))
    if not ranges:
        raise ValueError("no ranges given")
    return tuple(ranges)


def _iterate(ranges: Sequence[tuple[int, int]], skip: set[int]) -> Iterator[int]:
    for low, high in ranges:
        for did in range(low, high + 1):
            if did not in skip:
                yield did


def _count(ranges: Sequence[tuple[int, int]]) -> int:
    return sum(high - low + 1 for low, high in ranges)


def scan_dids(
    client: UdsClient,
    ranges: Sequence[tuple[int, int]] = INTERESTING_RANGES,
    *,
    delay: float = 0.01,
    skip: Iterable[int] = (),
    stop: Event | None = None,
    on_progress: Callable[[str], None] | None = None,
    on_observation: Callable[[DidObservation], None] | None = None,
    vin: str | None = None,
) -> DidScanReport:
    """Sweep *ranges* on one module.

    *on_observation* is called for every identifier as it is probed, which is how a
    caller streams results to disk. A full sweep takes a long time, and a crash or a
    disconnected cable partway through should not lose what was already learned --
    those results can be fed back as *skip* to resume.
    """
    stop = stop or Event()
    skipped = set(skip)
    report = DidScanReport(module=str(client.address), ranges=tuple(ranges), vin=vin)
    total = _count(ranges) - len(skipped)
    consecutive_transport_errors = 0
    started = time.monotonic()

    for index, did in enumerate(_iterate(ranges, skipped), start=1):
        if stop.is_set():
            report.aborted = "stopped by request"
            break

        observation = _probe(client, did)
        report.observations.append(observation)
        if on_observation is not None:
            on_observation(observation)

        if observation.status == STATUS_ERROR:
            consecutive_transport_errors += 1
            if consecutive_transport_errors >= _MAX_CONSECUTIVE_TRANSPORT_ERRORS:
                # Not a module refusing -- a bus that has stopped answering. Grinding
                # through tens of thousands of timeouts would waste an hour proving it.
                report.aborted = (
                    f"{consecutive_transport_errors} consecutive transport errors; "
                    f"the module stopped responding entirely"
                )
                break
        else:
            consecutive_transport_errors = 0

        if observation.exists and on_progress is not None:
            on_progress(str(observation))
        elif on_progress is not None and index % 256 == 0:
            elapsed = time.monotonic() - started
            rate = index / elapsed if elapsed else 0
            remaining = (total - index) / rate if rate else 0
            on_progress(
                f"{index}/{total} probed, {len(report.found)} found, "
                f"~{remaining / 60:.0f} min remaining"
            )

        if delay:
            stop.wait(delay)

    report.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
    if on_progress is not None:
        on_progress(
            f"done: {len(report.found)} identifier(s) exist out of "
            f"{len(report.observations)} probed"
        )
    return report


def _probe(client: UdsClient, did: int) -> DidObservation:
    try:
        raw = client.read_did(did)
    except UdsNegativeResponse as exc:
        if exc.is_protected:
            return DidObservation(did=did, status=STATUS_PROTECTED, nrc=exc.nrc)
        if exc.needs_different_conditions:
            # Worth keeping distinct: retrying with the engine running, or stationary,
            # may well succeed where this attempt did not.
            return DidObservation(did=did, status=STATUS_CONDITIONS, nrc=exc.nrc)
        return DidObservation(did=did, status=STATUS_UNSUPPORTED, nrc=exc.nrc)
    except NoResponse:
        return DidObservation(did=did, status=STATUS_UNSUPPORTED)
    except UdsError as exc:
        log.debug("DID 0x%04X: %s", did, exc)
        return DidObservation(did=did, status=STATUS_ERROR)

    return DidObservation(did=did, status=STATUS_DATA, raw=raw, text=_as_text(raw))


def _as_text(raw: bytes) -> str | None:
    if raw and all(0x20 <= byte <= 0x7E for byte in raw):
        return raw.decode("ascii").strip()
    return None
