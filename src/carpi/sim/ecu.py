"""A virtual ECU that speaks real OBD-II over a real ISO-TP stack.

Each simulated module listens on two addresses, as a real one does: its own physical
request ID, and the functional broadcast address every OBD-II ECU must answer. That is
what makes ECU discovery testable -- several simulated modules answer one broadcast,
exactly as they would in a car.

Unsupported PIDs get no reply at all, rather than a negative response. Real vehicles
mostly behave that way, and it means the client's "no answer means unsupported, never
means zero" path is exercised on every scan.
"""

from __future__ import annotations

import logging
import threading
from types import TracebackType

import can
import isotp

from carpi.core.protocol.dtc import encode_dtc
from carpi.sim import encode as enc
from carpi.sim.scenarios import EcuSpec, Scenario

__all__ = ["SimulatedVehicle", "VirtualEcu"]

log = logging.getLogger(__name__)

_FUNCTIONAL_11BIT = 0x7DF
_FUNCTIONAL_29BIT = 0x18DB33F1
_RESPONSE_BASE_11BIT = 0x7E8

MODE_LIVE = 0x01
MODE_FREEZE = 0x02
MODE_STORED = 0x03
MODE_CLEAR = 0x04
MODE_MONITOR = 0x06
MODE_PENDING = 0x07
MODE_INFO = 0x09
MODE_PERMANENT = 0x0A

_INFO_VIN = 0x02
_INFO_CAL_ID = 0x04
_INFO_CVN = 0x06
_INFO_ECU_NAME = 0x0A

_NRC_SERVICE_NOT_SUPPORTED = 0x11

_ISOTP_PARAMS = {
    "blocksize": 8,
    "stmin": 0,
    "tx_padding": 0x00,
    "tx_data_length": 8,
    "rx_flowcontrol_timeout": 1000,
    "rx_consecutive_frame_timeout": 1000,
    "max_frame_size": 4095,
    "blocking_send": False,
}


def _bitmap_bases(items: set[int]) -> list[int]:
    """Which support-bitmap PIDs to expose, given the items actually supported."""
    bases = [0x00]
    for base in (0x20, 0x40, 0x60, 0x80, 0xA0, 0xC0):
        if any(item > base for item in items):
            bases.append(base)
        else:
            break
    return bases


def _dtc_response(service: int, codes: tuple[str, ...]) -> bytes:
    """Build a Mode 03/07/0A response, including the DTC count byte."""
    payload = bytearray([service + 0x40, len(codes)])
    for code in codes:
        payload.extend(encode_dtc(code))
    return bytes(payload)


class VirtualEcu:
    """Answers OBD-II requests from a static :class:`EcuSpec`."""

    def __init__(self, spec: EcuSpec) -> None:
        self.spec = spec
        self._live_bases = _bitmap_bases(set(spec.pids))
        self._live_supported = set(spec.pids) | set(self._live_bases)

        self._freeze_bases = _bitmap_bases(set(spec.freeze_frame)) if spec.freeze_frame else []
        self._freeze_supported = set(spec.freeze_frame) | set(self._freeze_bases)

        monitor_ids = {test[0] for test in spec.monitor_tests}
        self._monitor_bases = _bitmap_bases(monitor_ids) if monitor_ids else []
        self._monitor_supported = monitor_ids | set(self._monitor_bases)

        self._info_items = {_INFO_VIN} if spec.vin else set()
        if spec.calibration_ids:
            self._info_items.add(_INFO_CAL_ID)
        if spec.cvns:
            self._info_items.add(_INFO_CVN)
        if spec.ecu_name:
            self._info_items.add(_INFO_ECU_NAME)
        self._info_bases = _bitmap_bases(self._info_items) if self._info_items else [0x00]

        # Any Mode 04 (clear fault codes) request that reaches this ECU is recorded.
        # The test suite asserts this stays empty across every scan: the strongest
        # available guarantee that the inspection path cannot destroy the evidence it
        # is meant to be collecting.
        self.clear_requests: list[bytes] = []
        self.received: list[bytes] = []

    @property
    def label(self) -> str:
        return self.spec.label

    def handle(self, request: bytes) -> bytes | None:
        """Answer one request, or return ``None`` to stay silent."""
        if not request:
            return None
        self.received.append(bytes(request))
        mode = request[0]

        if mode == MODE_CLEAR:
            self.clear_requests.append(bytes(request))
            log.warning("%s: received Mode 04 clear-codes request", self.spec.label)
            return bytes([MODE_CLEAR + 0x40])

        if mode == MODE_LIVE:
            return self._live(request)
        if mode == MODE_FREEZE:
            return self._freeze(request)
        if mode == MODE_STORED:
            return _dtc_response(MODE_STORED, self.spec.stored_dtcs)
        if mode == MODE_PENDING:
            return _dtc_response(MODE_PENDING, self.spec.pending_dtcs)
        if mode == MODE_PERMANENT:
            return _dtc_response(MODE_PERMANENT, self.spec.permanent_dtcs)
        if mode == MODE_MONITOR:
            return self._monitor(request)
        if mode == MODE_INFO:
            return self._info(request)

        return bytes([0x7F, mode, _NRC_SERVICE_NOT_SUPPORTED])

    def _live(self, request: bytes) -> bytes | None:
        if len(request) < 2:
            return None
        pid = request[1]
        if pid in self._live_bases:
            return bytes([0x41, pid]) + enc.support_bitmap(self._live_supported, pid)
        data = self.spec.pids.get(pid)
        return None if data is None else bytes([0x41, pid]) + data

    def _freeze(self, request: bytes) -> bytes | None:
        if len(request) < 3:
            return None
        pid, frame = request[1], request[2]
        if frame != 0 or not self.spec.freeze_frame:
            return None
        if pid in self._freeze_bases:
            bitmap = enc.support_bitmap(self._freeze_supported, pid)
            return bytes([0x42, pid, frame]) + bitmap
        data = self.spec.freeze_frame.get(pid)
        return None if data is None else bytes([0x42, pid, frame]) + data

    def _monitor(self, request: bytes) -> bytes | None:
        if len(request) < 2:
            return None
        monitor_id = request[1]
        if monitor_id in self._monitor_bases:
            bitmap = enc.support_bitmap(self._monitor_supported, monitor_id)
            return bytes([0x46, monitor_id]) + bitmap
        groups = [test for test in self.spec.monitor_tests if test[0] == monitor_id]
        if not groups:
            return None
        payload = bytearray([0x46])
        for mid, tid, uas, value, minimum, maximum in groups:
            payload.extend([mid, tid, uas])
            payload.extend(value.to_bytes(2, "big"))
            payload.extend(minimum.to_bytes(2, "big"))
            payload.extend(maximum.to_bytes(2, "big"))
        return bytes(payload)

    def _info(self, request: bytes) -> bytes | None:
        if len(request) < 2:
            return None
        item = request[1]
        if item in self._info_bases:
            return bytes([0x49, item]) + enc.support_bitmap(self._info_items, item)
        if item == _INFO_VIN and self.spec.vin:
            return bytes([0x49, item, 1]) + self.spec.vin.encode("ascii")
        if item == _INFO_CAL_ID and self.spec.calibration_ids:
            body = b"".join(
                text.encode("ascii").ljust(16, b"\x00") for text in self.spec.calibration_ids
            )
            return bytes([0x49, item, len(self.spec.calibration_ids)]) + body
        if item == _INFO_CVN and self.spec.cvns:
            body = b"".join(value.ljust(4, b"\x00")[:4] for value in self.spec.cvns)
            return bytes([0x49, item, len(self.spec.cvns)]) + body
        if item == _INFO_ECU_NAME and self.spec.ecu_name:
            body = self.spec.ecu_name.encode("ascii").ljust(20, b"\x00")[:20]
            return bytes([0x49, item, 1]) + body
        return None


class SimulatedVehicle:
    """Serves a set of :class:`VirtualEcu` instances on a CAN bus.

    Use as a context manager. Buses are torn down on exit, which matters for the
    virtual transport too: a leaked bus keeps receiving frames from the next test and
    the failures land somewhere unrelated.
    """

    def __init__(
        self,
        ecus: list[VirtualEcu],
        *,
        bus: can.BusABC | None = None,
        channel: str = "carpi",
        kind: str = "virtual",
        extended: bool = False,
    ) -> None:
        self.ecus = ecus
        self._extended = extended
        self._owns_bus = bus is None
        if bus is not None:
            self._bus = bus
        elif kind == "virtual":
            self._bus = can.interface.Bus(interface="virtual", channel=channel)
        elif kind == "udp":
            self._bus = can.interface.Bus(interface="udp_multicast", channel=channel)
        else:
            raise ValueError(f"the simulator supports 'virtual' and 'udp', not {kind!r}")

        self._notifier = can.Notifier(self._bus, listeners=[], timeout=0.05)
        self._stacks: list[tuple[VirtualEcu, isotp.TransportLayer]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def from_scenario(cls, scenario: Scenario, **kwargs: object) -> SimulatedVehicle:
        """Build a simulated vehicle from a named scenario."""
        return cls([VirtualEcu(spec) for spec in scenario.ecus], **kwargs)  # type: ignore[arg-type]

    @property
    def bus(self) -> can.BusABC:
        return self._bus

    def _addresses(self, response_id: int) -> tuple[isotp.Address, isotp.AsymmetricAddress]:
        """Physical and functional addresses for one ECU, from the ECU's own side."""
        if self._extended:
            index = response_id - _RESPONSE_BASE_11BIT
            mode = isotp.AddressingMode.Normal_29bits
            tx_id = 0x18DAF100 | index
            rx_id = 0x18DA0000 | (index << 8) | 0xF1
            functional_rx = _FUNCTIONAL_29BIT
        else:
            mode = isotp.AddressingMode.Normal_11bits
            tx_id = response_id
            rx_id = response_id - 8
            functional_rx = _FUNCTIONAL_11BIT

        physical = isotp.Address(mode, txid=tx_id, rxid=rx_id)
        functional = isotp.AsymmetricAddress(
            tx_addr=isotp.Address(mode, txid=tx_id, tx_only=True),
            rx_addr=isotp.Address(mode, rxid=functional_rx, rx_only=True),
        )
        return physical, functional

    def start(self) -> None:
        """Bring up the ISO-TP stacks and begin answering requests."""
        if self._thread is not None:
            return
        for ecu in self.ecus:
            for address in self._addresses(ecu.spec.response_id):
                stack = isotp.NotifierBasedCanStack(
                    bus=self._bus,
                    notifier=self._notifier,
                    address=address,
                    params=dict(_ISOTP_PARAMS),
                )
                stack.start()
                self._stacks.append((ecu, stack))

        self._thread = threading.Thread(target=self._serve, name="carpi-sim", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            handled_any = False
            for ecu, stack in self._stacks:
                try:
                    request = stack.recv(block=False)
                except Exception:  # noqa: BLE001 - a sim must not die on a malformed frame
                    log.debug("%s: receive error", ecu.label, exc_info=True)
                    continue
                if request is None:
                    continue
                handled_any = True
                try:
                    reply = ecu.handle(bytes(request))
                except Exception:  # noqa: BLE001
                    log.exception("%s: failed to handle %s", ecu.label, bytes(request).hex(" "))
                    continue
                if reply is not None:
                    stack.send(reply)
            if not handled_any:
                self._stop.wait(0.001)

    def stop(self) -> None:
        """Stop serving and release the bus. Safe to call more than once."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for _, stack in self._stacks:
            try:
                stack.stop()
            except Exception:  # noqa: BLE001
                log.debug("error stopping simulator stack", exc_info=True)
        self._stacks.clear()
        try:
            self._notifier.stop()
        except Exception:  # noqa: BLE001
            log.debug("error stopping simulator notifier", exc_info=True)
        if self._owns_bus:
            try:
                self._bus.shutdown()
            except Exception:  # noqa: BLE001
                log.debug("error shutting down simulator bus", exc_info=True)

    @property
    def clear_code_requests(self) -> list[bytes]:
        """Every Mode 04 request any simulated ECU received. Should always be empty."""
        return [request for ecu in self.ecus for request in ecu.clear_requests]

    def __enter__(self) -> SimulatedVehicle:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
