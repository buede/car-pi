"""``carpi bench`` -- exercise the stack over real CAN hardware, without a car.

The rest of car-pi's tests run on an in-process virtual bus. That covers every protocol
decision but nothing about real silicon: bit timing, frame pacing, inter-frame gaps, and
whether a transport's timing assumptions survive contact with a controller that takes
real microseconds to do things.

TP2.0 needs this more than anything else here. It is connection-oriented with negotiated
timing parameters and a keepalive, and both sides of car-pi's implementation were written
from the same specification by the same author -- so the existing tests prove the two
agree and nothing more. A bench run over two physical interfaces is the cheapest way to
find out whether the timing is right before leaning into a car with a laptop.

The setup
---------
Two CAN nodes wired together, each with a 120 ohm resistor across CAN_H and CAN_L at its
own end -- and *only* at the ends, which is the one place a terminator does belong. One
node runs the simulator, the other runs the client:

    carpi bench tp20 --responder can1 --tester can0

With a single interface, two sockets on the same bus still see each other's frames, so a
vcan interface works for exercising the command itself:

    sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0
    carpi bench tp20 --responder vcan0 --tester vcan0

That proves the plumbing but not the timing -- a virtual interface has no bit timing to
get wrong. Only two real controllers do that.
"""

from __future__ import annotations

import contextlib
import json
import statistics
import time
from collections.abc import Iterator
from dataclasses import dataclass, field

import can
import click

from carpi.core.database import Database
from carpi.core.protocol.kwp2000 import KwpClient
from carpi.core.protocol.obd2 import Obd2Client
from carpi.core.transport.base import NoResponse, TransportError
from carpi.core.transport.canbus import DEFAULT_BITRATE, CanLink, open_bus
from carpi.core.transport.tp20 import Tp20Error, open_tp20_channel

__all__ = ["bench"]


@dataclass
class Check:
    """One bench assertion, and how long it took."""

    name: str
    passed: bool
    detail: str = ""
    seconds: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "seconds": round(self.seconds, 4) if self.seconds is not None else None,
        }

    def __str__(self) -> str:
        mark = "ok  " if self.passed else "FAIL"
        timing = f"  ({self.seconds * 1000:.0f} ms)" if self.seconds else ""
        return f"  [{mark}] {self.name}{timing}" + (
            f"\n         {self.detail}" if self.detail else ""
        )


@dataclass
class BenchResult:
    """Everything a bench run established."""

    kind: str
    responder: str
    tester: str
    checks: list[Check] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def record(
        self, name: str, passed: bool, detail: str = "", seconds: float | None = None
    ) -> Check:
        check = Check(name=name, passed=passed, detail=detail, seconds=seconds)
        self.checks.append(check)
        return check

    def as_dict(self) -> dict[str, object]:
        summary: dict[str, object] = {
            "schema": "carpi.bench/1",
            "kind": self.kind,
            "responder_interface": self.responder,
            "tester_interface": self.tester,
            "passed": self.passed,
            "checks": [check.as_dict() for check in self.checks],
        }
        if self.latencies:
            # Timing is the whole reason to run on hardware, so it is reported rather
            # than merely used.
            summary["latency_ms"] = {
                "count": len(self.latencies),
                "min": round(min(self.latencies) * 1000, 2),
                "median": round(statistics.median(self.latencies) * 1000, 2),
                "max": round(max(self.latencies) * 1000, 2),
            }
        return summary


@contextlib.contextmanager
def _two_links(
    responder: str,
    tester: str,
    kind: str,
    bitrate: int,
) -> Iterator[tuple[can.BusABC, CanLink]]:
    """A raw bus for the simulator and a CanLink for the client.

    Separate sockets, so neither hears its own transmissions -- which is the topology of
    a real bus and the reason this works even when both names are the same interface.
    """
    sim_bus = open_bus(kind, responder, bitrate=bitrate)
    try:
        with CanLink.open(kind, tester, bitrate=bitrate) as link:
            yield sim_bus, link
    finally:
        sim_bus.shutdown()


_options = [
    click.option(
        "--responder",
        required=True,
        help="Interface the simulator serves on, e.g. can1 (or vcan0 for a plumbing check).",
    ),
    click.option("--tester", required=True, help="Interface the client scans from, e.g. can0."),
    click.option(
        "--kind",
        type=click.Choice(["socketcan", "virtual", "udp"]),
        default="socketcan",
        show_default=True,
    ),
    click.option("--bitrate", default=DEFAULT_BITRATE, show_default=True),
    click.option("--timeout", type=float, default=1.0, show_default=True),
    click.option("--format", "output_format", type=click.Choice(["text", "json"]), default="text"),
]


def _with_options(command):
    for option in reversed(_options):
        command = option(command)
    return command


@click.group()
def bench() -> None:
    """Exercise the stack over real CAN hardware, with no vehicle attached.

    Needs two CAN nodes wired together, each with a 120 ohm terminator at its own end.
    See docs/bring-up-a-new-board.md.

    This is what validates the parts the virtual-bus tests cannot reach: bit timing,
    frame pacing, and TP2.0's negotiated timing and keepalive.
    """


def _finish(result: BenchResult, output_format: str) -> None:
    if output_format == "json":
        click.echo(json.dumps(result.as_dict(), indent=2))
    else:
        for check in result.checks:
            click.echo(str(check))
        if result.latencies:
            data = result.as_dict()["latency_ms"]
            click.echo(
                f"\nrequest latency: min {data['min']} ms, median {data['median']} ms, "  # type: ignore[index]
                f"max {data['max']} ms"  # type: ignore[index]
            )
        click.echo("\nPASSED" if result.passed else "\nFAILED")
    if not result.passed:
        raise SystemExit(1)


@bench.command("obd")
@_with_options
def bench_obd(
    responder: str,
    tester: str,
    kind: str,
    bitrate: int,
    timeout: float,
    output_format: str,
) -> None:
    """Run the OBD-II simulator on one interface and scan from the other.

    Exercises ISO-TP segmentation over real silicon -- the VIN read in particular, since
    17 characters cannot fit one frame.
    """
    from carpi.sim import SimulatedVehicle, get_scenario

    database = Database.load()
    result = BenchResult(kind="obd", responder=responder, tester=tester)

    with _two_links(responder, tester, kind, bitrate) as (sim_bus, link):
        vehicle = SimulatedVehicle.from_scenario(get_scenario("failing-catalyst"), bus=sim_bus)
        vehicle.start()
        try:
            started = time.monotonic()
            addresses = link.discover_ecus(timeout=timeout)
            result.record(
                "ECU discovery over real CAN",
                bool(addresses),
                f"found {[a.label for a in addresses]}",
                time.monotonic() - started,
            )
            if not addresses:
                _finish(result, output_format)
                return

            client = Obd2Client(link.channel(addresses[0]), database, timeout=timeout)

            started = time.monotonic()
            rpm = client.read_pid("engine_rpm")
            elapsed = time.monotonic() - started
            result.latencies.append(elapsed)
            result.record(
                "single-frame read (engine RPM)",
                rpm.value == 760.0,
                f"got {rpm.value}",
                elapsed,
            )

            started = time.monotonic()
            vin = client.vin()
            elapsed = time.monotonic() - started
            result.record(
                "multi-frame ISO-TP read (VIN)",
                vin == "CARPI0SIMULATED01",
                f"got {vin!r}",
                elapsed,
            )

            # Repeated reads catch a desync that a single exchange would not.
            ok = True
            for _ in range(20):
                started = time.monotonic()
                try:
                    reading = client.read_pid("coolant_temp")
                except (NoResponse, TransportError) as exc:
                    ok = False
                    result.record("20 sequential reads stay in step", False, str(exc))
                    break
                result.latencies.append(time.monotonic() - started)
                if reading.value != 89.0:
                    ok = False
                    result.record(
                        "20 sequential reads stay in step",
                        False,
                        f"coolant read back as {reading.value}, expected 89.0 -- a "
                        f"desync would look exactly like this",
                    )
                    break
            else:
                result.record("20 sequential reads stay in step", True)
            del ok

            started = time.monotonic()
            codes = client.stored_dtcs()
            result.record(
                "fault code read",
                codes == ["P0420"],
                f"got {codes}",
                time.monotonic() - started,
            )
        finally:
            vehicle.stop()

    _finish(result, output_format)


@bench.command("tp20")
@_with_options
def bench_tp20(
    responder: str,
    tester: str,
    kind: str,
    bitrate: int,
    timeout: float,
    output_format: str,
) -> None:
    """Run the VAG simulator on one interface and drive TP2.0 from the other.

    The one bench that matters most. TP2.0 negotiates timing parameters and needs a
    keepalive, and neither of those can be wrong in a way an in-process virtual bus
    would notice.
    """
    from carpi.sim.tp20 import Tp20Responder
    from carpi.sim.vag import kwp2000_era_modules

    result = BenchResult(kind="tp20", responder=responder, tester=tester)

    with _two_links(responder, tester, kind, bitrate) as (sim_bus, link):
        vag = Tp20Responder(sim_bus, kwp2000_era_modules())
        vag.start()
        try:
            started = time.monotonic()
            try:
                channel = open_tp20_channel(link, 0x17, timeout=timeout)
            except (NoResponse, Tp20Error) as exc:
                result.record("TP2.0 channel setup to Instruments (0x17)", False, str(exc))
                _finish(result, output_format)
                return
            setup_seconds = time.monotonic() - started
            result.record(
                "TP2.0 channel setup to Instruments (0x17)",
                True,
                f"negotiated tx 0x{channel.address.tx_id:03X} rx 0x{channel.address.rx_id:03X}",
                setup_seconds,
            )

            try:
                client = KwpClient(channel, timeout=timeout)

                started = time.monotonic()
                opened = client.start_session()
                result.record(
                    "KWP2000 diagnostic session",
                    opened,
                    seconds=time.monotonic() - started,
                )

                # Identification is around 32 bytes, so it must segment. This is the
                # check most likely to expose a real timing or sequencing problem.
                started = time.monotonic()
                identity = client.identification()
                elapsed = time.monotonic() - started
                result.record(
                    "segmented reply reassembled (identification)",
                    "KOMBIINSTRUMENT" in (identity.get("text") or ""),
                    f"got {identity.get('text', '')[:48]!r}",
                    elapsed,
                )

                started = time.monotonic()
                block = client.read_measuring_block(1)
                elapsed = time.monotonic() - started
                result.latencies.append(elapsed)
                result.record(
                    "measuring block read",
                    len(block.values) == 4,
                    str(block),
                    elapsed,
                )

                ok = True
                for index in range(30):
                    started = time.monotonic()
                    try:
                        again = client.read_measuring_block(1)
                    except (NoResponse, TransportError) as exc:
                        result.record(
                            "30 sequential requests stay sequenced",
                            False,
                            f"failed at request {index}: {exc}",
                        )
                        ok = False
                        break
                    result.latencies.append(time.monotonic() - started)
                    if again.group != 1:
                        result.record(
                            "30 sequential requests stay sequenced",
                            False,
                            f"request {index} answered with group {again.group}; the "
                            f"sequence counter has drifted",
                        )
                        ok = False
                        break
                if ok:
                    result.record("30 sequential requests stay sequenced", True)

                # The point of the keepalive: a module drops a channel that goes quiet,
                # and the timeout is short. This is the check that a virtual bus cannot
                # meaningfully make, because nothing there ever times out.
                channel.start_keepalive(interval=0.4)
                time.sleep(2.0)
                started = time.monotonic()
                try:
                    after_idle = client.read_measuring_block(1)
                    held = after_idle.group == 1
                    detail = "channel survived 2 s idle with the keepalive running"
                except (NoResponse, TransportError) as exc:
                    held = False
                    detail = f"channel died during idle: {exc}"
                result.record(
                    "channel survives an idle period",
                    held,
                    detail,
                    time.monotonic() - started,
                )
            finally:
                channel.close()

            # A module that is not fitted must be silent, not hang.
            started = time.monotonic()
            silent = False
            try:
                stray = open_tp20_channel(link, 0x37, timeout=0.5)
                stray.close()
            except (NoResponse, Tp20Error):
                silent = True
            result.record(
                "absent module stays silent",
                silent,
                seconds=time.monotonic() - started,
            )
        finally:
            vag.stop()

    _finish(result, output_format)


@bench.command("loopback")
@click.option("--interface", required=True, help="A single CAN interface, e.g. can0.")
@click.option("--bitrate", default=DEFAULT_BITRATE, show_default=True)
@click.option("--frames", default=100, show_default=True)
def bench_loopback(interface: str, bitrate: int, frames: int) -> None:
    """Check one interface can transmit and receive, with no second node.

    The first thing to run on a newly built board. It needs the controller put into
    loopback mode first, which is a property of the link rather than of this tool:

        sudo ip link set {iface} down
        sudo ip link set {iface} type can bitrate 500000 loopback on
        sudo ip link set {iface} up

    A pass means the controller and the SPI wiring work. It says nothing about the
    transceiver or the bus wiring, because in loopback nothing reaches the pins -- so a
    board that passes this and fails `bench obd` points at the transceiver end.
    """
    bus = open_bus("socketcan", interface, bitrate=bitrate)
    try:
        received = 0
        latencies: list[float] = []
        for index in range(frames):
            started = time.monotonic()
            bus.send(
                can.Message(
                    arbitration_id=0x123,
                    data=bytes([index & 0xFF, 0xAA, 0x55, 0, 0, 0, 0, 0]),
                    is_extended_id=False,
                )
            )
            echo = bus.recv(timeout=0.2)
            if echo is not None and echo.arbitration_id == 0x123:
                received += 1
                latencies.append(time.monotonic() - started)
    finally:
        bus.shutdown()

    click.echo(f"sent {frames}, received {received}")
    if latencies:
        click.echo(
            f"round trip: min {min(latencies) * 1e3:.2f} ms, "
            f"median {statistics.median(latencies) * 1e3:.2f} ms, "
            f"max {max(latencies) * 1e3:.2f} ms"
        )
    if received == 0:
        raise click.ClickException(
            f"nothing came back. Either {interface} is not in loopback mode (see the help "
            f"for this command), or the controller is not working -- check the crystal "
            f"frequency in your dtoverlay matches the one actually fitted, which is the "
            f"single most common fault on a hand-built board."
        )
    if received < frames:
        raise click.ClickException(
            f"lost {frames - received} of {frames} frames. On a hand-built board this "
            f"usually means the SPI wiring is marginal or the interrupt pin is wrong."
        )
    click.echo("PASSED")
