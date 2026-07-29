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
from carpi.core.storage import write_private
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
        # A report contains the VIN, which identifies one car and its owner, so it is
        # written owner-only rather than with whatever the umask happens to be.
        write_private(out, text + "\n")
        click.echo(f"written to {out} (readable only by you)", err=True)
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
    # The unit is stated because a figure given in miles does not fail: it produces a
    # confident cross-check against the wrong number, on the one finding a buyer acts on.
    help="Advertised mileage in KILOMETRES, enabling the odometer cross-check.",
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
@click.option(
    "--discover",
    is_flag=True,
    help="Also sweep for modules outside the OBD-II range and identify them.",
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
    discover: bool,
) -> None:
    """Scan a vehicle and report on it.

    `carpi guide` does all of this with the checks performed for you. Use this when you
    already know the interface is up and the bus is healthy.

    On a real car, confirm that first:

        sudo ip link set can0 type can bitrate 500000 listen-only on
        sudo ip link set up can0
        candump can0        # expect error-free traffic

    Then bring the interface up without listen-only and scan. Ignition ON, not
    accessory -- many modules stay asleep in accessory mode and will not answer.

    Generic OBD-II reaches eight modules. `--discover` sweeps for the rest and reads each
    one's standardised identification, including its VIN -- so a module fitted from another
    car shows up without any definition file for the vehicle. It adds most of a minute.
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
                discover=discover,
                on_progress=lambda message: click.echo(f"  {message}", err=True),
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
    metavar="KM",
    help="Override the scenario's advertised mileage, in kilometres.",
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
    from carpi.cli.guide import guide
    from carpi.cli.vag import coding, vag

    cli.add_command(vag)
    cli.add_command(coding)
    cli.add_command(bench)
    # The guided menu drives the commands above rather than adding anything of its own,
    # so it is registered last, alongside them.
    cli.add_command(guide)


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


def _hex(value: str, what: str) -> int:
    """Parse a hex option, naming the option when it is wrong.

    Every arbitration ID and identifier on this command line is typed by hand from another
    command's output, so a transposed digit is the expected mistake rather than a rare one.
    Left unguarded, ``int(value, 16)`` reports it as a Python traceback, which tells the
    reader nothing about which of the four hex options they mistyped.
    """
    try:
        return int(value, 16)
    except (TypeError, ValueError):
        raise click.ClickException(
            f"{what} must be a hex number such as 0x714, not {value!r}."
        ) from None


def _address(request_id: str, response_id: str | None, *, extended: bool) -> Any:
    """Build an ECU address from what the user typed, accepting what discovery printed.

    ``carpi uds discover`` labels each module it finds as ``714/77E``, and the next command
    then wants those two halves as separate hex options. Copying them across by hand is
    where a digit gets transposed, so the label form is accepted directly.

    The response ID may also be omitted inside the OBD-II range, where ISO 15765-4 fixes
    it at the request ID plus eight. Outside that range there is no convention to rely on,
    so it is required rather than guessed -- a wrong response ID does not fail cleanly, it
    listens to the wrong module.
    """
    from carpi.core.transport.base import (
        RESPONSE_BASE_11BIT,
        EcuAddress,
    )

    if "/" in request_id and response_id is None:
        left, _, right = request_id.partition("/")
        return EcuAddress(
            tx_id=_hex(left, "--request-id"),
            rx_id=_hex(right, "--request-id"),
            extended=extended,
        )

    tx_id = _hex(request_id, "--request-id")
    if response_id is not None:
        return EcuAddress(tx_id=tx_id, rx_id=_hex(response_id, "--response-id"), extended=extended)

    # 0x7E0-0x7E7 request, 0x7E8-0x7EF response. The only pairing any standard promises.
    if not extended and RESPONSE_BASE_11BIT - 8 <= tx_id <= RESPONSE_BASE_11BIT - 1:
        return EcuAddress(tx_id=tx_id, rx_id=tx_id + 8, extended=extended)

    raise click.ClickException(
        f"--response-id is required for 0x{tx_id:X}. Only the OBD-II range 0x7E0-0x7E7 has "
        f"a standard reply address. Use the pair that 'uds discover' printed, either as "
        f"--request-id 0x{tx_id:X} --response-id 0xNNN or as --request-id {tx_id:03X}/NNN."
    )


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
@click.option(
    "--probe",
    "probe_name",
    type=click.Choice(["tester-present", "read-vin"]),
    default="tester-present",
    show_default=True,
    help="tester-present is the most inert. read-vin finds modules that ignore it.",
)
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
    probe_name: str,
    yes: bool,
) -> None:
    """Find every module that answers, including outside the OBD-II range.

    The odometer, and most of what is worth knowing about a used car, lives in modules
    generic OBD-II never speaks to. There is no standard map of their addresses, so they
    are found by probing.

    By default each probe is a TesterPresent request -- the most inert message in UDS,
    which cannot change anything. Do it with the vehicle stationary anyway.

    Some modules answer ReadDataByIdentifier while ignoring TesterPresent outside a
    session, so a module missing from one sweep may still appear in a `--probe read-vin`
    one. Both requests are reads.
    """
    from carpi.core.discovery import PROBES, observe_traffic, sweep_addresses

    first, last = _hex(low, "--low"), _hex(high, "--high")
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
            probe=PROBES[probe_name],
            request_delay=delay,
            on_progress=lambda message: click.echo(f"  {message}", err=True),
        )

    click.echo(
        json.dumps(
            {
                "schema": "carpi.discovery/1",
                "probed": stats.probed,
                "probe": probe_name,
                "elapsed_seconds": round(stats.elapsed, 2),
                "modules": [module.as_dict() for module in stats.modules],
            },
            indent=2,
        )
    )


@uds.command("read")
@_with_bus_options
@click.option(
    "--request-id",
    required=True,
    help="Address to send to, e.g. 0x714, or the 714/77E pair discover printed.",
)
@click.option(
    "--response-id",
    default=None,
    help="Address it replies on, e.g. 0x77E. Optional inside the OBD-II range.",
)
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
    response_id: str | None,
    dids: tuple[str, ...],
    session: str,
) -> None:
    """Read specific data identifiers from one module."""
    from carpi.core.didscan import STATUS_DATA, DidObservation
    from carpi.core.protocol.uds import DiagnosticSession, UdsClient

    address = _address(request_id, response_id, extended=extended)
    wanted = [_hex(value, "--did") for value in dids]

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
@click.option("--response-id", default=None)
def uds_identify(
    transport: str,
    channel: str | None,
    bitrate: int,
    extended: bool,
    fd: bool,
    timeout: float,
    scenario: str,
    request_id: str,
    response_id: str | None,
) -> None:
    """Read a module's ISO 14229 identification block.

    Standardised, so it needs no manufacturer definition: VIN, serial number, part and
    software numbers, and the programming and calibration dates. A module reprogrammed
    recently on a high-mileage car is worth asking about.
    """
    from carpi.core.protocol.uds import UdsClient

    address = _address(request_id, response_id, extended=extended)
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
@click.option("--response-id", default=None)
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
    response_id: str | None,
    mask: str,
) -> None:
    """Read manufacturer fault codes from one module.

    These are invisible to generic OBD-II, which only ever sees the emissions-related
    subset from the powertrain modules.
    """
    from carpi.core.protocol.uds import UdsClient

    address = _address(request_id, response_id, extended=extended)
    with _open_link(
        transport, channel, bitrate=bitrate, extended=extended, fd=fd, scenario=scenario
    ) as link:
        client = UdsClient(link.channel(address), timeout=timeout)
        client.start_session()
        codes = client.read_dtcs(_hex(mask, "--mask"))

    if not codes:
        click.echo("no fault codes matched")
        return
    for code in codes:
        click.echo(str(code))


@uds.command("scan-dids")
@_with_bus_options
@click.option("--request-id", required=True)
@click.option("--response-id", default=None)
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
    response_id: str | None,
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

    try:
        wanted = parse_ranges(ranges) if ranges else INTERESTING_RANGES
    except ValueError as exc:
        raise click.ClickException(
            f"--ranges must look like '0x2200-0x22ff,0xf190': {exc}"
        ) from None
    address = _address(request_id, response_id, extended=extended)

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
        # Owner-only: an un-anonymised sweep carries the VIN, and every payload the
        # module returned.
        write_private(out, json.dumps(document, indent=2) + "\n")
        click.echo(f"written to {out} (readable only by you)", err=True)
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


def _load_sweep(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"could not read {path}: {exc}") from None


@defs.command("compare")
@click.argument("before", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("after", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--expect-delta",
    type=float,
    default=None,
    metavar="AMOUNT",
    help="How much the thing you changed moved by, e.g. 1.2 for 1.2 km driven.",
)
@click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text")
@click.option("--top", type=int, default=20, show_default=True, help="How many to show.")
def defs_compare(
    before: Path, after: Path, expect_delta: float | None, output_format: str, top: int
) -> None:
    """Find which identifiers changed between two sweeps of the same module.

    The way to identify an unknown identifier is to sweep, change one thing about the car,
    sweep again, and see what moved by the right amount. This does the comparing.

        carpi uds scan-dids --request-id 714/77E --out before.json
        # drive 1.2 km
        carpi uds scan-dids --request-id 714/77E --out after.json
        carpi defs compare before.json after.json --expect-delta 1.2

    You should see a ranked list, best candidate first, with the units each identifier
    would have to be counting in for it to be the one you are looking for.

    These are candidates, not answers. Confirming one still needs a second car of the same
    platform whose true state you know independently -- one car can agree with a wrong
    guess by coincidence.
    """
    from carpi.core.candidates import DraftError, compare_sweeps

    try:
        candidates = compare_sweeps(_load_sweep(before), _load_sweep(after), expected=expect_delta)
    except DraftError as exc:
        raise click.ClickException(str(exc)) from None

    shown = candidates[: max(top, 1)]
    if output_format == "json":
        click.echo(
            json.dumps(
                {
                    "schema": "carpi.candidates/1",
                    "before": str(before),
                    "after": str(after),
                    "expected_delta": expect_delta,
                    "changed": len(candidates),
                    "candidates": [candidate.as_dict() for candidate in shown],
                },
                indent=2,
            )
        )
        return

    if not candidates:
        click.echo("No identifier changed between the two sweeps.")
        click.echo("Either nothing you changed is recorded here, or the change was too small.")
        return

    click.echo(f"{len(candidates)} identifier(s) changed. Best candidates first:")
    click.echo()
    for candidate in shown:
        line = f"  {candidate.label}  {candidate.before} -> {candidate.after}"
        line += f"  delta {candidate.delta:+d}"
        if candidate.familiar_scale is not None:
            line += f"   <- counts {candidate.familiar_scale:g} per unit"
        elif candidate.implied_scale is not None:
            line += f"   ({candidate.implied_scale:.4g} per count)"
        click.echo(line)
    click.echo()
    click.echo("These are candidates, not conclusions. Confirm against a second car of the")
    click.echo("same platform before treating any of them as identified.")


@defs.command("draft")
@click.argument("sweeps", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("--id", "profile_id", required=True, help="Profile id, e.g. vw-mqb.")
@click.option("--make", required=True, help="Manufacturer, e.g. Volkswagen.")
@click.option("--platform", required=True, help="Platform or model, e.g. MQB.")
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None)
def defs_draft(
    sweeps: tuple[Path, ...], profile_id: str, make: str, platform: str, out: Path | None
) -> None:
    """Turn module sweeps into a starting-point vehicle definition file.

        carpi defs draft cluster.json --id vw-mqb --make Volkswagen --platform MQB

    You should see YAML listing every identifier the sweep found, each marked TODO.

    Every read is emitted at 'community' confidence with a TODO name, because a sweep
    proves an identifier exists and nothing whatsoever about what it holds. Naming one
    'odometer_km' is a claim, and only somebody with the car can make it.

    This writes to where you tell it and never into the shipped database. An unverified
    entry there is worse than a missing one: a wrong odometer identifier returns plausible
    bytes that decode to a plausible mileage.
    """
    from carpi.core.candidates import DraftError, draft_profile, dump_yaml

    try:
        document = draft_profile(
            [_load_sweep(path) for path in sweeps],
            profile_id=profile_id,
            make=make,
            platform=platform,
        )
    except DraftError as exc:
        raise click.ClickException(str(exc)) from None

    text = dump_yaml(document)
    if out is not None:
        out.write_text(text, encoding="utf-8")
        click.echo(f"written to {out}", err=True)
        click.echo(
            "Every read is a TODO until you have proven it against the car. See "
            "docs/contribute-vehicle-data.md.",
            err=True,
        )
    else:
        click.echo(text)


@defs.command("contribute")
@click.argument("files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path))
@click.option("-o", "--out", type=click.Path(dir_okay=False, path_type=Path), default=None)
@click.option("--yes", is_flag=True, help="Skip the licence confirmation.")
def defs_contribute(files: tuple[Path, ...], out: Path | None, yes: bool) -> None:
    """Turn saved scans and sweeps into something you can offer to the project.

        carpi scan --channel can0 --discover --format json -o car.json
        carpi defs contribute car.json

    You should see a file naming which identifiers exist, and a link that opens a prefilled
    issue. Nothing is uploaded by this command.

    The file carries no values, no serial numbers and no VIN -- only the platform prefix of
    the VIN, which modules answered, and which identifiers exist with their length and type.
    Values are dropped rather than scrubbed: removing a VIN from a sweep still leaves the
    part numbers and programming dates, and those together identify one physical car.

    Sharing is a licence grant, so it asks first.
    """
    from carpi.core.candidates import DraftError, LeakedValue, issue_url, observe

    documents = [_load_sweep(path) for path in files]
    try:
        observation = observe(documents)
    except DraftError as exc:
        raise click.ClickException(str(exc)) from None
    except LeakedValue as exc:  # pragma: no cover - a bug if it ever fires
        raise click.ClickException(str(exc)) from None

    counted = sum(len(module["identifiers"]) for module in observation["modules"])
    prefix = observation["vin_prefix"] or "unknown"

    click.echo(f"Platform: {prefix}", err=True)
    click.echo(
        f"{len(observation['modules'])} module(s), {counted} identifier(s) that exist.",
        err=True,
    )
    click.echo("No values, no serial numbers and no VIN are included.", err=True)
    click.echo(err=True)

    target = out or Path(f"contribution-{prefix.lower()}.json")
    if not yes:
        click.echo(
            "Sharing this contributes it to the definition database under CC-BY-SA-4.0,\n"
            "which cannot be withdrawn later. If the car is not yours, ask the owner first.",
            err=True,
        )
        if not click.confirm(f"Write {target}?", default=True, err=True):
            return

    target.write_text(json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    click.echo(f"written to {target}", err=True)
    click.echo("Nothing has been sent.", err=True)
    click.echo(err=True)
    click.echo("Read the file, then open this to offer it:", err=True)
    click.echo(issue_url(observation))


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
