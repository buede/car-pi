"""``carpi vag`` and ``carpi coding`` -- the KWP2000-era VAG commands.

Split into its own module rather than added to ``main.py`` for the same reason
:mod:`carpi.coding` is a separate package: the coding commands are the only part of
car-pi that can change a car, and keeping them visibly apart makes that fact hard to
lose track of.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from pathlib import Path

import click

from carpi.core.protocol.kwp2000 import KwpClient, KwpError, KwpNegativeResponse
from carpi.core.transport.base import NoResponse
from carpi.core.transport.canbus import DEFAULT_BITRATE, CanLink
from carpi.core.transport.tp20 import (
    SAFETY_CRITICAL_MODULES,
    VAG_MODULES,
    Tp20Error,
    open_tp20_channel,
)

_SIM_CHANNEL = "carpi-vag-cli"


def _parse_module(value: str) -> int:
    """Accept 0x17, 17 (hex, as VCDS writes it), or a name fragment."""
    text = value.strip()
    with contextlib.suppress(ValueError):
        return int(text, 16)
    matches = [address for address, name in VAG_MODULES.items() if text.lower() in name.lower()]
    if len(matches) == 1:
        return matches[0]
    if matches:
        names = ", ".join(f"{a:02X} {VAG_MODULES[a]}" for a in sorted(matches))
        raise click.ClickException(f"{value!r} matches several modules: {names}")
    raise click.ClickException(f"{value!r} is not a module address or a known module name")


@contextlib.contextmanager
def _link(transport: str, channel: str | None, bitrate: int, fd: bool) -> Iterator[CanLink]:
    if transport == "sim":
        import can

        from carpi.sim.tp20 import Tp20Responder
        from carpi.sim.vag import kwp2000_era_modules

        bus = can.interface.Bus(interface="virtual", channel=_SIM_CHANNEL)
        responder = Tp20Responder(bus, kwp2000_era_modules())
        responder.start()
        try:
            with CanLink.open("virtual", _SIM_CHANNEL) as link:
                yield link
        finally:
            responder.stop()
            bus.shutdown()
    else:
        with CanLink.open(transport, channel, bitrate=bitrate, fd=fd) as link:
            yield link


_bus_options = [
    click.option(
        "--transport",
        type=click.Choice(["socketcan", "virtual", "udp", "sim"]),
        default="socketcan",
        show_default=True,
    ),
    click.option("--channel", default=None, help="Interface name, e.g. can0."),
    click.option("--bitrate", default=DEFAULT_BITRATE, show_default=True),
    click.option("--fd", is_flag=True),
    click.option("--timeout", type=float, default=1.5, show_default=True),
]


def _with_bus(command):
    for option in reversed(_bus_options):
        command = option(command)
    return command


@click.group()
def vag() -> None:
    """VAG diagnostics over KWP2000 on TP2.0. Read-only.

    For VAG vehicles of roughly 2001-2010. These cars carry
    manufacturer diagnostics over VW's own TP2.0 transport, not ISO-TP, so the UDS
    commands will not find their modules -- generic OBD-II still works, but it only ever
    sees the emissions modules.

    NOT YET VERIFIED ON A REAL VEHICLE. The transport was implemented from published
    documentation and tested against a simulator built from the same documentation.
    """


@vag.command("modules")
@_with_bus
@click.option(
    "--addresses",
    default=None,
    help="Comma-separated addresses to try. Defaults to the well-known VAG map.",
)
def vag_modules(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    addresses: str | None,
) -> None:
    """Find which modules this car has, by trying to open a channel to each.

    The equivalent of VCDS's auto-scan. Read-only: opening a channel and asking whether
    anybody is there cannot change anything.
    """
    wanted = [int(part, 16) for part in addresses.split(",")] if addresses else sorted(VAG_MODULES)

    found: list[dict[str, object]] = []
    with _link(transport, channel, bitrate, fd) as link:
        for address in wanted:
            name = VAG_MODULES.get(address, f"0x{address:02X}")
            try:
                tp_channel = open_tp20_channel(link, address, timeout=timeout)
            except (NoResponse, Tp20Error):
                continue
            try:
                client = KwpClient(tp_channel, timeout=timeout)
                client.start_session()
                identity = client.identification()
                codes = client.read_dtcs()
                entry = {
                    "address": f"0x{address:02X}",
                    "name": name,
                    "identification": identity.get("text"),
                    "fault_codes": codes,
                    "safety_critical": address in SAFETY_CRITICAL_MODULES,
                }
                found.append(entry)
                flag = "  [safety-critical]" if entry["safety_critical"] else ""
                click.echo(
                    f"  {address:02X}  {name:<28} {len(codes)} fault code(s){flag}", err=True
                )
            finally:
                tp_channel.close()

    click.echo(json.dumps({"schema": "carpi.vagscan/1", "modules": found}, indent=2))


@vag.command("blocks")
@_with_bus
@click.option("--module", "module_ref", required=True, help="Address (0x17) or name.")
@click.option("--group", "groups", multiple=True, help="Group number. Repeatable.")
@click.option("--range", "group_range", default=None, help="e.g. 1-20")
def vag_blocks(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    module_ref: str,
    groups: tuple[str, ...],
    group_range: str | None,
) -> None:
    """Read measuring blocks -- the live values VCDS shows.

    What each field means is module-specific and not something car-pi knows. Values whose
    scaling formula is not recognised are shown as raw bytes rather than guessed at.
    """
    address = _parse_module(module_ref)
    wanted: list[int] = [int(value) for value in groups]
    if group_range:
        start, _, end = group_range.partition("-")
        wanted.extend(range(int(start), int(end or start) + 1))
    if not wanted:
        wanted = list(range(1, 11))

    with _link(transport, channel, bitrate, fd) as link:
        tp_channel = open_tp20_channel(link, address, timeout=timeout)
        try:
            tp_channel.start_keepalive()
            client = KwpClient(tp_channel, timeout=timeout)
            client.start_session()
            blocks = client.read_measuring_blocks(wanted)
        finally:
            tp_channel.close()

    if not blocks:
        raise click.ClickException(
            f"{VAG_MODULES.get(address, hex(address))} returned no measuring blocks"
        )
    for group in sorted(blocks):
        click.echo(str(blocks[group]))


@vag.command("read")
@_with_bus
@click.option("--module", "module_ref", required=True)
@click.option("--identifier", required=True, help="Local identifier, e.g. 0x22.")
def vag_read(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    module_ref: str,
    identifier: str,
) -> None:
    """Read one local identifier's raw bytes from a module."""
    address = _parse_module(module_ref)
    local_id = int(identifier, 16)

    with _link(transport, channel, bitrate, fd) as link:
        tp_channel = open_tp20_channel(link, address, timeout=timeout)
        try:
            client = KwpClient(tp_channel, timeout=timeout)
            client.start_session()
            try:
                raw = client.read_local_identifier(local_id)
            except KwpNegativeResponse as exc:
                if exc.is_protected:
                    raise click.ClickException(
                        f"identifier 0x{local_id:02X} exists but is locked behind a login "
                        f"({exc}). That is a positive finding: the module holds something "
                        f"there."
                    ) from exc
                raise click.ClickException(str(exc)) from exc
            except KwpError as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            tp_channel.close()

    printable = raw and all(0x20 <= byte <= 0x7E for byte in raw)
    click.echo(
        json.dumps(
            {
                "module": f"0x{address:02X}",
                "identifier": f"0x{local_id:02X}",
                "raw": raw.hex(),
                "length": len(raw),
                "unsigned": int.from_bytes(raw, "big") if 0 < len(raw) <= 8 else None,
                "text": raw.decode("ascii") if printable else None,
            },
            indent=2,
        )
    )


# --- coding ---------------------------------------------------------------------


@click.group()
def coding() -> None:
    """Change a module's configuration. THIS WRITES TO YOUR CAR.

    The only part of car-pi that can modify a vehicle. Everything else is structurally
    incapable of it.

    Coding is feasible on this era because a login is a five-digit code rather than a
    cryptographic exchange. Feasible is not the same as safe:

      * Airbag, ABS, steering, immobiliser and parking-brake modules are refused, and
        there is no flag to override that.
      * The current value is archived to disk before any write. If it cannot be archived,
        the write does not happen.
      * `plan` shows a decoded before-and-after and changes nothing. `apply` requires you
        to type the module's name.
      * Supply voltage and vehicle speed are checked first.

    Not exposed over the web interface, and it will not be: that server has no
    authentication.
    """


def _coding_session(link, address: int, timeout: float):
    from carpi.coding import CodingSession

    tp_channel = open_tp20_channel(link, address, timeout=timeout)
    tp_channel.start_keepalive()
    return tp_channel, CodingSession(tp_channel, timeout=timeout)


@coding.command("plan")
@_with_bus
@click.option("--module", "module_ref", required=True)
@click.option("--identifier", default="0x00", show_default=True, help="Usually 0x00 for coding.")
@click.option("--value", required=True, help="New value in hex, e.g. 0A1B2C.")
@click.option("--login", type=int, default=None, help="Login code, if the read needs one.")
@click.option("--voltage", type=float, default=None, help="Supply voltage, if known.")
def coding_plan(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    module_ref: str,
    identifier: str,
    value: str,
    login: int | None,
    voltage: float | None,
) -> None:
    """Check a change and show what it would do. Writes nothing."""
    from carpi.coding import CodingRefused, build_plan

    address = _parse_module(module_ref)
    try:
        intended = bytes.fromhex(value.replace(" ", ""))
    except ValueError as exc:
        raise click.ClickException(f"--value must be hex bytes: {exc}") from exc

    with _link(transport, channel, bitrate, fd) as link:
        tp_channel, session = _coding_session(link, address, timeout)
        try:
            if login is not None:
                session.login(login)
            try:
                plan = build_plan(
                    session,
                    module_address=address,
                    identifier=int(identifier, 16),
                    intended=intended,
                    login_code=login,
                    voltage=voltage,
                )
            except CodingRefused as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            tp_channel.close()

    click.echo(f"module:     {plan.module_name} (0x{plan.module_address:02X})")
    click.echo(f"identifier: 0x{plan.identifier:02X}")
    for line in plan.diff_lines():
        click.echo(line)
    if plan.is_noop:
        click.echo("\nThe module already holds this value. Nothing to do.")
        return
    for warning in plan.warnings:
        click.echo(f"warning: {warning}", err=True)
    click.echo(
        f'\nTo apply, re-run with "coding apply" and confirm by typing: '
        f"{plan.confirmation_phrase!r}"
    )


@coding.command("apply")
@_with_bus
@click.option("--module", "module_ref", required=True)
@click.option("--identifier", default="0x00", show_default=True)
@click.option("--value", required=True, help="New value in hex.")
@click.option("--login", type=int, required=True, help="Login code for this module.")
@click.option("--voltage", type=float, default=None)
@click.option(
    "--confirm",
    default=None,
    help="The module's name. Omit to be prompted.",
)
@click.option(
    "--restore-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where to archive the previous value.",
)
def coding_apply(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    module_ref: str,
    identifier: str,
    value: str,
    login: int,
    voltage: float | None,
    confirm: str | None,
    restore_dir: Path | None,
) -> None:
    """Write the change. Archives the previous value first."""
    from carpi.coding import CodingRefused, apply_plan, build_plan

    address = _parse_module(module_ref)
    try:
        intended = bytes.fromhex(value.replace(" ", ""))
    except ValueError as exc:
        raise click.ClickException(f"--value must be hex bytes: {exc}") from exc

    with _link(transport, channel, bitrate, fd) as link:
        tp_channel, session = _coding_session(link, address, timeout)
        try:
            session.login(login)
            try:
                plan = build_plan(
                    session,
                    module_address=address,
                    identifier=int(identifier, 16),
                    intended=intended,
                    login_code=login,
                    voltage=voltage,
                )
            except CodingRefused as exc:
                raise click.ClickException(str(exc)) from exc

            click.echo(f"module:     {plan.module_name} (0x{plan.module_address:02X})", err=True)
            for line in plan.diff_lines():
                click.echo(line, err=True)
            for warning in plan.warnings:
                click.echo(f"warning: {warning}", err=True)

            phrase = confirm
            if phrase is None:
                click.echo("", err=True)
                phrase = click.prompt(
                    f"Type the module name to confirm ({plan.confirmation_phrase!r})",
                    default="",
                    show_default=False,
                    err=True,
                )

            try:
                path, took = apply_plan(
                    session, plan, confirmation=phrase or "", restore_dir=restore_dir
                )
            except CodingRefused as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            tp_channel.close()

    click.echo(f"previous value archived to {path}", err=True)
    if took:
        click.echo("written and verified by reading back")
    else:
        click.echo(
            "the module accepted the write but reads back differently. Restore with:\n"
            f"  carpi coding restore --file {path}",
            err=True,
        )
        raise SystemExit(1)


@coding.command("restore")
@_with_bus
@click.option(
    "--file",
    "restore_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option("--login", type=int, default=None, help="Overrides the code in the file.")
@click.option("--confirm", default=None)
def coding_restore(
    transport: str,
    channel: str | None,
    bitrate: int,
    fd: bool,
    timeout: float,
    restore_file: Path,
    login: int | None,
    confirm: str | None,
) -> None:
    """Put a module back to an archived value."""
    from carpi.coding import CodingRefused, apply_plan, build_plan, load_restore_point

    point = load_restore_point(restore_file)
    code = login if login is not None else point.login_code
    if code is None:
        raise click.ClickException("the restore point records no login code; pass --login")

    with _link(transport, channel, bitrate, fd) as link:
        tp_channel, session = _coding_session(link, point.module_address, timeout)
        try:
            session.login(code)
            try:
                plan = build_plan(
                    session,
                    module_address=point.module_address,
                    identifier=point.identifier,
                    intended=point.previous_bytes,
                    login_code=code,
                )
                if plan.is_noop:
                    click.echo("already at the archived value; nothing to do")
                    return
                for line in plan.diff_lines():
                    click.echo(line, err=True)
                phrase = confirm
                if phrase is None:
                    phrase = click.prompt(
                        f"Type the module name to confirm ({plan.confirmation_phrase!r})",
                        default="",
                        show_default=False,
                        err=True,
                    )
                path, took = apply_plan(session, plan, confirmation=phrase or "")
            except CodingRefused as exc:
                raise click.ClickException(str(exc)) from exc
        finally:
            tp_channel.close()

    click.echo(f"restored; the pre-restore value is archived at {path}", err=True)
    if not took:
        raise SystemExit(1)


@coding.command("list-restore-points")
@click.option(
    "--dir",
    "directory",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
)
def coding_list(directory: Path | None) -> None:
    """List archived values, newest first."""
    from carpi.coding.plan import restore_directory

    target = directory or restore_directory()
    if not target.is_dir():
        click.echo(f"no restore points under {target}")
        return
    files = sorted(target.glob("*.json"), reverse=True)
    if not files:
        click.echo(f"no restore points under {target}")
        return
    for path in files:
        with contextlib.suppress(Exception):
            document = json.loads(path.read_text(encoding="utf-8"))
            click.echo(
                f"{document['created_at']}  {document['module_name']:<28} "
                f"{document['identifier']}  {document['previous']} -> {document['intended']}"
            )
            click.echo(f"    {path}")
