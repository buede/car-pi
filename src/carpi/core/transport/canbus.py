"""CAN bus setup and ISO-TP channels, over python-can.

Three bus kinds, chosen by name:

``socketcan``
    A real vehicle, on Linux. The interface must already be configured and up.
``virtual``
    python-can's in-process virtual bus. Buses sharing a channel name inside one
    process see each other's traffic, which is what the simulator and the tests use.
    Works on any OS, needs no hardware, and needs no privileges.
``udp``
    A virtual bus over UDP multicast, so a simulator in one terminal and a scan in
    another can talk. Also works on any OS.

Everything above this module sees only :class:`~carpi.core.transport.base.Channel`,
so swapping a simulator for a real car changes one command-line flag.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any

import can
import isotp

from carpi.core.transport.base import (
    EcuAddress,
    NoResponse,
    TransportError,
)

__all__ = ["BUS_KINDS", "CanLink", "IsoTpChannel", "open_bus"]

log = logging.getLogger(__name__)

BUS_KINDS = ("socketcan", "virtual", "udp")

DEFAULT_BITRATE = 500_000

# Default channel names per bus kind, so the common case needs no --channel.
_DEFAULT_CHANNELS = {
    "socketcan": "can0",
    "virtual": "carpi",
    # python-can's udp_multicast default group. IPv4 multicast keeps this working on
    # hosts where IPv6 multicast is not routed.
    "udp": "224.0.0.251:31000",
}

# ISO-TP tuning. Requests are padded to a full 8 bytes because a good number of ECUs
# quietly ignore short frames, and padding costs nothing on a diagnostic bus.
_ISOTP_PARAMS: dict[str, Any] = {
    "blocksize": 8,
    "stmin": 0,
    "tx_padding": 0x00,
    "tx_data_length": 8,
    "rx_flowcontrol_timeout": 1000,
    "rx_consecutive_frame_timeout": 1000,
    "max_frame_size": 4095,
    "can_fd": False,
    "blocking_send": False,
}


def open_bus(
    kind: str = "virtual",
    channel: str | None = None,
    *,
    bitrate: int = DEFAULT_BITRATE,
    fd: bool = False,
) -> can.BusABC:
    """Open a python-can bus of the given *kind*."""
    if kind not in BUS_KINDS:
        raise TransportError(f"unknown bus kind {kind!r}; expected one of {', '.join(BUS_KINDS)}")

    name = channel or _DEFAULT_CHANNELS[kind]
    try:
        if kind == "socketcan":
            # Bitrate is a property of the configured link, not of this handle. It is
            # accepted here only so the value can be reported alongside a scan.
            return can.interface.Bus(interface="socketcan", channel=name, fd=fd)
        if kind == "virtual":
            return can.interface.Bus(interface="virtual", channel=name)
        return can.interface.Bus(interface="udp_multicast", channel=name, fd=fd)
    except OSError as exc:
        if kind == "socketcan":
            raise TransportError(
                f"could not open SocketCAN interface {name!r}: {exc}. "
                f"Bring it up first, e.g. "
                f"'sudo ip link set {name} type can bitrate {bitrate} && "
                f"sudo ip link set up {name}'."
            ) from exc
        raise TransportError(f"could not open {kind} bus {name!r}: {exc}") from exc


class IsoTpChannel:
    """A framed request/response channel to one ECU."""

    def __init__(self, stack: isotp.TransportLayer, address: EcuAddress) -> None:
        self._stack = stack
        self._address = address

    @property
    def address(self) -> EcuAddress:
        return self._address

    def request(self, payload: bytes, timeout: float = 1.0) -> bytes:
        """Send *payload*, return the reply.

        Any reply still queued from a previous exchange is discarded first. Without
        that, one timeout would desynchronise every later request on this channel and
        the scan would report each PID's value against the wrong PID.
        """
        self._stack.clear_rx_queue()
        self._stack.send(bytes(payload))
        reply = self._stack.recv(block=True, timeout=timeout)
        if reply is None:
            raise NoResponse(
                f"{self._address} did not answer {payload.hex(' ')} within {timeout:g}s"
            )
        return bytes(reply)

    def close(self) -> None:
        self._stack.stop()


class CanLink:
    """Owns a bus and its ISO-TP stacks, and finds out which ECUs are present.

    Use as a context manager so the bus, notifier and stacks are always torn down --
    a leaked SocketCAN handle survives the process and blocks the next run.
    """

    def __init__(
        self,
        bus: can.BusABC,
        *,
        extended: bool = False,
        owns_bus: bool = True,
        fd: bool = False,
    ) -> None:
        self._bus = bus
        self._extended = extended
        self._owns_bus = owns_bus
        self._fd = fd
        # One notifier feeds every ISO-TP stack plus the discovery reader. Separate
        # readers polling the same bus would steal each other's frames.
        self._notifier = can.Notifier(bus, listeners=[], timeout=0.05)
        self._channels: dict[int, IsoTpChannel] = {}
        self._closed = False

    @classmethod
    def open(
        cls,
        kind: str = "virtual",
        channel: str | None = None,
        *,
        bitrate: int = DEFAULT_BITRATE,
        extended: bool = False,
        fd: bool = False,
    ) -> CanLink:
        """Open a bus of *kind* and wrap it."""
        bus = open_bus(kind, channel, bitrate=bitrate, fd=fd)
        return cls(bus, extended=extended, owns_bus=True, fd=fd)

    @property
    def bus(self) -> can.BusABC:
        return self._bus

    @property
    def extended(self) -> bool:
        """True if this link uses 29-bit addressing."""
        return self._extended

    def channel(self, address: EcuAddress) -> IsoTpChannel:
        """Get (creating if needed) the ISO-TP channel to *address*."""
        self._check_open()
        existing = self._channels.get(address.rx_id)
        if existing is not None:
            return existing

        mode = (
            isotp.AddressingMode.Normal_29bits
            if address.extended
            else isotp.AddressingMode.Normal_11bits
        )
        params = dict(_ISOTP_PARAMS, can_fd=self._fd)
        stack = isotp.NotifierBasedCanStack(
            bus=self._bus,
            notifier=self._notifier,
            address=isotp.Address(mode, txid=address.tx_id, rxid=address.rx_id),
            params=params,
        )
        stack.start()
        created = IsoTpChannel(stack, address)
        self._channels[address.rx_id] = created
        return created

    def discover_ecus(self, timeout: float = 1.0) -> list[EcuAddress]:
        """Broadcast one request and report every ECU that answers.

        Sends Mode 01 PID 00 -- "which PIDs do you support" -- to the functional
        address, because every OBD-II compliant ECU must answer it. Raw frames are
        collected rather than framed replies, since several ECUs respond at once and
        each reply belongs to a different conversation.
        """
        self._check_open()
        functional = EcuAddress.functional(extended=self._extended)
        reader = can.BufferedReader()
        self._notifier.add_listener(reader)
        try:
            # Hand-built single frame: PCI byte 0x02 (single frame, 2 data bytes),
            # then service 01, PID 00, padded out to 8.
            frame = can.Message(
                arbitration_id=functional.tx_id,
                data=bytes([0x02, 0x01, 0x00, 0, 0, 0, 0, 0]),
                is_extended_id=self._extended,
                is_fd=self._fd,
            )
            try:
                self._bus.send(frame)
            except can.CanError as exc:
                raise TransportError(f"could not send discovery frame: {exc}") from exc

            found: dict[int, EcuAddress] = {}
            deadline_reader_timeout = timeout
            while True:
                message = reader.get_message(timeout=deadline_reader_timeout)
                if message is None:
                    break
                # After the first answer, stop waiting the full timeout for stragglers;
                # ECUs on the same bus reply within milliseconds of each other.
                deadline_reader_timeout = 0.2 if found else timeout
                if message.is_extended_id != self._extended:
                    continue
                if message.arbitration_id in found:
                    continue
                try:
                    address = EcuAddress.from_response_id(
                        message.arbitration_id, extended=self._extended
                    )
                except ValueError:
                    # Ordinary powertrain broadcast traffic, not a diagnostic reply.
                    continue
                found[message.arbitration_id] = address
            return [found[key] for key in sorted(found)]
        finally:
            self._notifier.remove_listener(reader)
            reader.stop()

    @contextmanager
    def raw_reader(self) -> Iterator[can.BufferedReader]:
        """Observe every frame on the bus, without transmitting anything.

        Used by address discovery, which has to watch for a reply on an arbitration ID
        it does not know in advance, and by passive traffic mapping -- which is the
        safest possible first contact with an unfamiliar vehicle, since it sends nothing.
        """
        self._check_open()
        reader = can.BufferedReader()
        self._notifier.add_listener(reader)
        try:
            yield reader
        finally:
            self._notifier.remove_listener(reader)
            reader.stop()

    def send_raw(self, arbitration_id: int, data: bytes) -> None:
        """Send a single unsegmented frame.

        Deliberately low-level: address discovery needs to probe an ID before it knows
        whether anything is there, and an ISO-TP stack per candidate address would cost
        far more than the one frame it takes to find out.
        """
        self._check_open()
        payload = bytes(data)
        if len(payload) > 8:
            raise TransportError(
                f"{len(payload)} bytes will not fit one frame; use a channel for "
                f"anything that needs segmenting"
            )
        # Padded to a full 8 bytes: a good number of ECUs quietly ignore short frames.
        frame = can.Message(
            arbitration_id=arbitration_id,
            data=payload.ljust(8, b"\x00"),
            is_extended_id=self._extended,
            is_fd=self._fd,
        )
        try:
            self._bus.send(frame)
        except can.CanError as exc:
            raise TransportError(f"could not send to 0x{arbitration_id:X}: {exc}") from exc

    def _check_open(self) -> None:
        if self._closed:
            raise TransportError("this CanLink has already been closed")

    def close(self) -> None:
        """Tear down stacks, notifier and (if owned) the bus. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        for channel in self._channels.values():
            try:
                channel.close()
            except Exception:  # noqa: BLE001 - teardown must not mask the real error
                log.debug("error stopping ISO-TP stack for %s", channel.address, exc_info=True)
        self._channels.clear()
        try:
            self._notifier.stop()
        except Exception:  # noqa: BLE001
            log.debug("error stopping notifier", exc_info=True)
        if self._owns_bus:
            try:
                self._bus.shutdown()
            except Exception:  # noqa: BLE001
                log.debug("error shutting down bus", exc_info=True)

    def __enter__(self) -> CanLink:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
