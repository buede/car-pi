"""The ``carpi`` command.

``carpi demo`` is the zero-setup path: it runs a simulated car in-process and scans
it, so the whole stack can be exercised on any machine with nothing plugged in.
``carpi scan`` points the same code at a real vehicle.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

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


def _resolve_profile(database: Database, profile_id: str | None):
    """Pick a vehicle profile explicitly, or defer to VIN matching during the scan.

    Named profiles are looked up now so a typo fails before the vehicle is touched
    rather than halfway through a scan.
    """
    if profile_id is None:
        return None
    try:
        return database.profile(profile_id)
    except DefinitionError as exc:
        raise click.ClickException(str(exc)) from exc


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
@click.option(
    "--profile",
    "profile_id",
    default=None,
    help="Vehicle profile for manufacturer reads. Omit to select one by VIN.",
)
@click.option(
    "--no-profile",
    is_flag=True,
    help="Skip manufacturer reads entirely; generic OBD-II only.",
)
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
    profile_id: str | None,
    no_profile: bool,
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
    profile = None if no_profile else _resolve_profile(database, profile_id)
    try:
        with CanLink.open(transport, channel, bitrate=bitrate, extended=extended, fd=fd) as link:
            result = scan_vehicle(
                link,
                database,
                claimed_odometer_km=odometer,
                timeout=timeout,
                profile=profile,
            )
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

    # A scenario names the profile that describes it, so the demo exercises the
    # manufacturer-specific path too rather than only the generic layer.
    profile = database.profile(spec.profile) if spec.profile else None

    with (
        SimulatedVehicle.from_scenario(spec, channel=_SIM_CHANNEL),
        CanLink.open("virtual", _SIM_CHANNEL) as link,
    ):
        result = scan_vehicle(link, database, claimed_odometer_km=claimed, profile=profile)

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
    The portable unit binds its hotspot interface instead -- see
    docs/build-the-field-unit.md.

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


# Registered here rather than defined in this module: the coding commands are the only
# part of car-pi that can change a car, and they live where that is unmissable.
def _register_vag_commands() -> None:
    from carpi.cli.bench import bench
    from carpi.cli.vag import coding, vag

    cli.add_command(vag)
    cli.add_command(coding)
    cli.add_command(bench)


@cli.group()
def uds() -> None:
    """Manufacturer-specific diagnostics over UDS (ISO 14229). Read-only.

    Generic OBD-II reaches eight modules, chosen by emissions regulators. UDS reaches
    every module the manufacturer's own tool does -- the instrument cluster holding the
    odometer, the ABS controller, the body electronics.

    Nothing in this command group can modify a vehicle. The services that could
    (WriteDataByIdentifier, RoutineControl, SecurityAccess, ECUReset, the transfer
    services) are not implemented, and a test asserts none reaches the bus.
    """


def _open_link(
    transport: str,
    channel: str | None,
    *,
    bitrate: int,
    extended: bool,
    fd: bool,
    scenario: str | None = None,
) -> Any:
    """Context manager yielding a CanLink, simulating a car if asked to."""
    from contextlib import ExitStack, contextmanager

    from carpi.core.transport.canbus import CanLink

    @contextmanager
    def opener():
        with ExitStack() as stack:
            if transport == "sim":
                from carpi.sim import SimulatedVehicle, get_scenario

                spec = get_scenario(scenario or "cluster-tampered")
                sim_channel = "carpi-uds-cli"
                stack.enter_context(SimulatedVehicle.from_scenario(spec, channel=sim_channel))
                yield stack.enter_context(CanLink.open("virtual", sim_channel))
            else:
                yield stack.enter_context(
                    CanLink.open(transport, channel, bitrate=bitrate, extended=extended, fd=fd)
                )

    return opener()


_transport_option = click.option(
    "--transport",
    type=click.Choice(["socketcan", "virtual", "udp", "sim"]),
    default="socketcan",
    show_default=True,
    help="'sim' talks to an in-process simulated car.",
)
_channel_option = click.option("--channel", default=None, help="Interface name, e.g. can0.")
_bus_options = [
    click.option("--bitrate", default=DEFAULT_BITRATE, show_default=True),
    click.option("--extended/--standard", default=False, show_default=True),
    click.option("--fd", is_flag=True, help="CAN FD."),
    click.option("--timeout", type=float, default=1.0, show_default=True),
    click.option(
        "--scenario",
        type=click.Choice(sorted(SCENARIOS)),
        default="cluster-tampered",
        show_default=True,
        help="Which simulated car, when --transport sim.",
    ),
]


def _with_bus_options(command: Any) -> Any:
    for option in reversed(_bus_options):
        command = option(command)
    return _channel_option(_transport_option(command))


@uds.command("discover")
@_with_bus_options
@click.option("--low", default="0x700", show_default=True, help="First address to probe.")
@click.option("--high", default="0x7FF", show_default=True, help="Last address to probe.")
@click.option(
    "--observe",
    type=float,
    default=3.0,
    show_default=True,
    help="Seconds to watch the bus before transmitting. 0 to skip.",
)
@click.option("--delay", type=float, default=0.02, show_default=True, help="Between probes.")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def uds_discover(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    low: str,
    high: str,
    observe: float,
    delay: float,
    yes: bool,
) -> None:
    """Find every module that answers, including outside the OBD-II range.

    The odometer, and most of what is worth knowing about a used car, lives in modules
    generic OBD-II never speaks to. There is no standard map of their addresses, so they
    are found by probing.

    Each probe is a TesterPresent request -- the most inert message in UDS, which cannot
    change anything. Do it with the vehicle stationary anyway.
    """
    from carpi.core.discovery import observe_traffic, sweep_addresses

    first, last = int(low, 16), int(high, 16)
    is_real = transport == "socketcan"

    if is_real and not yes:
        click.echo(
            f"About to probe {last - first + 1} addresses on a real vehicle.\n"
            f"Every request is read-only and cannot change anything, but the vehicle "
            f"should be stationary.",
            err=True,
        )
        click.confirm("Continue?", abort=True, err=True)

    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        if observe > 0:
            # Entirely passive, and the fastest way to find out the interface is not
            # actually connected to a live bus.
            click.echo(f"watching the bus for {observe:g}s (sending nothing)...", err=True)
            traffic = observe_traffic(link, observe)
            if traffic:
                click.echo(f"  {len(traffic)} arbitration IDs broadcasting", err=True)
            else:
                click.echo(
                    "  nothing heard. On a real car that means the interface is not on a "
                    "live bus: check the bitrate, the wiring, and that the ignition is ON.",
                    err=True,
                )

        stats = sweep_addresses(
            link,
            low=first,
            high=last,
            request_delay=delay,
            on_progress=lambda message: click.echo(f"  {message}", err=True),
        )

    click.echo(
        json.dumps(
            {
                "schema": "carpi.discovery/1",
                "probed": stats.probed,
                "elapsed_seconds": round(stats.elapsed, 2),
                "modules": [module.as_dict() for module in stats.modules],
            },
            indent=2,
        )
    )


@uds.command("read")
@_with_bus_options
@click.option("--request-id", required=True, help="Address to send to, e.g. 0x714.")
@click.option("--response-id", required=True, help="Address it replies on, e.g. 0x77E.")
@click.option("--did", "dids", multiple=True, required=True, help="Identifier, e.g. 0xF190.")
@click.option(
    "--session",
    type=click.Choice(["default", "extended"]),
    default="extended",
    show_default=True,
)
def uds_read(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    request_id: str,
    response_id: str,
    dids: tuple[str, ...],
    session: str,
) -> None:
    """Read specific data identifiers from one module."""
    from carpi.core.didscan import STATUS_DATA, DidObservation
    from carpi.core.protocol.uds import DiagnosticSession, UdsClient
    from carpi.core.transport.base import EcuAddress

    address = EcuAddress(tx_id=int(request_id, 16), rx_id=int(response_id, 16), extended=extended)
    wanted = [int(value, 16) for value in dids]

    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        client = UdsClient(link.channel(address), timeout=timeout)
        client.start_session(
            DiagnosticSession.DEFAULT if session == "default" else DiagnosticSession.EXTENDED
        )
        found = client.read_dids(wanted)

    for did in wanted:
        raw = found.get(did)
        if raw is None:
            click.echo(f"0x{did:04X}: no answer")
            continue
        printable = bool(raw) and all(0x20 <= byte <= 0x7E for byte in raw)
        text = raw.decode("ascii", "ignore").strip() if printable else None
        click.echo(str(DidObservation(did=did, status=STATUS_DATA, raw=raw, text=text)))


@uds.command("identify")
@_with_bus_options
@click.option("--request-id", required=True)
@click.option("--response-id", required=True)
def uds_identify(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    request_id: str,
    response_id: str,
) -> None:
    """Read a module's ISO 14229 identification block.

    Standardised, so it needs no manufacturer definition: VIN, serial number, part and
    software numbers, and the programming and calibration dates. A module reprogrammed
    recently on a high-mileage car is worth asking about.
    """
    from carpi.core.protocol.uds import UdsClient
    from carpi.core.transport.base import EcuAddress

    address = EcuAddress(tx_id=int(request_id, 16), rx_id=int(response_id, 16), extended=extended)
    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        client = UdsClient(link.channel(address), timeout=timeout)
        client.start_session()
        identity = client.identification()

    if not identity:
        raise click.ClickException(f"{address} returned no identification data")
    click.echo(json.dumps(identity, indent=2))


@uds.command("dtcs")
@_with_bus_options
@click.option("--request-id", required=True)
@click.option("--response-id", required=True)
@click.option(
    "--mask",
    default="0xFF",
    show_default=True,
    help="Status mask. 0x08 is confirmed faults only.",
)
def uds_dtcs(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    request_id: str,
    response_id: str,
    mask: str,
) -> None:
    """Read manufacturer fault codes from one module.

    These are invisible to generic OBD-II, which only ever sees the emissions-related
    subset from the powertrain modules.
    """
    from carpi.core.protocol.uds import UdsClient
    from carpi.core.transport.base import EcuAddress

    address = EcuAddress(tx_id=int(request_id, 16), rx_id=int(response_id, 16), extended=extended)
    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        client = UdsClient(link.channel(address), timeout=timeout)
        client.start_session()
        codes = client.read_dtcs(int(mask, 16))

    if not codes:
        click.echo("no fault codes matched")
        return
    for code in codes:
        click.echo(str(code))


@uds.command("scan-dids")
@_with_bus_options
@click.option("--request-id", required=True)
@click.option("--response-id", required=True)
@click.option(
    "--ranges",
    default=None,
    help="Identifier ranges, e.g. '0x2200-0x22ff,0xf190'. Defaults to a first-pass set.",
)
@click.option("--delay", type=float, default=0.01, show_default=True)
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option(
    "--anonymise",
    is_flag=True,
    help="Remove the VIN and redact any value containing it, for sharing publicly.",
)
def uds_scan_dids(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    request_id: str,
    response_id: str,
    ranges: str | None,
    delay: float,
    out: Path | None,
    anonymise: bool,
) -> None:
    """Sweep a module's data identifiers to find out what it holds.

    This is how the definition database grows. The three outcomes all matter: data,
    'protected' (the identifier exists and is locked -- often the interesting ones), and
    nothing there.

    Read-only throughout. A full 16-bit sweep is 65,536 requests and takes a long time;
    start with a narrow range. Some manufacturers log that a tester talked to a module.
    """
    from carpi.core.didscan import INTERESTING_RANGES, parse_ranges, scan_dids
    from carpi.core.protocol.uds import UdsClient
    from carpi.core.transport.base import EcuAddress

    wanted = parse_ranges(ranges) if ranges else INTERESTING_RANGES
    address = EcuAddress(tx_id=int(request_id, 16), rx_id=int(response_id, 16), extended=extended)

    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        client = UdsClient(link.channel(address), timeout=timeout)
        client.start_session()
        vin = None
        # Recorded so the report can be anonymised later. A module need not implement it.
        with contextlib.suppress(Exception):
            vin = client.read_did(0xF190).decode("ascii", errors="ignore").strip("\x00 ")
        report = scan_dids(
            client,
            wanted,
            delay=delay,
            vin=vin or None,
            on_progress=lambda message: click.echo(f"  {message}", err=True),
        )

    document = report.as_dict(anonymise=anonymise)
    if out is not None:
        out.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        click.echo(f"written to {out}", err=True)
        if report.vin and not anonymise:
            # Said plainly, because a scan posted to a public issue tracker identifies
            # one physical car and, through it, a person.
            click.echo(
                f"NOTE: {out} contains the VIN ({report.vin}). Re-run with --anonymise "
                f"before sharing it publicly.",
                err=True,
            )
    else:
        click.echo(json.dumps(document, indent=2))


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
    _register_vag_commands()
    try:
        cli.main(standalone_mode=False)
    except click.ClickException as exc:
        click.echo(f"error: {exc.format_message()}", err=True)
        return 1
    except click.Abort:
        return 130
    return 0


_register_vag_commands()


if __name__ == "__main__":
    sys.exit(main())
