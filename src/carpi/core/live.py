"""Continuous sampling of live values from one ECU.

A scan is a snapshot. Some faults only show themselves in motion -- fuel trims that
are fine at idle and awful under load, a coolant temperature that never climbs, a
misfire that appears at a particular engine speed. Watching values change during a
test drive is the difference between "this car has no stored faults" and "this car is
about to have one".

Polling is deliberately sequential over one channel. OBD-II gives no way to subscribe
to a value, so every sample is a request/response round trip, and the achievable rate
is a property of the bus and the ECU rather than something to tune here.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from threading import Event

from carpi.core.protocol.obd2 import Obd2Client, PidReading, ProtocolError
from carpi.core.transport.base import NoResponse

__all__ = ["DEFAULT_LIVE_PIDS", "LivePoller", "LiveSample"]

log = logging.getLogger(__name__)

# Values worth watching on a test drive, in the order a person reads them. Whichever
# of these the vehicle does not support is dropped at startup rather than retried on
# every cycle -- an unsupported PID costs a full timeout, and a handful of those would
# dominate the sample rate.
DEFAULT_LIVE_PIDS: tuple[str, ...] = (
    "engine_rpm",
    "vehicle_speed",
    "coolant_temp",
    "engine_load",
    "throttle_position",
    "maf_rate",
    "stft_bank1",
    "ltft_bank1",
    "stft_bank2",
    "ltft_bank2",
    "intake_air_temp",
    "timing_advance",
    "control_module_voltage",
    "engine_oil_temp",
    "fuel_system_status",
)


@dataclass(frozen=True)
class LiveSample:
    """One pass over the polled PIDs."""

    sequence: int
    # Seconds since the poller started, not wall-clock: a portable unit may well have
    # no correct clock, having spent the last month unplugged in a drawer.
    elapsed: float
    readings: dict[str, PidReading] = field(default_factory=dict)
    failures: tuple[str, ...] = ()

    def numeric(self) -> dict[str, float]:
        """Just the scalar values, for plotting."""
        return {
            name: reading.value
            for name, reading in self.readings.items()
            if isinstance(reading.value, float)
        }


class LivePoller:
    """Repeatedly reads a set of PIDs from one ECU."""

    def __init__(
        self,
        client: Obd2Client,
        pid_names: Sequence[str] | None = None,
        *,
        interval: float = 0.2,
    ) -> None:
        """*interval* is a floor between cycles, not a target rate.

        If a cycle takes longer than *interval* -- which it will, on a slow ECU or a
        long PID list -- samples simply arrive as fast as the bus allows.
        """
        self._client = client
        self._requested = tuple(pid_names or DEFAULT_LIVE_PIDS)
        self._interval = max(0.0, interval)
        self._available: tuple[str, ...] | None = None

    @property
    def available(self) -> tuple[str, ...]:
        """PIDs this ECU actually answers, determined on first use."""
        if self._available is None:
            raise RuntimeError("call prepare() before reading `available`")
        return self._available

    def prepare(self) -> tuple[str, ...]:
        """Narrow the requested PIDs to those the ECU supports. Returns the survivors."""
        if self._available is not None:
            return self._available

        try:
            supported = self._client.supported_pids()
        except (NoResponse, ProtocolError) as exc:
            # Without a support map, try everything: better a slow stream than none.
            log.debug("support map unavailable, polling all requested PIDs: %s", exc)
            self._available = self._requested
            return self._available

        database = self._client.database
        keep: list[str] = []
        for name in self._requested:
            definition = database.pids_by_name.get(name)
            if definition is None:
                log.debug("live PID %r is not in the definition database", name)
                continue
            if definition.pid in supported:
                keep.append(name)
        self._available = tuple(keep)
        log.info(
            "%s: polling %d of %d requested live values",
            self._client.address,
            len(keep),
            len(self._requested),
        )
        return self._available

    def sample(self, sequence: int = 0, elapsed: float = 0.0) -> LiveSample:
        """Read every available PID once."""
        readings: dict[str, PidReading] = {}
        failures: list[str] = []
        for name in self.prepare():
            try:
                readings[name] = self._client.read_pid(name)
            except (NoResponse, ProtocolError) as exc:
                # Reported rather than swallowed: a value that drops out mid-drive is
                # itself a symptom, and hiding it would make a gap look like a plateau.
                log.debug("live read of %s failed: %s", name, exc)
                failures.append(name)
        return LiveSample(
            sequence=sequence,
            elapsed=elapsed,
            readings=readings,
            failures=tuple(failures),
        )

    def stream(self, stop: Event | None = None) -> Iterator[LiveSample]:
        """Yield samples until *stop* is set.

        Blocking, and intended to run in its own thread. The caller owns *stop*; the
        loop checks it between cycles and again while waiting, so shutdown does not
        have to wait out a whole interval.
        """
        stop = stop or Event()
        started = time.monotonic()
        sequence = 0
        while not stop.is_set():
            began = time.monotonic()
            yield self.sample(sequence=sequence, elapsed=began - started)
            sequence += 1
            remaining = self._interval - (time.monotonic() - began)
            if remaining > 0:
                stop.wait(remaining)
