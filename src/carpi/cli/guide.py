"""``carpi guide`` -- the menu that walks somebody through an inspection.

Split out of ``main.py`` because it is a different kind of thing from the commands it
drives. Every other command does one job and takes flags; this one asks questions, and
the questions encode a procedure that otherwise lives in prose the reader has to follow
by hand.

**It adds no capability.** Everything here is reachable with the individual commands, and
those keep working exactly as before. What this removes is the requirement to know that
``ip link`` must be run before ``carpi scan``, that ``candump`` is how you check a bus,
and that an arbitration ID is written in hex.

Two rules shape the design:

*Every step prints the command it is about to run.* A menu that hides what it does leaves
the user no better off the second time, and gives them nothing to paste into a bug report.
Printing the equivalent command means the guide teaches the command line instead of
replacing it, and it keeps the guide honest -- it cannot quietly do something the printed
command would not.

*Nothing here writes to a vehicle.* Coding has its own deliberately awkward path, where
the user types a module's name back before anything is written. Putting an irreversible
write behind a menu, two keystrokes from a list of options, is the exact opposite of what
that awkwardness is for.
"""

from __future__ import annotations

import contextlib
import shutil
import subprocess
from pathlib import Path

import click

from carpi.core.database import Database
from carpi.core.storage import write_private
from carpi.core.transport.base import TransportError
from carpi.core.transport.canbus import DEFAULT_BITRATE, CanLink

# Bitrates worth offering, commonest first. Almost every car since about 2008 is the
# first one; the second is mostly older or non-powertrain buses.
_BITRATES = (500000, 250000)

_PREFLIGHT_SECONDS = 3.0


def _say(message: str = "") -> None:
    """Write to stderr, so a guided session never contaminates piped report output."""
    click.echo(message, err=True)


def _heading(step: int, total: int, title: str) -> None:
    _say()
    _say(f"Step {step} of {total} -- {title}")
    _say("-" * (len(title) + 18))


def _shows(command: str) -> None:
    """Print the equivalent command, so the guide teaches rather than hides."""
    _say(f"  Equivalent command:  {command}")


def _can_interfaces() -> list[str]:
    """CAN interfaces the kernel currently knows about.

    Asking somebody to type ``can0`` assumes they know that is what it is called, and
    that it exists. Reading it from the system removes a question with a wrong answer.
    """
    sysfs = Path("/sys/class/net")
    if not sysfs.is_dir():
        return []
    names = []
    for entry in sorted(sysfs.iterdir()):
        # A CAN interface has a bittiming directory; ordinary ethernet does not. This is
        # cheaper and more reliable than parsing `ip -details link show`.
        if (entry / "can_bittiming").exists() or entry.name.startswith(("can", "vcan")):
            names.append(entry.name)
    return names


def _run(command: list[str]) -> tuple[bool, str]:
    """Run a system command, returning success and whatever it said."""
    try:
        finished = subprocess.run(command, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = (finished.stderr or finished.stdout or "").strip()
    return finished.returncode == 0, output


def _bring_up(interface: str, bitrate: int) -> bool:
    """Set the interface's bitrate and bring it up, reporting what it ran.

    This exists because ``carpi scan --bitrate`` cannot do it. That flag is reported in
    the scan metadata and nothing more, so somebody who passes it to fix a silent bus has
    changed nothing and has no way to tell. Here the bitrate is actually applied.
    """
    steps = [
        ["sudo", "ip", "link", "set", interface, "down"],
        ["sudo", "ip", "link", "set", interface, "type", "can", "bitrate", str(bitrate)],
        ["sudo", "ip", "link", "set", interface, "up"],
    ]
    for step in steps:
        _shows(" ".join(step))
        ok, output = _run(step)
        # Taking an already-down interface down fails harmlessly, so only the bitrate and
        # the bring-up are treated as fatal.
        if not ok and "set" in step and step[-1] != "down":
            _say(f"  failed: {output}")
            return False
    _say(f"  {interface} is up at {bitrate} bit/s.")
    return True


def _default_scan_path(vin: str | None) -> Path:
    """Somewhere a report can be kept without ending up in a repository.

    Named after the platform rather than the whole VIN, so a directory listing or a shell
    history does not identify the car on its own.
    """
    platform = (vin or "unknown")[:8].lower() or "unknown"
    return Path.home() / ".carpi" / "scans" / f"carpi-scan-{platform}.json"


def _acknowledge_ownership() -> bool:
    """State what this is and is not, and ask once.

    The project's position is that somebody who owns a car may examine it. That position
    is only worth anything if the tool says so where it is used, rather than in a document
    the reader never opens. It also protects the person on the other side of a sale, which
    is what makes the position defensible rather than merely convenient.
    """
    _say()
    _say("Before starting")
    _say("---------------")
    _say("  This tool reads. It cannot write to a vehicle, clear a fault code, or")
    _say("  change a setting -- those services are not implemented at all.")
    _say()
    _say("  Ask the owner before plugging into a car that is not yours. Reads are")
    _say("  non-invasive, but it is still someone else's property.")
    _say()
    _say("  car-pi is pre-alpha, and its vehicle database is nearly empty on purpose.")
    _say("  A question the car does not answer is reported as 'not assessed', never as")
    _say("  a pass. Absence of a finding is not evidence of a sound car.")
    _say()
    return click.confirm("  Do you own this car, or have the owner's permission?", err=True)


def _choose_interface(total: int) -> str | None:
    _heading(1, total, "find the interface")
    interfaces = _can_interfaces()

    if not interfaces:
        _say("  No CAN interface exists on this machine.")
        _say()
        _say("  On a Raspberry Pi with the hardware fitted, set it up first:")
        _shows("sudo ./deploy/setup-can.sh --dry-run")
        _say()
        _say("  To see car-pi work with no hardware at all, choose 'try it with no car'")
        _say("  from the menu instead.")
        return None

    if len(interfaces) == 1:
        _say(f"  Found one: {interfaces[0]}")
        return interfaces[0]

    _say(f"  Found {len(interfaces)}: {', '.join(interfaces)}")
    return click.prompt(
        "  Which one is connected to the car",
        type=click.Choice(interfaces),
        default=interfaces[0],
        err=True,
    )


def _preflight(interface: str, total: int) -> bool:
    """Listen to the bus, and explain whatever came back. Transmits nothing."""
    from carpi.core.discovery import check_bus

    _heading(2, total, "check the bus, without transmitting")
    _say("  This listens only. It physically cannot disturb the car.")

    for attempt, bitrate in enumerate(_BITRATES):
        if attempt == 0:
            if not click.confirm(
                f"  Bring {interface} up at {bitrate} bit/s?", default=True, err=True
            ):
                return False
        else:
            _say()
            _say(f"  Nothing heard at {_BITRATES[attempt - 1]}. Some buses run slower.")
            if not click.confirm(f"  Try {bitrate} bit/s?", default=True, err=True):
                return False

        if not _bring_up(interface, bitrate):
            _say()
            _say("  Could not configure the interface. If this machine is not the Pi, or")
            _say("  you are not root, run the printed commands yourself and start again.")
            return False

        _say()
        _shows(f"candump {interface}    # the manual equivalent of the next few seconds")
        try:
            with CanLink.open("socketcan", interface, bitrate=bitrate) as link:
                health = check_bus(link, _PREFLIGHT_SECONDS, on_progress=lambda m: _say(f"  {m}"))
        except TransportError as exc:
            _say(f"  {exc}")
            return False

        _say(f"  {health.summary}")
        if health.healthy:
            _say("  The bus is alive and error-free. Safe to continue.")
            return True

        _say()
        for line in health.advice:
            _say(f"  - {line}")

        # An error-frame flood is not a bitrate problem, so retrying at another rate only
        # wastes the user's time and leaves the real cause in place.
        if health.verdict == "errors":
            return False

    _say()
    _say("  Nothing heard at any bitrate. Do not scan yet -- a scan against a silent bus")
    _say("  produces a report saying the car answered nothing, which reads far too much")
    _say("  like a clean car. Work through the causes above first.")
    return False


def _inspect(database: Database, defs: Path | None) -> None:
    """The procedure from docs/inspect-a-car.md, in order, with the checks performed."""
    total = 4
    if not _acknowledge_ownership():
        _say()
        _say("  Stopping. Ask first -- the conversation goes better when the report is")
        _say("  something you offer to share.")
        return

    interface = _choose_interface(total)
    if interface is None:
        return
    if not _preflight(interface, total):
        return

    _heading(3, total, "the advertised mileage")
    _say("  Optional. Given it, the report cross-checks what the seller claims against")
    _say("  what the engine controller actually holds.")
    _say()
    odometer = click.prompt(
        "  Advertised mileage in KILOMETRES (blank to skip)",
        default="",
        show_default=False,
        err=True,
    ).strip()
    claimed: float | None = None
    if odometer:
        try:
            claimed = float(odometer.replace(",", "").replace(" ", ""))
        except ValueError:
            _say(f"  {odometer!r} is not a number, so the cross-check is skipped.")
    if claimed is not None:
        _say(f"  Cross-checking against {claimed:,.0f} km.")

    _heading(4, total, "scan")
    _say("  Ignition ON, not accessory. Engine off. This takes a few minutes.")
    _say()
    command = f"carpi scan --channel {interface}"
    if claimed is not None:
        command += f" --odometer {claimed:.0f}"
    _shows(command)
    if not click.confirm("  Start the scan?", default=True, err=True):
        return

    from carpi.core.scan import scan_vehicle
    from carpi.report.text import render_json, render_text

    try:
        with CanLink.open("socketcan", interface) as link:
            result = scan_vehicle(
                link,
                database,
                claimed_odometer_km=claimed,
                on_progress=lambda message: _say(f"  {message}"),
            )
    except TransportError as exc:
        raise click.ClickException(str(exc)) from exc

    evaluation = result.evaluate(database)
    _say()
    click.echo(render_text(result, evaluation))

    _say()
    if click.confirm("  Keep a copy of the full data?", default=True, err=True):
        # Defaulted to an absolute path under the user's home, not a bare name in the
        # working directory. A bare name lands wherever the terminal happens to be, which
        # for anybody working on car-pi is a git checkout -- and then a real car's VIN is
        # one `git add -A` away from being published.
        suggested = _default_scan_path(result.vin)
        target = Path(
            click.prompt("  File name", default=str(suggested), show_default=True, err=True)
        ).expanduser()
        write_private(target, render_json(result, evaluation) + "\n")
        _say(f"  Written to {target}, readable only by you.")
        _say("  It contains the VIN, which identifies the car and its owner. To share what")
        _say("  you learned without sharing the car:")
        _shows(f"carpi defs contribute {target}")
    _ = defs


def _hardware() -> None:
    """Wrap the bench commands, which prove a board before a car depends on it."""
    _say()
    _say("Check the interface hardware")
    _say("---------------------------")
    interfaces = _can_interfaces()
    if not interfaces:
        _say("  No CAN interface exists on this machine, so there is nothing to test.")
        return

    interface = (
        interfaces[0]
        if len(interfaces) == 1
        else click.prompt(
            "  Which interface", type=click.Choice(interfaces), default=interfaces[0], err=True
        )
    )
    _say()
    _say("  A loopback test needs no second node and no car. It proves the controller")
    _say("  and its wiring work. It says nothing about the transceiver, because in")
    _say("  loopback no signal reaches its pins.")
    _say()
    _say("  The controller has to be put into loopback mode first. That is a property of")
    _say("  the interface, not of car-pi, so it happens with 'ip link'.")
    _say()
    if not click.confirm(
        f"  Put {interface} into loopback mode and test it?", default=True, err=True
    ):
        return

    steps = [
        ["sudo", "ip", "link", "set", interface, "down"],
        [
            "sudo",
            "ip",
            "link",
            "set",
            interface,
            "type",
            "can",
            "bitrate",
            str(DEFAULT_BITRATE),
            "loopback",
            "on",
        ],
        ["sudo", "ip", "link", "set", interface, "up"],
    ]
    for step in steps:
        _shows(" ".join(step))
        ok, output = _run(step)
        if not ok and step[-1] != "down":
            _say(f"  failed: {output}")
            return

    _say()
    _shows(f"carpi bench loopback --interface {interface}")

    from carpi.cli.bench import bench_loopback

    ctx = click.get_current_context()
    # bench commands exit non-zero on a failing board, which is a result here rather than
    # an error in the guide -- the printed report has already said what failed and why.
    with contextlib.suppress(SystemExit):
        ctx.invoke(bench_loopback, interface=interface, bitrate=DEFAULT_BITRATE, frames=10)

    _say()
    _say("  Loopback mode is still set. Turn it off before touching a car:")
    _shows(f"sudo ip link set {interface} down")
    _shows(f"sudo ip link set {interface} type can bitrate {DEFAULT_BITRATE} loopback off")


def _no_car(database: Database) -> None:
    """Scan a simulated car, so the whole flow can be learned before a car is involved."""
    from carpi.sim import SCENARIOS, get_scenario

    _say()
    _say("Try it with no car")
    _say("------------------")
    _say("  These are simulated vehicles. Nothing is plugged in, and nothing is real.")
    _say()
    for name, spec in sorted(SCENARIOS.items()):
        _say(f"  {name:<20} {spec.summary}")
    _say()
    scenario = click.prompt(
        "  Which car",
        type=click.Choice(sorted(SCENARIOS)),
        default="recently-cleared",
        err=True,
    )
    _shows(f"carpi demo --scenario {scenario}")

    from carpi.core.scan import scan_vehicle
    from carpi.report.text import render_text
    from carpi.sim import SimulatedVehicle

    spec = get_scenario(scenario)
    profile = database.profile(spec.profile) if spec.profile else None
    with (
        SimulatedVehicle.from_scenario(spec, channel="carpi-guide"),
        CanLink.open("virtual", "carpi-guide") as link,
    ):
        result = scan_vehicle(
            link,
            database,
            claimed_odometer_km=spec.claimed_odometer_km,
            profile=profile,
            on_progress=lambda message: _say(f"  {message}"),
        )
    _say()
    click.echo(render_text(result, result.evaluate(database)))


_ACTIONS = {
    "inspect a car": "Walk through an inspection at the car, checking each step.",
    "try it with no car": "Scan a simulated vehicle. Nothing plugged in.",
    "check my hardware": "Prove a CAN interface works before a car depends on it.",
    "quit": "",
}


@click.command()
@click.option("--defs", type=click.Path(file_okay=False, path_type=Path), default=None)
def guide(defs: Path | None) -> None:
    """Walk through an inspection step by step, with no flags to remember.

    Everything this does is available as an individual command, and each step prints the
    command it is running so they can be used directly next time. Nothing here writes to
    a vehicle.
    """
    from carpi.core.database import DefinitionError

    try:
        database = Database.load(defs)
    except DefinitionError as exc:
        raise click.ClickException(f"definition database is invalid: {exc}") from exc

    _say()
    _say("car-pi")
    _say("======")
    for name, description in _ACTIONS.items():
        if description:
            _say(f"  {name:<20} {description}")

    _say()
    choice = click.prompt(
        "What would you like to do",
        type=click.Choice(list(_ACTIONS)),
        default="inspect a car",
        err=True,
    )

    if choice == "inspect a car":
        if not shutil.which("ip"):
            _say()
            _say("  This machine has no 'ip' command, so it is not a Linux host with")
            _say("  SocketCAN. A real inspection has to run on the Pi.")
            _say("  Choose 'try it with no car' to see the whole flow here instead.")
            return
        _inspect(database, defs)
    elif choice == "try it with no car":
        _no_car(database)
    elif choice == "check my hardware":
        _hardware()
