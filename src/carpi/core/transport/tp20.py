"""Volkswagen Transport Protocol 2.0.

VAG cars from roughly 2001 to 2010 carry manufacturer diagnostics over KWP2000 on CAN,
but not over ISO-TP. They use TP2.0, VW's own transport,
and the two are not interchangeable. Generic OBD-II on the same car still uses ISO-TP,
because EOBD mandates it, which is why a cheap scan tool works while telling you almost
nothing: the odometer, the cluster and the comfort modules are all behind TP2.0.

How it works
------------
TP2.0 is connection-oriented, which ISO-TP is not. A conversation has three phases:

1. **Channel setup.** The tester sends to ``0x200`` naming the module's *logical address*
   (``0x01`` engine, ``0x17`` instruments, and so on -- the same numbers VCDS shows). The
   module answers on ``0x200 + its address`` with the pair of CAN IDs to use from then on.
   So the IDs are negotiated per session rather than fixed, and a sweep of arbitration
   IDs will not find these modules at all.
2. **Parameter negotiation.** Block size and four timing parameters, ``0xA0``/``0xA1``.
3. **Data.** Segmented, sequence-numbered, and acknowledged, with a keepalive that has to
   keep flowing or the module drops the channel.

The framing below follows Jared Wiltshire's protocol description at
https://jazdw.net/tp20, which is the reference open documentation for TP2.0.

**Unverified against a real vehicle.** This was written from that specification, and the
simulator implements the same specification independently -- so the tests prove the two
agree, not that a real vehicle agrees with either. Timing and keepalive behaviour in particular
are the sort of thing only a real car settles. Treat it as a careful hypothesis.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from types import TracebackType

import can

from carpi.core.transport.base import EcuAddress, NoResponse, TransportError

__all__ = [
    "BROADCAST_ID",
    "VAG_MODULES",
    "Tp20Channel",
    "Tp20Error",
    "Tp20Params",
    "open_tp20_channel",
]

log = logging.getLogger(__name__)

# Channel setup is always addressed here; the reply comes back on 0x200 + logical address.
BROADCAST_ID = 0x200

OPCODE_SETUP_REQUEST = 0xC0
OPCODE_SETUP_OK = 0xD0
_SETUP_REFUSALS = {0xD6: "refused", 0xD7: "refused", 0xD8: "refused"}

OPCODE_PARAM_REQUEST = 0xA0
OPCODE_PARAM_RESPONSE = 0xA1
OPCODE_CHANNEL_TEST = 0xA3
OPCODE_BREAK = 0xA4
OPCODE_DISCONNECT = 0xA8

# Data PCI high nibble. The low nibble carries a sequence number that wraps at 0x0F.
OP_MORE_WITH_ACK = 0x0
OP_LAST_WITH_ACK = 0x1
OP_MORE_NO_ACK = 0x2
OP_LAST_NO_ACK = 0x3
OP_ACK_NOT_READY = 0x9
OP_ACK_READY = 0xB

# The application type byte in channel setup. 0x01 selects KWP2000, which is the only
# application this module has any business speaking.
APP_TYPE_KWP2000 = 0x01

# The tester asks the module to transmit to one of these. Anything in 0x300-0x310 is
# conventional; the module replies telling us which ID to send to (commonly 0x740).
DEFAULT_TESTER_RX_ID = 0x300

# Well-known VAG logical addresses -- the numbers VCDS displays. Stable across the whole
# KWP2000 era, which is why they can live in code rather than in a per-car definition.
VAG_MODULES: dict[int, str] = {
    0x01: "Engine",
    0x02: "Auto transmission",
    0x03: "ABS brakes",
    0x08: "Auto HVAC",
    0x09: "Central electrics",
    0x0F: "Digital radio",
    0x15: "Airbags",
    0x16: "Steering wheel",
    0x17: "Instruments",
    0x19: "CAN gateway",
    0x1C: "Position sensing",
    0x25: "Immobiliser",
    0x2B: "Steering column lock",
    0x36: "Seat memory driver",
    0x37: "Navigation",
    0x3F: "Immobiliser III",
    0x42: "Door electronics driver",
    0x44: "Steering assist",
    0x46: "Central convenience",
    0x47: "Sound system",
    0x52: "Door electronics passenger",
    0x53: "Parking brake",
    0x55: "Headlight range",
    0x56: "Radio",
    0x5F: "Information control head",
    0x62: "Door rear left",
    0x72: "Door rear right",
}

# Modules whose misconfiguration is dangerous or immobilising. Advisory for reads, which
# are always safe; the coding path refuses them outright.
SAFETY_CRITICAL_MODULES = frozenset({0x03, 0x15, 0x16, 0x25, 0x2B, 0x44, 0x53})


class Tp20Error(TransportError):
    """A TP2.0 channel could not be established or maintained."""


@dataclass(frozen=True)
class Tp20Params:
    """Negotiated channel parameters."""

    block_size: int = 0x0F
    t1: float = 0.1
    t3: float = 0.001

    @staticmethod
    def decode_timing(raw: int) -> float:
        """Decode a TP2.0 timing byte into seconds.

        The top two bits select a unit -- 0.1 ms, 1 ms, 10 ms, 100 ms -- and the low six
        bits are a multiplier.
        """
        units = (0.0001, 0.001, 0.01, 0.1)
        return units[(raw >> 6) & 0x03] * (raw & 0x3F)


class Tp20Channel:
    """A KWP2000-carrying channel to one module.

    Presents the same ``request``/``address`` interface as an ISO-TP channel, so the
    KWP2000 client above it neither knows nor cares which transport it is on.

    Use as a context manager. A channel left open holds resources on the module, and the
    module will drop it when the keepalive stops -- but disconnecting cleanly is better
    manners and avoids a stale channel on the next attempt.
    """

    def __init__(
        self,
        bus: can.BusABC,
        reader: can.BufferedReader,
        *,
        logical_address: int,
        tx_id: int,
        rx_id: int,
        params: Tp20Params,
        link=None,
    ) -> None:
        self._bus = bus
        self._reader = reader
        # Held so close() can detach the reader. A leaked listener keeps receiving frames
        # from whatever runs next, and the resulting confusion surfaces far from here.
        self._link = link
        self._logical_address = logical_address
        self._tx_id = tx_id
        self._rx_id = rx_id
        self._params = params
        self._sequence = 0
        self._closed = False
        self._keepalive_stop = threading.Event()
        self._keepalive: threading.Thread | None = None

    @property
    def address(self) -> EcuAddress:
        """Presented for reporting. The IDs were negotiated, not looked up."""
        return EcuAddress(
            tx_id=self._tx_id,
            rx_id=self._rx_id,
            name=f"{VAG_MODULES.get(self._logical_address, 'module')} "
            f"({self._logical_address:02X})",
        )

    @property
    def logical_address(self) -> int:
        return self._logical_address

    # --- keepalive ------------------------------------------------------------

    def start_keepalive(self, interval: float = 0.5) -> None:
        """Keep the channel alive between requests.

        TP2.0 modules close a channel that goes quiet, and the timeout is short. Reading
        a long list of measuring blocks with pauses in between needs this running or the
        channel dies partway through -- which looks like the module refusing a value
        rather than the transport giving up.
        """
        if self._keepalive is not None:
            return
        self._keepalive = threading.Thread(
            target=self._keepalive_loop, args=(interval,), name="tp20-keepalive", daemon=True
        )
        self._keepalive.start()

    def _keepalive_loop(self, interval: float) -> None:
        while not self._keepalive_stop.wait(interval):
            try:
                self._send_frame(bytes([OPCODE_CHANNEL_TEST]))
            except Exception:  # noqa: BLE001 - a dead channel is handled by the next request
                log.debug("keepalive failed on %s", self.address, exc_info=True)
                return

    # --- framing --------------------------------------------------------------

    def _send_frame(self, payload: bytes) -> None:
        message = can.Message(
            arbitration_id=self._tx_id,
            data=payload,
            is_extended_id=False,
        )
        try:
            self._bus.send(message)
        except can.CanError as exc:
            raise Tp20Error(f"could not send on 0x{self._tx_id:03X}: {exc}") from exc

    def _next_sequence(self) -> int:
        value = self._sequence
        self._sequence = (self._sequence + 1) & 0x0F
        return value

    def request(self, payload: bytes, timeout: float = 1.0) -> bytes:
        """Send a KWP2000 message and return the reply."""
        if self._closed:
            raise Tp20Error("channel is closed")
        self._drain()
        self._send_message(bytes(payload), timeout=timeout)
        return self._receive_message(timeout=timeout)

    def _drain(self) -> None:
        while self._reader.get_message(timeout=0.0) is not None:
            pass

    def _send_message(self, payload: bytes, *, timeout: float) -> None:
        """Segment and send, waiting for an acknowledgement after the final packet.

        The first packet carries a two-byte big-endian length ahead of the payload, so a
        receiver knows how much to expect before the segments arrive.
        """
        body = len(payload).to_bytes(2, "big") + payload
        chunks = [body[index : index + 7] for index in range(0, len(body), 7)]

        for position, chunk in enumerate(chunks):
            last = position == len(chunks) - 1
            opcode = OP_LAST_WITH_ACK if last else OP_MORE_NO_ACK
            self._send_frame(bytes([(opcode << 4) | self._next_sequence()]) + chunk)
            # T3 paces consecutive frames. Sending faster than the module negotiated is
            # how a transfer fails on a real car but not against a simulator.
            if not last and self._params.t3:
                time.sleep(self._params.t3)

        self._await_ack(timeout=timeout)

    def _await_ack(self, *, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NoResponse(f"{self.address} did not acknowledge within {timeout:g}s")
            message = self._reader.get_message(timeout=remaining)
            if message is None or message.arbitration_id != self._rx_id or not message.data:
                continue
            opcode = (message.data[0] >> 4) & 0x0F
            if opcode == OP_ACK_READY:
                return
            if opcode == OP_ACK_NOT_READY:
                # The module is busy. Keep waiting rather than treating it as a refusal.
                log.debug("%s not ready for more data", self.address)
                continue
            if opcode == OPCODE_CHANNEL_TEST >> 4:
                continue

    def _receive_message(self, *, timeout: float) -> bytes:
        """Reassemble a segmented reply."""
        deadline = time.monotonic() + timeout
        expected: int | None = None
        collected = bytearray()

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise NoResponse(f"{self.address} did not reply within {timeout:g}s")
            message = self._reader.get_message(timeout=remaining)
            if message is None or message.arbitration_id != self._rx_id or not message.data:
                continue

            first = message.data[0]
            opcode = (first >> 4) & 0x0F
            if opcode in (OP_ACK_READY, OP_ACK_NOT_READY):
                continue
            if first in (OPCODE_CHANNEL_TEST, OPCODE_PARAM_REQUEST, OPCODE_PARAM_RESPONSE):
                continue
            if opcode not in (OP_MORE_WITH_ACK, OP_LAST_WITH_ACK, OP_MORE_NO_ACK, OP_LAST_NO_ACK):
                continue

            collected.extend(message.data[1:])
            if expected is None and len(collected) >= 2:
                expected = int.from_bytes(collected[:2], "big")

            needs_ack = opcode in (OP_MORE_WITH_ACK, OP_LAST_WITH_ACK)
            if needs_ack:
                self._send_frame(bytes([(OP_ACK_READY << 4) | self._next_sequence()]))

            if opcode in (OP_LAST_WITH_ACK, OP_LAST_NO_ACK):
                break
            # Reassembly can outlast the per-request window on a slow module, so the
            # deadline is extended while segments are actually arriving.
            deadline = time.monotonic() + timeout

        if expected is None:
            raise Tp20Error(f"{self.address} sent a reply with no length header")
        body = bytes(collected[2 : 2 + expected])
        if len(body) < expected:
            raise Tp20Error(f"{self.address} announced {expected} bytes but sent {len(body)}")
        return body

    # --- teardown -------------------------------------------------------------

    def close(self) -> None:
        """Disconnect cleanly. Safe to call twice."""
        if self._closed:
            return
        self._closed = True
        self._keepalive_stop.set()
        if self._keepalive is not None:
            self._keepalive.join(timeout=1.0)
            self._keepalive = None
        try:
            self._send_frame(bytes([OPCODE_DISCONNECT]))
        except Exception:  # noqa: BLE001 - teardown must not mask the original error
            log.debug("disconnect failed on %s", self.address, exc_info=True)
        if self._link is not None:
            self._link.detach_listener(self._reader)
        self._reader.stop()

    def __enter__(self) -> Tp20Channel:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def open_tp20_channel(
    link,
    logical_address: int,
    *,
    timeout: float = 1.0,
    tester_rx_id: int = DEFAULT_TESTER_RX_ID,
) -> Tp20Channel:
    """Negotiate a TP2.0 channel to a module by its VAG logical address.

    *link* is a :class:`~carpi.core.transport.canbus.CanLink`. The returned channel owns a
    raw reader on that link for its lifetime, so close it when finished.
    """
    if not 0x00 <= logical_address <= 0xFF:
        raise Tp20Error(f"logical address 0x{logical_address:X} is out of range")

    reader = can.BufferedReader()
    link.attach_listener(reader)
    try:
        params, tx_id, rx_id = _negotiate(
            link, reader, logical_address, timeout=timeout, tester_rx_id=tester_rx_id
        )
    except Exception:
        link.detach_listener(reader)
        reader.stop()
        raise

    return Tp20Channel(
        link.bus,
        reader,
        logical_address=logical_address,
        tx_id=tx_id,
        rx_id=rx_id,
        params=params,
        link=link,
    )


def _negotiate(
    link,
    reader: can.BufferedReader,
    logical_address: int,
    *,
    timeout: float,
    tester_rx_id: int,
) -> tuple[Tp20Params, int, int]:
    """Channel setup followed by parameter negotiation."""
    setup = bytes(
        [
            logical_address,
            OPCODE_SETUP_REQUEST,
            tester_rx_id & 0xFF,
            (tester_rx_id >> 8) & 0x0F,
            0x00,
            0x00,
            APP_TYPE_KWP2000,
        ]
    )
    link.send_raw(BROADCAST_ID, setup)

    reply = _await_id(reader, BROADCAST_ID + logical_address, timeout=timeout)
    if reply is None:
        raise NoResponse(
            f"no module answered channel setup for logical address "
            f"0x{logical_address:02X} ({VAG_MODULES.get(logical_address, 'unknown')})"
        )
    if len(reply) < 6:
        raise Tp20Error(f"channel setup reply too short: {reply.hex(' ')}")

    opcode = reply[1]
    if opcode != OPCODE_SETUP_OK:
        reason = _SETUP_REFUSALS.get(opcode, f"opcode 0x{opcode:02X}")
        raise Tp20Error(
            f"module 0x{logical_address:02X} refused the channel ({reason}). On a real "
            f"vehicle this usually means the module is not present on this platform."
        )

    # Bytes 4-5 are the ID the tester must transmit to from now on.
    tx_id = reply[4] | ((reply[5] & 0x0F) << 8)
    rx_id = tester_rx_id
    log.debug(
        "channel to 0x%02X: tester sends 0x%03X, receives 0x%03X",
        logical_address,
        tx_id,
        rx_id,
    )

    params = _negotiate_params(link, reader, tx_id=tx_id, rx_id=rx_id, timeout=timeout)
    return params, tx_id, rx_id


def _negotiate_params(
    link,
    reader: can.BufferedReader,
    *,
    tx_id: int,
    rx_id: int,
    timeout: float,
) -> Tp20Params:
    request = bytes([OPCODE_PARAM_REQUEST, 0x0F, 0x8A, 0xFF, 0x32, 0xFF])
    link.send_raw(tx_id, request)

    reply = _await_id(reader, rx_id, timeout=timeout, expect_first=OPCODE_PARAM_RESPONSE)
    if reply is None or len(reply) < 6:
        # Defaults are conservative rather than fast. A module that will not negotiate
        # may still talk, and failing here would lose that.
        log.debug("no parameter response; using conservative defaults")
        return Tp20Params()

    return Tp20Params(
        block_size=reply[1],
        t1=Tp20Params.decode_timing(reply[2]),
        t3=Tp20Params.decode_timing(reply[4]),
    )


def _await_id(
    reader: can.BufferedReader,
    arbitration_id: int,
    *,
    timeout: float,
    expect_first: int | None = None,
) -> bytes | None:
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        message = reader.get_message(timeout=remaining)
        if message is None or message.arbitration_id != arbitration_id:
            continue
        data = bytes(message.data)
        if expect_first is not None and (not data or data[0] != expect_first):
            continue
        return data
