"""A simulated VAG module speaking KWP2000 over TP2.0.

Written from the protocol description at https://jazdw.net/tp20 as an independent
implementation, deliberately not by calling into
:mod:`carpi.core.transport.tp20`. If both sides shared code, a round-trip test would
prove only that the code agrees with itself -- the same trap the OBD-II encoders in
:mod:`carpi.sim.encode` avoid.

That still leaves a real limit worth stating plainly: **both sides were written from the
same document, with no independently built implementation to check either against.** A
shared misreading of the specification would pass every test here and fail on a real
vehicle. These tests establish internal consistency and catch regressions; only the car
establishes correctness.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field

import can

__all__ = ["SimulatedTp20Module", "Tp20Responder"]

log = logging.getLogger(__name__)

BROADCAST_ID = 0x200

OPCODE_SETUP_REQUEST = 0xC0
OPCODE_SETUP_OK = 0xD0
OPCODE_SETUP_REFUSED = 0xD6
OPCODE_PARAM_REQUEST = 0xA0
OPCODE_PARAM_RESPONSE = 0xA1
OPCODE_CHANNEL_TEST = 0xA3
OPCODE_DISCONNECT = 0xA8

OP_MORE_WITH_ACK = 0x0
OP_LAST_WITH_ACK = 0x1
OP_MORE_NO_ACK = 0x2
OP_LAST_NO_ACK = 0x3
OP_ACK_READY = 0xB

# KWP2000, read-only. Deliberately mirrors what the real client sends.
SERVICE_START_SESSION = 0x10
SERVICE_READ_DTC_BY_STATUS = 0x18
SERVICE_READ_ECU_ID = 0x1A
SERVICE_READ_LOCAL_ID = 0x21
SERVICE_READ_COMMON_ID = 0x22
SERVICE_TESTER_PRESENT = 0x3E
SERVICE_SECURITY_ACCESS = 0x27
SERVICE_WRITE_LOCAL_ID = 0x3B

NRC_SERVICE_NOT_SUPPORTED = 0x11
NRC_REQUEST_OUT_OF_RANGE = 0x31
NRC_SECURITY_ACCESS_DENIED = 0x33
NRC_INVALID_KEY = 0x35


@dataclass
class SimulatedTp20Module:
    """One VAG module addressed by logical address."""

    logical_address: int
    label: str
    # The module transmits to the tester on this ID and receives on the one the tester
    # nominated during setup.
    module_tx_id: int = 0x740

    identification: bytes = b"\x03\x1a\x80" + b"3C0920870A  KOMBIINSTRUMENT VDD"[:29]
    # Measuring blocks: group -> (formula, a, b) triples.
    blocks: dict[int, tuple[tuple[int, int, int], ...]] = field(default_factory=dict)
    local_ids: dict[int, bytes] = field(default_factory=dict)
    common_ids: dict[int, bytes] = field(default_factory=dict)
    # VAG fault codes as five-digit numbers, with a status byte.
    dtcs: tuple[tuple[int, int], ...] = ()
    # Coding, and the login required to change it. On this era a login is a five-digit
    # code, not a cryptographic handshake -- which is exactly why coding is feasible here
    # and impossible on a modern car.
    login_code: int | None = None
    coding: bytes = b"\x00\x00\x00"
    present: bool = True

    def __post_init__(self) -> None:
        self.logged_in = False
        self.write_attempts: list[bytes] = []
        self.received: list[bytes] = []

    # --- KWP2000 ---------------------------------------------------------------

    def handle(self, request: bytes) -> bytes | None:
        """Answer one KWP2000 message."""
        if not request:
            return None
        self.received.append(bytes(request))
        service = request[0]

        if service == SERVICE_TESTER_PRESENT:
            return bytes([0x7E])
        if service == SERVICE_START_SESSION:
            return bytes([0x50, request[1] if len(request) > 1 else 0x89])
        if service == SERVICE_READ_ECU_ID:
            return bytes([0x5A, request[1] if len(request) > 1 else 0x80]) + self.identification
        if service == SERVICE_READ_LOCAL_ID:
            return self._read_local(request)
        if service == SERVICE_READ_COMMON_ID:
            return self._read_common(request)
        if service == SERVICE_READ_DTC_BY_STATUS:
            return self._read_dtcs()
        if service == SERVICE_SECURITY_ACCESS:
            return self._login(request)
        if service == SERVICE_WRITE_LOCAL_ID:
            return self._write_local(request)

        return bytes([0x7F, service, NRC_SERVICE_NOT_SUPPORTED])

    def _read_local(self, request: bytes) -> bytes:
        if len(request) < 2:
            return bytes([0x7F, SERVICE_READ_LOCAL_ID, NRC_REQUEST_OUT_OF_RANGE])
        identifier = request[1]

        if identifier in self.blocks:
            payload = bytearray([0x61, identifier])
            for formula, a, b in self.blocks[identifier]:
                payload.extend([formula, a, b])
            return bytes(payload)
        if identifier in self.local_ids:
            return bytes([0x61, identifier]) + self.local_ids[identifier]
        return bytes([0x7F, SERVICE_READ_LOCAL_ID, NRC_REQUEST_OUT_OF_RANGE])

    def _read_common(self, request: bytes) -> bytes:
        if len(request) < 3:
            return bytes([0x7F, SERVICE_READ_COMMON_ID, NRC_REQUEST_OUT_OF_RANGE])
        identifier = (request[1] << 8) | request[2]
        data = self.common_ids.get(identifier)
        if data is None:
            return bytes([0x7F, SERVICE_READ_COMMON_ID, NRC_REQUEST_OUT_OF_RANGE])
        return bytes([0x62, request[1], request[2]]) + data

    def _read_dtcs(self) -> bytes:
        payload = bytearray([0x58, len(self.dtcs)])
        for code, status in self.dtcs:
            payload.extend([(code >> 8) & 0xFF, code & 0xFF, status])
        return bytes(payload)

    def _login(self, request: bytes) -> bytes:
        """VAG login. A five-digit code compared directly, not a seed/key exchange."""
        if self.login_code is None:
            return bytes([0x7F, SERVICE_SECURITY_ACCESS, NRC_SERVICE_NOT_SUPPORTED])
        if len(request) < 4:
            return bytes([0x7F, SERVICE_SECURITY_ACCESS, NRC_INVALID_KEY])
        offered = (request[2] << 8) | request[3]
        if offered != self.login_code:
            return bytes([0x7F, SERVICE_SECURITY_ACCESS, NRC_INVALID_KEY])
        self.logged_in = True
        return bytes([0x67, request[1]])

    def _write_local(self, request: bytes) -> bytes:
        """Recorded whether or not it is permitted, so tests can assert it never happens."""
        self.write_attempts.append(bytes(request))
        if not self.logged_in:
            return bytes([0x7F, SERVICE_WRITE_LOCAL_ID, NRC_SECURITY_ACCESS_DENIED])
        if len(request) < 3:
            return bytes([0x7F, SERVICE_WRITE_LOCAL_ID, NRC_REQUEST_OUT_OF_RANGE])

        identifier = request[1]
        value = bytes(request[2:])
        # The value written has to be the value read back, as on a real module. Updating
        # only a separate `coding` attribute made writes invisible to reads, which the
        # client's verify step correctly flagged as a failed write.
        self.local_ids[identifier] = value
        if identifier == 0x00:
            self.coding = value
        return bytes([0x7B, identifier])


class Tp20Responder:
    """Serves a set of :class:`SimulatedTp20Module` over TP2.0 on a CAN bus."""

    def __init__(self, bus: can.BusABC, modules: list[SimulatedTp20Module]) -> None:
        self._bus = bus
        self._modules = {module.logical_address: module for module in modules if module.present}
        self._notifier = can.Notifier(bus, listeners=[], timeout=0.05)
        self._reader = can.BufferedReader()
        self._notifier.add_listener(self._reader)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # Open channels, keyed by the ID the tester sends to.
        self._channels: dict[int, _Channel] = {}

    @property
    def modules(self) -> list[SimulatedTp20Module]:
        return list(self._modules.values())

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._serve, name="sim-tp20", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            message = self._reader.get_message(timeout=0.05)
            if message is None:
                continue
            try:
                self._dispatch(message)
            except Exception:  # noqa: BLE001 - a simulator must survive a malformed frame
                log.debug("tp20 responder error", exc_info=True)

    def _dispatch(self, message: can.Message) -> None:
        data = bytes(message.data)
        if message.arbitration_id == BROADCAST_ID:
            self._channel_setup(data)
            return
        channel = self._channels.get(message.arbitration_id)
        if channel is not None:
            channel.feed(data)

    def _channel_setup(self, data: bytes) -> None:
        if len(data) < 7 or data[1] != OPCODE_SETUP_REQUEST:
            return
        address = data[0]
        module = self._modules.get(address)
        if module is None:
            # A real bus is simply silent for an absent module, which is what makes
            # "module not fitted" and "module asleep" indistinguishable on a real car.
            return

        tester_rx_id = data[2] | ((data[3] & 0x0F) << 8)
        module_rx_id = module.module_tx_id

        # Byte 4-5 tell the tester which ID to transmit to from now on.
        reply = bytes(
            [
                0x00,
                OPCODE_SETUP_OK,
                tester_rx_id & 0xFF,
                (tester_rx_id >> 8) & 0x0F,
                module_rx_id & 0xFF,
                (module_rx_id >> 8) & 0x0F,
                0x01,
            ]
        )
        self._send(BROADCAST_ID + address, reply)

        self._channels[module_rx_id] = _Channel(
            responder=self,
            module=module,
            tester_rx_id=tester_rx_id,
        )

    def _send(self, arbitration_id: int, payload: bytes) -> None:
        self._bus.send(
            can.Message(arbitration_id=arbitration_id, data=payload, is_extended_id=False)
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        try:
            self._notifier.stop()
        except Exception:  # noqa: BLE001
            log.debug("error stopping tp20 notifier", exc_info=True)

    def __enter__(self) -> Tp20Responder:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class _Channel:
    """Reassembles requests and segments replies for one open channel."""

    def __init__(
        self,
        *,
        responder: Tp20Responder,
        module: SimulatedTp20Module,
        tester_rx_id: int,
    ) -> None:
        self._responder = responder
        self._module = module
        self._tester_rx_id = tester_rx_id
        self._sequence = 0
        self._buffer = bytearray()
        self._expected: int | None = None

    def feed(self, data: bytes) -> None:
        if not data:
            return
        first = data[0]

        if first == OPCODE_CHANNEL_TEST:
            self._responder._send(self._tester_rx_id, bytes([OPCODE_CHANNEL_TEST]))
            return
        if first == OPCODE_DISCONNECT:
            return
        if first == OPCODE_PARAM_REQUEST:
            # Block size then T1..T4, echoing what a module typically offers.
            self._responder._send(
                self._tester_rx_id,
                bytes([OPCODE_PARAM_RESPONSE, 0x0F, 0x8A, 0xFF, 0x32, 0xFF]),
            )
            return

        opcode = (first >> 4) & 0x0F
        if opcode not in (OP_MORE_WITH_ACK, OP_LAST_WITH_ACK, OP_MORE_NO_ACK, OP_LAST_NO_ACK):
            return

        self._buffer.extend(data[1:])
        if self._expected is None and len(self._buffer) >= 2:
            self._expected = int.from_bytes(self._buffer[:2], "big")

        if opcode in (OP_MORE_WITH_ACK, OP_LAST_WITH_ACK):
            self._responder._send(
                self._tester_rx_id, bytes([(OP_ACK_READY << 4) | self._next_sequence()])
            )

        if opcode not in (OP_LAST_WITH_ACK, OP_LAST_NO_ACK):
            return

        expected = self._expected or 0
        request = bytes(self._buffer[2 : 2 + expected])
        self._buffer.clear()
        self._expected = None

        reply = self._module.handle(request)
        if reply is not None:
            self._send_message(reply)

    def _next_sequence(self) -> int:
        value = self._sequence
        self._sequence = (self._sequence + 1) & 0x0F
        return value

    def _send_message(self, payload: bytes) -> None:
        body = len(payload).to_bytes(2, "big") + payload
        chunks = [body[index : index + 7] for index in range(0, len(body), 7)]
        for position, chunk in enumerate(chunks):
            last = position == len(chunks) - 1
            opcode = OP_LAST_NO_ACK if last else OP_MORE_NO_ACK
            self._responder._send(
                self._tester_rx_id,
                bytes([(opcode << 4) | self._next_sequence()]) + chunk,
            )
