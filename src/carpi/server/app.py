"""HTTP and WebSocket API, plus the UI it serves.

Shape of it: a scan takes tens of seconds, so ``POST /api/scans`` starts a job and
returns immediately, and the client follows progress over a WebSocket. Live values get
their own socket, sampled straight off the bus.

Everything that touches the vehicle is blocking, so it runs in a worker thread via
:func:`asyncio.to_thread`. Doing a one-second ISO-TP timeout on the event loop would
stall every other request, including the UI's own asset loads.

There is no authentication. The unit serves a read-only interface over its own
hotspot, and adding a login to a tool used one-handed in somebody's driveway would
cost more than it protects. That reasoning stops holding the moment writing to a
vehicle becomes possible: whoever implements coding (M5) must put authentication in
front of it, because the failure mode changes from "a passenger reads your fuel trims"
to "a passenger reconfigures your ABS".
"""

from __future__ import annotations

import asyncio
import logging
import time
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from carpi import __version__
from carpi.core.live import DEFAULT_LIVE_PIDS, LivePoller
from carpi.core.protocol.obd2 import Obd2Client
from carpi.core.scan import scan_vehicle
from carpi.server.jobs import DEFAULT_HISTORY, JobStore, ScanJob
from carpi.server.vehicle import BusBusy, VehicleGateway

__all__ = ["create_app", "static_dir"]

log = logging.getLogger(__name__)

# How often a progress socket checks for new events. Progress is prose for a human to
# read, so a quarter second is already faster than anyone can absorb it.
_PROGRESS_POLL = 0.25

_ENGINE_RESPONSE_ID = 0x7E8


def static_dir() -> Path:
    """Directory holding the PWA."""
    return Path(str(resources.files("carpi.server"))) / "static"


def create_app(
    gateway: VehicleGateway,
    *,
    history: int = DEFAULT_HISTORY,
    serve_ui: bool = True,
) -> FastAPI:
    """Build the application around an already-configured *gateway*."""
    # FastAPI's interactive docs load Swagger's bundle from a public CDN, so on the unit's
    # own hotspot -- where there is no internet at all -- /api/docs renders a blank page.
    # A route that is blank exactly where the product is used is worse than an absent one,
    # so it is served only when the UI is not, which is the development and testing case.
    # The schema itself stays available either way; it needs no external asset to be useful.
    app = FastAPI(
        title="car-pi",
        version=__version__,
        description="Open-source vehicle diagnostics. Read-only.",
        docs_url=None if serve_ui else "/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    store = JobStore(history=history)
    # Tasks are held so the garbage collector cannot drop a running scan mid-flight.
    running: set[asyncio.Task[None]] = set()

    app.state.gateway = gateway
    app.state.jobs = store

    # --- vehicle-facing work -------------------------------------------------

    def _perform_scan(job: ScanJob) -> Any:
        with gateway.claim("scan", job.id) as link:
            return scan_vehicle(
                link,
                gateway.database,
                claimed_odometer_km=job.claimed_odometer_km,
                timeout=gateway.timeout,
                on_progress=job.add_event,
            )

    async def _run_scan(job: ScanJob) -> None:
        job.mark_running()
        try:
            result = await asyncio.to_thread(_perform_scan, job)
            evaluation = result.evaluate(gateway.database)
            job.mark_done(result, evaluation)
        except BusBusy as exc:
            job.mark_failed(str(exc))
        except Exception as exc:  # noqa: BLE001 - a failed scan must not kill the server
            log.exception("scan %s failed", job.id)
            job.mark_failed(f"{type(exc).__name__}: {exc}")

    # --- status ---------------------------------------------------------------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        activity = gateway.activity
        return {
            "status": "ok",
            "version": __version__,
            "interface": gateway.provider.description,
            "simulated": gateway.provider.is_simulated,
            "busy": activity is not None,
            "activity": activity.as_dict() if activity else None,
            "definitions": {
                "pids": len(gateway.database.pids_by_number),
                "rules": len(gateway.database.rules),
            },
        }

    @app.get("/api/preflight")
    async def preflight(seconds: float = 3.0) -> dict[str, Any]:
        """Listen to the bus and say whether it is usable. Transmits nothing.

        A GET, and not only because it changes no server state: this genuinely sends no
        frame to the vehicle. It is also the shape the write firewall requires -- a test
        asserts the only POST route is ``/api/scans`` -- and that constraint happens to
        agree with what the operation actually is.

        Worth having on the phone path for the same reason it is worth having on the
        command line: scanning a silent bus produces a report saying the car answered
        nothing, which reads far too much like a clean car.
        """
        from carpi.core.discovery import check_bus

        # A simulated vehicle answers requests but broadcasts nothing, so listening finds
        # silence. Reporting that as a dead bus would send somebody trying the demo off to
        # check a bitrate and a wiring loom that do not exist.
        if gateway.provider.is_simulated:
            return {
                "verdict": "simulated",
                "summary": "this is a simulated vehicle, so there is no bus to check",
                "frames": 0,
                "error_frames": 0,
                "sources": 0,
                "advice": [],
            }

        duration = min(max(seconds, 0.5), 10.0)
        try:
            with gateway.claim("preflight", "http") as link:
                health = await asyncio.to_thread(check_bus, link, duration)
        except BusBusy as exc:
            raise HTTPException(status_code=409, detail={"message": str(exc)}) from None
        except Exception as exc:  # noqa: BLE001 - a bad interface must not kill the server
            log.exception("preflight failed")
            return {
                "verdict": "unavailable",
                "summary": f"{type(exc).__name__}: {exc}",
                "advice": [
                    "The interface could not be opened at all. Check that it exists and is up.",
                ],
            }
        return {
            "verdict": health.verdict,
            "summary": health.summary,
            "frames": health.frames,
            "error_frames": health.error_frames,
            "sources": len(health.sources),
            "advice": list(health.advice),
        }

    # --- scans ---------------------------------------------------------------

    @app.post("/api/scans", status_code=202)
    async def start_scan(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """Begin a scan. 409 if the interface is already in use."""
        if gateway.busy:
            activity = gateway.activity
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "The vehicle interface is already in use.",
                    "activity": activity.as_dict() if activity else None,
                },
            )

        odometer = (payload or {}).get("claimed_odometer_km")
        if odometer is not None:
            try:
                odometer = float(odometer)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=422, detail="claimed_odometer_km must be a number"
                ) from None
            if odometer < 0:
                raise HTTPException(
                    status_code=422, detail="claimed_odometer_km cannot be negative"
                )

        job = store.create(claimed_odometer_km=odometer)
        job.add_event("queued")
        task = asyncio.create_task(_run_scan(job))
        running.add(task)
        task.add_done_callback(running.discard)
        return {"id": job.id, "state": job.state}

    @app.get("/api/scans")
    async def list_scans() -> dict[str, Any]:
        return {"scans": [job.summary() for job in store.recent()]}

    @app.get("/api/scans/{job_id}")
    async def get_scan(job_id: str) -> dict[str, Any]:
        job = _require(store, job_id)
        return job.summary()

    @app.get("/api/scans/{job_id}/report")
    async def get_report(job_id: str) -> JSONResponse:
        """The full inspection document, identical to `carpi scan --format json`."""
        job = _require(store, job_id)
        report = job.report()
        if report is None:
            raise HTTPException(
                status_code=409,
                detail=f"scan {job_id} is {job.state}; no report yet",
            )
        return JSONResponse(report)

    @app.get("/api/scans/{job_id}/events")
    async def get_events(job_id: str, since: int = 0) -> dict[str, Any]:
        """Progress events, for clients that would rather poll than hold a socket."""
        job = _require(store, job_id)
        index, events = job.events_since(max(0, since))
        return {"index": index, "events": events, "state": job.state}

    # --- definitions ---------------------------------------------------------

    @app.get("/api/defs/pids")
    async def list_pids() -> dict[str, Any]:
        return {
            "pids": [
                {
                    "pid": definition.pid,
                    "name": definition.name,
                    "label": definition.label,
                    "unit": definition.unit,
                    "confidence": definition.confidence,
                }
                for definition in sorted(
                    gateway.database.pids_by_number.values(), key=lambda d: d.pid
                )
            ]
        }

    @app.get("/api/defs/rules")
    async def list_rules() -> dict[str, Any]:
        return {
            "rules": [
                {
                    "id": rule.id,
                    "title": rule.title,
                    "severity": rule.severity,
                    "explain": rule.explain,
                    "confidence": rule.confidence,
                    "requires": sorted(rule.required_facts),
                }
                for rule in gateway.database.rules_by_severity()
            ]
        }

    # --- websockets ----------------------------------------------------------

    @app.websocket("/ws/scans/{job_id}")
    async def ws_scan(socket: WebSocket, job_id: str) -> None:
        """Stream one scan's progress, then its result."""
        await socket.accept()
        job = store.get(job_id)
        if job is None:
            await socket.send_json({"type": "error", "message": f"no scan {job_id}"})
            await socket.close()
            return

        index = 0
        try:
            while True:
                index, events = job.events_since(index)
                for message in events:
                    await socket.send_json({"type": "progress", "message": message})
                if job.finished:
                    await socket.send_json({"type": "finished", "summary": job.summary()})
                    break
                await asyncio.sleep(_PROGRESS_POLL)
        except WebSocketDisconnect:
            return
        await socket.close()

    @app.websocket("/ws/live")
    async def ws_live(socket: WebSocket) -> None:
        """Stream live values until the client disconnects."""
        await socket.accept()
        ident = f"ws-{int(time.monotonic() * 1000) % 100000}"
        try:
            with gateway.claim("live", ident) as link:
                addresses = await asyncio.to_thread(link.discover_ecus)
                if not addresses:
                    await socket.send_json(
                        {
                            "type": "error",
                            "message": (
                                "No module answered. Check the ignition is ON rather "
                                "than in accessory mode."
                            ),
                        }
                    )
                    await socket.close()
                    return

                address = next(
                    (a for a in addresses if a.rx_id == _ENGINE_RESPONSE_ID), addresses[0]
                )
                client = Obd2Client(
                    link.channel(address), gateway.database, timeout=gateway.timeout
                )
                poller = LivePoller(client, DEFAULT_LIVE_PIDS)
                available = await asyncio.to_thread(poller.prepare)

                await socket.send_json(
                    {
                        "type": "ready",
                        "module": address.label,
                        "pids": [
                            {
                                "name": name,
                                "label": gateway.database.pid(name).label,
                                "unit": gateway.database.pid(name).unit,
                            }
                            for name in available
                        ],
                    }
                )

                started = time.monotonic()
                sequence = 0
                while True:
                    sample = await asyncio.to_thread(
                        poller.sample, sequence, time.monotonic() - started
                    )
                    await socket.send_json(
                        {
                            "type": "sample",
                            "sequence": sample.sequence,
                            "elapsed": round(sample.elapsed, 2),
                            "values": sample.numeric(),
                            "failures": list(sample.failures),
                        }
                    )
                    sequence += 1
        except BusBusy as exc:
            await socket.send_json({"type": "busy", "message": str(exc)})
            await socket.close()
        except WebSocketDisconnect:
            # Normal: the phone was locked, or the user switched tabs. The claim is
            # released by the context manager on the way out.
            log.debug("live socket %s disconnected", ident)
        except Exception as exc:  # noqa: BLE001
            log.exception("live socket %s failed", ident)
            with_error = {"type": "error", "message": f"{type(exc).__name__}: {exc}"}
            try:
                await socket.send_json(with_error)
                await socket.close()
            except (RuntimeError, WebSocketDisconnect):
                pass

    # --- the UI --------------------------------------------------------------

    if serve_ui:
        directory = static_dir()
        if directory.is_dir():
            # Mounted last so it cannot shadow an API route.
            app.mount("/", StaticFiles(directory=directory, html=True), name="ui")
        else:  # pragma: no cover - only reachable from a broken installation
            log.error("UI assets are missing from the installation at %s", directory)

    return app


def _require(store: JobStore, job_id: str) -> ScanJob:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no scan {job_id}")
    return job
