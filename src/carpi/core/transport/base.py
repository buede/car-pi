"""Addressing and the channel interface the protocol layers depend on.

OBD-II diagnostic addressing is defined by ISO 15765-4. Two layouts exist and a
vehicle uses exactly one of them:

**11-bit.** A tester broadcasts on ``0x7DF`` and every ECU that implements OBD-II
answers, each on its own ID in ``0x7E8..0x7EF``. To address one ECU directly, send to
its reply ID minus 8 -- so the ECU replying on ``0x7E8`` is addressed at ``0x7E0``.

**29-bit.** The tester's own address is ``0xF1`` by convention. Functional requests go
to ``0x18DB33F1``; an ECU with address ``nn`` replies on ``0x18DAF1nn`` and is
addressed directly at ``0x18DAnnF1``. Note the target and source bytes swap between
request and reply, which is the easy thing to get backwards.

Discovery therefore works by broadcasting once and noting who answers, rather than by
probing addresses -- one frame instead of hundreds, and it cannot miss an ECU at an
address nobody thought to try.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = [
    "FUNCTIONAL_REQUEST_11BIT",
    "FUNCTIONAL_REQUEST_29BIT",
    "TESTER_ADDRESS",
    "Channel",
    "EcuAddress",
    "NoResponse",
    "TransportError",
]

FUNCTIONAL_REQUEST_11BIT = 0x7DF
RESPONSE_BASE_11BIT = 0x7E8
RESPONSE_LAST_11BIT = 0x7EF
_PHYSICAL_OFFSET_11BIT = 8

FUNCTIONAL_REQUEST_29BIT = 0x18DB33F1
_RESPONSE_PREFIX_29BIT = 0x18DAF100
_REQUEST_PREFIX_29BIT = 0x18DA0000
TESTER_ADDRESS = 0xF1


class TransportError(Exception):
    """The transport could not carry out the exchange."""


class NoResponse(TransportError):
    """No reply arrived within the timeout.

    Routine during a scan, not exceptional: an ECU that does not implement a mode
    simply says nothing. Callers treat this as "unsupported", never as "value zero".
    """


@dataclass(frozen=True)
class EcuAddress:
    """A request/reply ID pair identifying one ECU on one bus."""

    tx_id: int
    rx_id: int
    extended: bool = False
    name: str | None = None

    @classmethod
    def from_response_id(cls, rx_id: int, *, extended: bool) -> EcuAddress:
        """Derive the address to send to, from an ID we saw an ECU reply on."""
        if extended:
            if (rx_id & 0xFFFFFF00) != _RESPONSE_PREFIX_29BIT:
                raise ValueError(
                    f"0x{rx_id:08X} is not an OBD-II 29-bit response ID "
                    f"(expected 0x{_RESPONSE_PREFIX_29BIT:08X} | ecu)"
                )
            ecu = rx_id & 0xFF
            tx_id = _REQUEST_PREFIX_29BIT | (ecu << 8) | TESTER_ADDRESS
            return cls(tx_id=tx_id, rx_id=rx_id, extended=True)

        if not RESPONSE_BASE_11BIT <= rx_id <= RESPONSE_LAST_11BIT:
            raise ValueError(
                f"0x{rx_id:03X} is not an OBD-II 11-bit response ID "
                f"(expected 0x{RESPONSE_BASE_11BIT:03X}-0x{RESPONSE_LAST_11BIT:03X})"
            )
        return cls(tx_id=rx_id - _PHYSICAL_OFFSET_11BIT, rx_id=rx_id, extended=False)

    @classmethod
    def functional(cls, *, extended: bool = False) -> EcuAddress:
        """The broadcast address every OBD-II ECU listens on.

        The reply ID is nominally the first response ID, but a functional request
        draws answers from several ECUs at once, so use this for discovery only --
        collect raw frames rather than expecting one framed reply.
        """
        if extended:
            return cls(
                tx_id=FUNCTIONAL_REQUEST_29BIT,
                rx_id=_RESPONSE_PREFIX_29BIT,
                extended=True,
                name="functional",
            )
        return cls(
            tx_id=FUNCTIONAL_REQUEST_11BIT,
            rx_id=RESPONSE_BASE_11BIT,
            extended=False,
            name="functional",
        )

    @property
    def ecu_number(self) -> int:
        """Zero-based index of this ECU among the OBD-II reply addresses."""
        if self.extended:
            return self.rx_id & 0xFF
        return self.rx_id - RESPONSE_BASE_11BIT

    @property
    def label(self) -> str:
        """Short human label, e.g. ``7E0/7E8``."""
        if self.name:
            return self.name
        width = 8 if self.extended else 3
        return f"{self.tx_id:0{width}X}/{self.rx_id:0{width}X}"

    def __str__(self) -> str:
        return self.label


@runtime_checkable
class Channel(Protocol):
    """A request/response pipe to one ECU, with segmentation already handled."""

    @property
    def address(self) -> EcuAddress:
        """Which ECU this channel talks to."""
        ...

    def request(self, payload: bytes, timeout: float = 1.0) -> bytes:
        """Send *payload* and return the reply payload.

        Raises :class:`NoResponse` if nothing arrives within *timeout*.
        """
        ...
