"""Exclusive access to the vehicle interface.

**One interface means one conversation at a time.** OBD-II is request/response over
ISO-TP, and two callers sharing a channel would each receive the other's replies --
producing values silently attributed to the wrong parameter rather than an error. The
protocol layer already checks that an ECU echoed the PID it was asked about, but that
only turns the corruption into an exception; it does not make concurrent use safe.

So every conversation goes through :class:`VehicleGateway`, which hands out one claim
at a time and refuses the rest immediately. Refusing beats queueing here: somebody
holding a phone wants to be told the unit is already scanning, not to watch a
spinner for forty seconds because their first tap is still running.

The link is opened per claim and closed after. That costs milliseconds and removes a
whole category of bug -- stale ISO-TP buffers, leaked SocketCAN handles surviving into
the next scan -- which matters more on an appliance that stays powered for hours.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from typing import Protocol

from carpi.core.database import Database
from carpi.core.transport.canbus import DEFAULT_BITRATE, CanLink

__all__ = [
    "Activity",
    "BusBusy",
    "DirectProvider",
    "LinkProvider",
    "SimulatedProvider",
    "VehicleGateway",
]

log = logging.getLogger(__name__)

_sim_channels = itertools.count()


class BusBusy(RuntimeError):
    """The interface is already in use by another conversation."""

    def __init__(self, activity: Activity) -> None:
        self.activity = activity
        super().__init__(
            f"the vehicle interface is busy: {activity.kind} {activity.id} "
            f"started {activity.age:.0f}s ago"
        )


@dataclass(frozen=True)
class Activity:
    """What currently holds the interface."""

    kind: str
    id: str
    started: float

    @property
    def age(self) -> float:
        return time.monotonic() - self.started

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "id": self.id, "age_seconds": round(self.age, 1)}


class LinkProvider(Protocol):
    """Opens a link to a vehicle, real or simulated."""

    @property
    def description(self) -> str:
        """Short label for the UI, e.g. ``socketcan can0``."""
        ...

    @property
    def is_simulated(self) -> bool:
        """True when there is no real vehicle, so the UI can say so plainly."""
        ...

    def session(self) -> AbstractContextManager[CanLink]:
        """Open a link, and tear it down on exit."""
        ...


class DirectProvider:
    """A real vehicle, or a bus somebody else is simulating."""

    def __init__(
        self,
        kind: str = "socketcan",
        channel: str | None = None,
        *,
        bitrate: int = DEFAULT_BITRATE,
        extended: bool = False,
        fd: bool = False,
    ) -> None:
        self.kind = kind
        self.channel = channel
        self.bitrate = bitrate
        self.extended = extended
        self.fd = fd

    @property
    def description(self) -> str:
        return f"{self.kind} {self.channel or 'default'}"

    @property
    def is_simulated(self) -> bool:
        return False

    @contextmanager
    def session(self) -> Iterator[CanLink]:
        link = CanLink.open(
            self.kind,
            self.channel,
            bitrate=self.bitrate,
            extended=self.extended,
            fd=self.fd,
        )
        try:
            yield link
        finally:
            link.close()


class SimulatedProvider:
    """An in-process simulated car, for development and demonstration.

    Each session gets a fresh channel name and a fresh simulator. Reusing one would
    let a previous session's ECUs answer the next one, and the resulting failure would
    surface somewhere unrelated.
    """

    def __init__(self, scenario: str = "recently-cleared") -> None:
        self.scenario = scenario

    @property
    def description(self) -> str:
        return f"simulated vehicle ({self.scenario})"

    @property
    def is_simulated(self) -> bool:
        return True

    @contextmanager
    def session(self) -> Iterator[CanLink]:
        # Imported here so the simulator is not a hard dependency of serving a real car.
        from carpi.sim import SimulatedVehicle, get_scenario

        spec = get_scenario(self.scenario)
        channel = f"carpi-serve-{next(_sim_channels)}"
        vehicle = SimulatedVehicle.from_scenario(spec, channel=channel)
        vehicle.start()
        try:
            link = CanLink.open("virtual", channel)
            try:
                yield link
            finally:
                link.close()
        finally:
            vehicle.stop()


class VehicleGateway:
    """Serialises all access to the vehicle interface."""

    def __init__(
        self,
        provider: LinkProvider,
        database: Database,
        *,
        timeout: float = 1.0,
    ) -> None:
        self.provider = provider
        self.database = database
        self.timeout = timeout
        self._lock = threading.Lock()
        self._activity: Activity | None = None

    @property
    def activity(self) -> Activity | None:
        """What holds the interface right now, if anything."""
        return self._activity

    @property
    def busy(self) -> bool:
        return self._activity is not None

    @contextmanager
    def claim(self, kind: str, ident: str) -> Iterator[CanLink]:
        """Take exclusive use of the interface.

        Raises :class:`BusBusy` at once if something else holds it, rather than
        blocking -- see the module docstring.
        """
        if not self._lock.acquire(blocking=False):
            # Read after failing to acquire, so the message names the actual holder.
            current = self._activity
            raise BusBusy(current or Activity(kind="unknown", id="-", started=time.monotonic()))

        activity = Activity(kind=kind, id=ident, started=time.monotonic())
        self._activity = activity
        log.info("interface claimed by %s %s (%s)", kind, ident, self.provider.description)
        try:
            with self.provider.session() as link:
                yield link
        finally:
            self._activity = None
            self._lock.release()
            log.info("interface released by %s %s after %.1fs", kind, ident, activity.age)
