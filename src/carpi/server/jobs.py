"""Scan jobs and their progress.

A scan takes tens of seconds, so the HTTP request that starts one cannot wait for it.
Each scan becomes a job the client polls or subscribes to.

Progress is kept as a plain list guarded by a lock, and subscribers ask for everything
after an index they already have. That is deliberately duller than pushing events
through an asyncio queue: the scan runs in a worker thread, the sockets live on the
event loop, and a snapshot-by-index avoids bridging the two entirely. Progress text
arrives a few times a second at most, so nothing is gained by being cleverer.
"""

from __future__ import annotations

import logging
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from carpi.core.rules import Evaluation
from carpi.core.scan import ScanResult
from carpi.report.text import to_dict

__all__ = ["JobStore", "ScanJob"]

log = logging.getLogger(__name__)

STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_DONE = "done"
STATE_FAILED = "failed"

# An appliance may stay powered for hours and be scanned repeatedly. Reports are held
# in memory, so the history is capped rather than allowed to grow until the Pi swaps.
DEFAULT_HISTORY = 20


@dataclass
class ScanJob:
    """One scan, from request to report."""

    id: str
    state: str = STATE_QUEUED
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    finished_at: str | None = None
    claimed_odometer_km: float | None = None
    result: ScanResult | None = None
    evaluation: Evaluation | None = None
    error: str | None = None
    _events: list[str] = field(default_factory=list, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def finished(self) -> bool:
        return self.state in (STATE_DONE, STATE_FAILED)

    def add_event(self, message: str) -> None:
        """Record a progress message. Safe to call from the scan's worker thread."""
        with self._lock:
            self._events.append(message)

    def events_since(self, index: int) -> tuple[int, list[str]]:
        """Events after *index*, with the new index to pass next time."""
        with self._lock:
            total = len(self._events)
            if index >= total:
                return total, []
            return total, list(self._events[index:])

    @property
    def event_count(self) -> int:
        with self._lock:
            return len(self._events)

    def summary(self) -> dict[str, Any]:
        """State and headline findings, without the full report payload."""
        payload: dict[str, Any] = {
            "id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "claimed_odometer_km": self.claimed_odometer_km,
            "event_count": self.event_count,
            "error": self.error,
        }
        if self.result is not None:
            payload["vin"] = self.result.vin
            payload["module_count"] = len(self.result.ecus)
        if self.evaluation is not None:
            payload["findings"] = [
                {
                    "rule_id": finding.rule_id,
                    "title": finding.title,
                    "severity": finding.severity,
                }
                for finding in self.evaluation.findings
            ]
            payload["worst_severity"] = self.evaluation.worst_severity
            payload["passed_count"] = len(self.evaluation.passed)
            # Surfaced alongside the counts on purpose. "Could not be assessed" must
            # never be presentable as "passed", at any layer of this system.
            payload["not_assessed_count"] = len([s for s in self.evaluation.skipped if s.missing])
        return payload

    def report(self) -> dict[str, Any] | None:
        """The full inspection document, or ``None`` if the scan has not finished."""
        if self.result is None or self.evaluation is None:
            return None
        return to_dict(self.result, self.evaluation)

    def mark_running(self) -> None:
        self.state = STATE_RUNNING

    def mark_done(self, result: ScanResult, evaluation: Evaluation) -> None:
        self.result = result
        self.evaluation = evaluation
        self.state = STATE_DONE
        self.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.add_event("scan complete")

    def mark_failed(self, error: str) -> None:
        self.error = error
        self.state = STATE_FAILED
        self.finished_at = datetime.now(UTC).isoformat(timespec="seconds")
        self.add_event(f"scan failed: {error}")


class JobStore:
    """Recent scans, newest last, capped."""

    def __init__(self, history: int = DEFAULT_HISTORY) -> None:
        self._history = max(1, history)
        self._jobs: OrderedDict[str, ScanJob] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, *, claimed_odometer_km: float | None = None) -> ScanJob:
        job = ScanJob(id=secrets.token_hex(5), claimed_odometer_km=claimed_odometer_km)
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._history:
                evicted, _ = self._jobs.popitem(last=False)
                log.debug("evicted scan %s from history", evicted)
        return job

    def get(self, job_id: str) -> ScanJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def recent(self) -> list[ScanJob]:
        """Newest first, which is the order a UI wants."""
        with self._lock:
            return list(reversed(self._jobs.values()))

    def latest(self) -> ScanJob | None:
        with self._lock:
            if not self._jobs:
                return None
            return next(reversed(self._jobs.values()))
