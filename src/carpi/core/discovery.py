"""Finding the modules a vehicle does not advertise.

OBD-II requires a vehicle to answer on eight addresses, ``0x7E8``-``0x7EF``, and those
are the emissions-related modules. Everything else is elsewhere: the instrument cluster
holding the odometer, the ABS module, the airbag controller, the body electronics. A
generic scan tool never speaks to any of them, which is most of why a generic scan tool
tells you so little about a used car.

There is no standard map of those addresses, and each manufacturer chose differently.
So they are found rather than looked up: send one benign request to each candidate
address and note who answers, and on which identifier. One frame per address, and it
cannot miss a module sitting somewhere nobody thought to try.

Safety
------
Discovery transmits, so it gets treated carefully:

* The probe is ``TesterPresent`` (``0x3E 0x00``), the most inert request in UDS. A
  refusal is as good as an acceptance -- both prove somebody is listening.
* Only read-only services may ever be used as a probe, checked at the call.
* Requests are rate limited. A diagnostic bus is shared with real traffic, and
  saturating it is the one way a read-only tool could affect a running vehicle.
* :func:`observe_traffic` maps the bus passively, sending nothing at all. On an
  unfamiliar vehicle that is the right first step.

Do this with the vehicle stationary. Nothing here can change a module's configuration,
but a bus busy with diagnostic requests is not what a car was designed around, and
there is no reason to find out at speed.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from threading import Event

from carpi.core.protocol.uds import FORBIDDEN_SERVICES
from carpi.core.transport.base import (
    FUNCTIONAL_REQUEST_11BIT,
    FUNCTIONAL_REQUEST_29BIT,
    RESPONSE_BASE_11BIT,
    RESPONSE_LAST_11BIT,
    EcuAddress,
)
from carpi.core.transport.canbus import CanLink

__all__ = [
    "DEFAULT_SWEEP_HIGH",
    "DEFAULT_SWEEP_LOW",
    "TESTER_PRESENT_PROBE",
    "DiscoveredModule",
    "observe_traffic",
    "sweep_addresses",
]

log = logging.getLogger(__name__)

# The 11-bit diagnostic neighbourhood. Manufacturers put diagnostic addresses in here
# by convention; the OBD-II eight are a small reserved corner of it.
DEFAULT_SWEEP_LOW = 0x700
DEFAULT_SWEEP_HIGH = 0x7FF

# TesterPresent with the "response required" sub-function. Two payload bytes, so the
# ISO-TP single-frame PCI byte is 0x02.
TESTER_PRESENT_PROBE = bytes([0x02, 0x3E, 0x00])

# Read the standardised VIN identifier. A heavier probe than TesterPresent, but some
# modules answer 0x22 while ignoring 0x3E outside a session.
READ_VIN_PROBE = bytes([0x03, 0x22, 0xF1, 0x90])

PROBES = {"tester-present": TESTER_PRESENT_PROBE, "read-vin": READ_VIN_PROBE}


@dataclass(frozen=True)
class DiscoveredModule:
    """A module that answered, and the address pair to reach it on."""

    request_id: int
    response_id: int
    extended: bool = False
    negative: bool = False
    service: int | None = None
    nrc: int | None = None

    @property
    def address(self) -> EcuAddress:
        return EcuAddress(tx_id=self.request_id, rx_id=self.response_id, extended=self.extended)

    @property
    def is_obd_address(self) -> bool:
        """Whether generic OBD-II would have found this module anyway."""
        return RESPONSE_BASE_11BIT <= self.response_id <= RESPONSE_LAST_11BIT

    @property
    def label(self) -> str:
        width = 8 if self.extended else 3
        return f"{self.request_id:0{width}X}/{self.response_id:0{width}X}"

    def as_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "response_id": self.response_id,
            "label": self.label,
            "extended": self.extended,
            "obd_address": self.is_obd_address,
            "answered_negatively": self.negative,
            "nrc": self.nrc,
        }

    def __str__(self) -> str:
        kind = "OBD-II" if self.is_obd_address else "manufacturer"
        how = "refused politely" if self.negative else "answered"
        return f"{self.label}  ({kind}, {how})"


@dataclass
class SweepStats:
    """What a sweep did, for reporting and for spotting a bus that is unwell."""

    probed: int = 0
    found: int = 0
    send_failures: int = 0
    elapsed: float = 0.0
    modules: list[DiscoveredModule] = field(default_factory=list)


def observe_traffic(
    link: CanLink,
    duration: float = 5.0,
    *,
    on_progress: Callable[[str], None] | None = None,
) -> dict[int, int]:
    """Watch the bus without transmitting. Returns frame counts by arbitration ID.

    Completely inert, and therefore the right way to make first contact with a vehicle
    you do not know: it confirms the wiring, the bitrate and that the bus is alive
    before anything is sent. An empty result after several seconds with the ignition on
    means the interface is not actually connected to a live bus, and no amount of
    probing afterwards will work.
    """
    counts: dict[int, int] = {}
    deadline = time.monotonic() + max(0.0, duration)
    with link.raw_reader() as reader:
        while time.monotonic() < deadline:
            message = reader.get_message(timeout=0.2)
            if message is None:
                continue
            counts[message.arbitration_id] = counts.get(message.arbitration_id, 0) + 1
    if on_progress is not None:
        on_progress(f"observed {sum(counts.values())} frames from {len(counts)} sources")
    return dict(sorted(counts.items()))


def sweep_addresses(
    link: CanLink,
    *,
    low: int = DEFAULT_SWEEP_LOW,
    high: int = DEFAULT_SWEEP_HIGH,
    probe: bytes = TESTER_PRESENT_PROBE,
    request_delay: float = 0.02,
    response_window: float = 0.08,
    skip: Iterable[int] = (),
    stop: Event | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> SweepStats:
    """Probe each address in ``[low, high]`` and report which ones answer.

    *response_window* is how long to wait for a reply before moving on. Too short and
    slow modules are missed; too long and a full sweep takes minutes. The default suits
    a quiet diagnostic bus.

    A module is recorded against the first arbitration ID that replies within its
    window. On a bus carrying live powertrain broadcast traffic this can mis-pair, which
    is why the result marks whether the reply came from an OBD-II address and why
    :func:`observe_traffic` is worth running first: an ID already broadcasting on its own
    is not a reply to anything.
    """
    _reject_forbidden_probe(probe)
    if low > high:
        raise ValueError(f"empty range: 0x{low:X} to 0x{high:X}")

    # The functional broadcast is not a module. Probing it draws replies from every
    # OBD-II module at once, and the first would be credited to 0x7DF as though that
    # were its address -- which then hides the real physical address behind an
    # already-seen response ID.
    skipped = set(skip) | {FUNCTIONAL_REQUEST_11BIT, FUNCTIONAL_REQUEST_29BIT}
    stop = stop or Event()
    stats = SweepStats()
    started = time.monotonic()
    seen_responses: set[int] = set()

    # Frames already flowing before anything is sent are broadcast traffic, not replies.
    # Excluding them removes the main source of false pairings.
    ambient = _ambient_ids(link, 0.3)
    if ambient and on_progress is not None:
        on_progress(f"ignoring {len(ambient)} arbitration IDs already broadcasting")

    with link.raw_reader() as reader:
        for request_id in range(low, high + 1):
            if stop.is_set():
                break
            if request_id in skipped:
                continue

            # Drain anything that arrived during the inter-request delay, so a reply is
            # not credited to the wrong address.
            while reader.get_message(timeout=0.0) is not None:
                pass

            try:
                link.send_raw(request_id, probe)
            except Exception as exc:  # noqa: BLE001 - one bad address must not end a sweep
                stats.send_failures += 1
                log.debug("probe of 0x%03X could not be sent: %s", request_id, exc)
                continue
            stats.probed += 1

            module = _collect_reply(
                reader,
                request_id=request_id,
                window=response_window,
                extended=link.extended,
                ambient=ambient,
                already_seen=seen_responses,
            )
            if module is not None:
                seen_responses.add(module.response_id)
                stats.modules.append(module)
                stats.found += 1
                if on_progress is not None:
                    on_progress(f"found {module}")

            if request_delay:
                stop.wait(request_delay)

    stats.elapsed = time.monotonic() - started
    if on_progress is not None:
        on_progress(
            f"probed {stats.probed} addresses in {stats.elapsed:.1f}s, "
            f"found {stats.found} module(s)"
        )
    return stats


def _reject_forbidden_probe(probe: bytes) -> None:
    """Refuse to use anything that could change the vehicle as a discovery probe."""
    if len(probe) < 2:
        raise ValueError("a probe needs at least a PCI byte and a service byte")
    service = probe[1]
    if service in FORBIDDEN_SERVICES:
        raise ValueError(
            f"0x{service:02X} ({FORBIDDEN_SERVICES[service]}) must never be used as a "
            f"probe: discovery sends to addresses whose purpose is unknown, so the "
            f"request has to be one that cannot do anything"
        )


def _ambient_ids(link: CanLink, duration: float) -> set[int]:
    seen: set[int] = set()
    deadline = time.monotonic() + duration
    with link.raw_reader() as reader:
        while time.monotonic() < deadline:
            message = reader.get_message(timeout=0.05)
            if message is not None:
                seen.add(message.arbitration_id)
    return seen


def _collect_reply(
    reader,
    *,
    request_id: int,
    window: float,
    extended: bool,
    ambient: set[int],
    already_seen: set[int],
) -> DiscoveredModule | None:
    deadline = time.monotonic() + window
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        message = reader.get_message(timeout=remaining)
        if message is None:
            return None
        if message.arbitration_id == request_id:
            continue  # our own frame, echoed back by some interfaces
        if message.arbitration_id in ambient or message.arbitration_id in already_seen:
            continue
        if message.is_extended_id != extended:
            continue

        data = bytes(message.data)
        negative = False
        service: int | None = None
        nrc: int | None = None
        # Single frame: low nibble of byte 0 is the length, byte 1 the service.
        if len(data) >= 3 and (data[0] & 0xF0) == 0x00:
            if data[1] == 0x7F:
                negative = True
                service = data[2] if len(data) > 2 else None
                nrc = data[3] if len(data) > 3 else None
            else:
                service = data[1]

        return DiscoveredModule(
            request_id=request_id,
            response_id=message.arbitration_id,
            extended=extended,
            negative=negative,
            service=service,
            nrc=nrc,
        )
