"""Transports: how requests reach a vehicle.

Real vehicles are reached over SocketCAN with the kernel or userspace ISO-TP stack.
Development and CI use an in-process virtual bus so the whole tool runs on any OS
without hardware. Both present the same :class:`~carpi.core.transport.base.Channel`
interface, so nothing above this layer knows which is in use.
"""

from carpi.core.transport.base import (
    Channel,
    EcuAddress,
    NoResponse,
    TransportError,
)
from carpi.core.transport.canbus import CanLink, open_bus

__all__ = [
    "CanLink",
    "Channel",
    "EcuAddress",
    "NoResponse",
    "TransportError",
    "open_bus",
]
