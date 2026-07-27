"""The ``carpi`` command.

``carpi demo`` is the zero-setup path: it runs a simulated car in-process and scans
it, so the whole stack can be exercised on any machine with nothing plugged in.
``carpi scan`` points the same code at a real vehicle.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from carpi import __version__
from carpi.core.database import Database, DefinitionError, defs_root
from carpi.core.scan import scan_vehicle
from carpi.core.transport.base import TransportError
from carpi.core.transport.canbus import DEFAULT_BITRATE, CanLink
from carpi.report.text import render_json, render_text
from carpi.sim import SCENARIOS, SimulatedVehicle, get_scenario

_SIM_CHANNEL = "carpi-demo"


def _configure_logging(verbose: int) -> None:
    level = logging.WARNING
    if verbose == 1:
        level = logging.INFO
    elif verbose >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_database(defs: Path | None) -> Database:
    try:
        return Database.load(defs)
    except DefinitionError as exc:
        raise click.ClickException(f"definition database is invalid: {exc}") from exc


def _emit(result, evaluation, output_format: str, out: Path | None, verbose: bool) -> None:
    """Write the report to stdout, or to *out*.

    Only the report itself goes to stdout. Status and progress messages go to stderr,
    so `--format json` produces a stream that can be piped straight into another tool
    without a human-readable preamble corrupting it.
    """
    if output_format == "json":
        text = render_json(result, evaluation)
    else:
        text = render_text(result, evaluation, verbose=verbose)
    if out is not None:
        out.write_text(text + "\n", encoding="utf-8")
        click.echo(f"written to {out}", err=True)
    else:
        click.echo(text)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="carpi")
@click.option("-v", "--verbose", count=True, help="Repeat for more detail.")
def cli(verbose: int) -> None:
    """Open-source vehicle diagnostics.

    Nothing in this tool writes to a vehicle, and clearing fault codes is
    deliberately not implemented -- it would destroy the evidence an inspection
    depends on.
    """
    _configure_logging(verbose)


@cli.command()
@click.option(
    "--transport",
    type=click.Choice(["socketcan", "virtual", "udp"]),
    default="socketcan",
    show_default=True,
    help="socketcan for a real car (Linux); virtual or udp for a simulator.",
)
@click.option("--channel", default=None, help="Interface name, e.g. can0.")
@click.option(
    "--bitrate",
    default=DEFAULT_BITRATE,
    show_default=True,
    help="Bus bitrate. Reported only; set the real value with 'ip link'.",
)
@click.option(
    "--extended/--standard",
    default=False,
    show_default=True,
    help="29-bit addressing instead of 11-bit. Most cars use 11-bit.",
)
@click.option("--fd", is_flag=True, help="CAN FD. Needed for many 2019-onward vehicles.")
@click.option(
    "--odometer",
    type=float,
    default=None,
    metavar="KM",
    help="Advertised mileage, enabling the odometer cross-check.",
)
@click.option("--timeout", type=float, default=1.0, show_default=True, help="Per-request timeout.")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--defs", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--detail", is_flag=True, help="Include all live data and Mode 06 results.")
def scan(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    odometer: float | None,
    timeout: float,
    output_format: str,
    out: Path | None,
    defs: Path | None,
    detail: bool,
) -> None:
    """Scan a vehicle and report on it.

    On a real car, confirm the bus is healthy before running this:

        sudo ip link set can0 type can bitrate 500000 listen-only on
        sudo ip link set up can0
        candump can0        # expect error-free traffic

    Then bring the interface up without listen-only and scan. Ignition ON, not
    accessory -- many modules stay asleep in accessory mode and will not answer.
    """
    database = _load_database(defs)
    try:
        with CanLink.open(transport, channel, bitrate=bitrate, extended=extended, fd=fd) as link:
            result = scan_vehicle(link, database, claimed_odometer_km=odometer, timeout=timeout)
    except TransportError as exc:
        raise click.ClickException(str(exc)) from exc

    evaluation = result.evaluate(database)
    _emit(result, evaluation, output_format, out, detail)


@cli.command()
@click.option(
    "--scenario",
    type=click.Choice(sorted(SCENARIOS)),
    default="recently-cleared",
    show_default=True,
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--defs", type=click.Path(file_okay=False, path_type=Path), default=None)
@click.option("--detail", is_flag=True, help="Include all live data and Mode 06 results.")
@click.option(
    "--odometer",
    type=float,
    default=None,
    help="Override the scenario's advertised mileage.",
)
def demo(
    scenario: str,
    output_format: str,
    out: Path | None,
    defs: Path | None,
    detail: bool,
    odometer: float | None,
) -> None:
    """Scan a simulated car. Needs no hardware, no privileges and no vehicle."""
    database = _load_database(defs)
    spec = get_scenario(scenario)
    claimed = odometer if odometer is not None else spec.claimed_odometer_km

    with (
        SimulatedVehicle.from_scenario(spec, channel=_SIM_CHANNEL),
        CanLink.open("virtual", _SIM_CHANNEL) as link,
    ):
        result = scan_vehicle(link, database, claimed_odometer_km=claimed)

    evaluation = result.evaluate(database)
    # stderr: which car was simulated is context for the operator, not part of the
    # report, and on stdout it would make `--format json` unparseable.
    click.echo(f"scenario: {spec.name} -- {spec.summary}", err=True)
    _emit(result, evaluation, output_format, out, detail)


@cli.command()
@click.option(
    "--scenario",
    type=click.Choice(sorted(SCENARIOS)),
    default="recently-cleared",
    show_default=True,
)
@click.option(
    "--transport",
    type=click.Choice(["udp", "virtual"]),
    default="udp",
    show_default=True,
    help="udp lets a scan in another terminal reach this simulator.",
)
@click.option("--channel", default=None, help="Bus channel to serve on.")
def sim(scenario: str, transport: str, channel: str | None) -> None:
    """Run a simulated car until interrupted, for use from another terminal.

    Then, elsewhere: carpi scan --transport udp
    """
    spec = get_scenario(scenario)
    kwargs: dict[str, object] = {"kind": transport}
    if channel:
        kwargs["channel"] = channel
    elif transport == "udp":
        kwargs["channel"] = "224.0.0.251:31000"

    vehicle = SimulatedVehicle.from_scenario(spec, **kwargs)
    modules = ", ".join(f"0x{e.spec.response_id:03X} {e.label}" for e in vehicle.ecus)
    click.echo(f"scenario: {spec.name} -- {spec.summary}", err=True)
    click.echo(f"serving {modules} on {transport}", err=True)
    click.echo("Ctrl-C to stop.", err=True)
    try:
        with vehicle:
            while True:
                # The simulator answers on its own thread; nothing to do here.
                import time

                time.sleep(0.5)
    except KeyboardInterrupt:
        click.echo("\nstopped", err=True)


@cli.command()
@click.option(
    "--transport",
    type=click.Choice(["socketcan", "virtual", "udp", "sim"]),
    default="socketcan",
    show_default=True,
    help="'sim' runs an in-process simulated car; the rest reach a real bus.",
)
@click.option("--channel", default=None, help="Interface name, e.g. can0.")
@click.option("--bitrate", default=DEFAULT_BITRATE, show_default=True)
@click.option("--extended/--standard", default=False, show_default=True)
@click.option("--fd", is_flag=True, help="CAN FD.")
@click.option(
    "--scenario",
    type=click.Choice(sorted(SCENARIOS)),
    default="recently-cleared",
    show_default=True,
    help="Which simulated car to serve, when --transport sim.",
)
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Bind address. Use 0.0.0.0 to accept connections from a phone.",
)
@click.option("--port", default=8080, show_default=True)
@click.option("--timeout", type=float, default=1.0, show_default=True)
@click.option("--defs", type=click.Path(file_okay=False, path_type=Path), default=None)
def serve(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    scenario: str,
    host: str,
    port: int,
    timeout: float,
    defs: Path | None,
) -> None:
    """Serve the web UI, for use from a phone.

    Defaults to 127.0.0.1 so an unconfigured run is not reachable from the network.
    The portable unit binds its hotspot interface instead -- see deploy/.

    The interface is used by one conversation at a time. A second inspection, or live
    values during an inspection, is refused rather than queued: two request/response
    conversations on one ISO-TP channel would each decode the other's replies.
    """
    import uvicorn

    from carpi.server import DirectProvider, SimulatedProvider, VehicleGateway, create_app

    database = _load_database(defs)
    provider = (
        SimulatedProvider(scenario)
        if transport == "sim"
        else DirectProvider(transport, channel, bitrate=bitrate, extended=extended, fd=fd)
    )
    gateway = VehicleGateway(provider, database, timeout=timeout)
    app = create_app(gateway)

    click.echo(f"interface: {provider.description}", err=True)
    if provider.is_simulated:
        click.echo("NOTE: serving a simulated vehicle, not a real one.", err=True)
    if host in ("127.0.0.1", "localhost"):
        click.echo(
            "listening on localhost only; pass --host 0.0.0.0 to reach it from a phone",
            err=True,
        )
    click.echo(f"UI: http://{host}:{port}/", err=True)

    uvicorn.run(app, host=host, port=port, log_level="info", ws="websockets")


@cli.command("scenarios")
def list_scenarios() -> None:
    """List the simulated vehicles available to 'carpi demo'."""
    for name in sorted(SCENARIOS):
        scenario = SCENARIOS[name]
        click.echo(f"{name}")
        click.echo(f"    {' '.join(scenario.summary.split())}")
        if scenario.expect_findings:
            click.echo(f"    expects: {', '.join(scenario.expect_findings)}")
        click.echo()


@cli.group()
def defs() -> None:
    """Inspect the definition database."""


@defs.command("check")
@click.option("--path", type=click.Path(file_okay=False, path_type=Path), default=None)
def defs_check(path: Path | None) -> None:
    """Validate every definition file against its schema."""
    root = path or defs_root()
    try:
        database = Database.load(path)
    except DefinitionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"{root}")
    click.echo(f"  {len(database.pids_by_number)} Mode 01 PIDs")
    click.echo(f"  {len(database.rules)} inspection rules")
    by_confidence: dict[str, int] = {}
    for pid in database.pids_by_number.values():
        by_confidence[pid.confidence] = by_confidence.get(pid.confidence, 0) + 1
    shown = ", ".join(f"{count} {level}" for level, count in sorted(by_confidence.items()))
    click.echo(f"  PID confidence: {shown}")
    click.echo("OK")


@defs.command("facts")
@click.option("--defs-path", type=click.Path(file_okay=False, path_type=Path), default=None)
def defs_facts(defs_path: Path | None) -> None:
    """List every fact the rules reference, for writing new rules against."""
    database = _load_database(defs_path)
    facts: dict[str, list[str]] = {}
    for rule in database.rules:
        for name in sorted(rule.required_facts):
            facts.setdefault(name, []).append(rule.id)
    click.echo(json.dumps(facts, indent=2, sort_keys=True))


def main() -> int:
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        return 1
    except click.Abort:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
